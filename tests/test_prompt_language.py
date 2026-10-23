"""Prompt language must not replace the user's requested output language."""

import json
import re
from types import SimpleNamespace

import pytest

from folio.api import workspace
from folio.api.style_analysis import profile_context
from folio.orchestration.requests import RequestWorkflow
from folio.runtime.base import INSTRUCTIONS, prompt_for
from folio.runtime.language import LANGUAGE_INSTRUCTION, language_instruction, normalize_language
from folio.runtime.llm import LLMRuntime

SCHEMA = {"type": "object", "properties": {}, "required": []}
USER_REQUESTS = [
    "请根据上传资料编写投标文件，保留模板中的固定标题。",
    "Write an English bid proposal using the attached sources.",
    "请根据中文资料写一份英文投标文件。",
]


def test_all_task_instructions_are_english_and_include_language_policy_once():
    for task, instruction in INSTRUCTIONS.items():
        assert not re.search(r"[\u3400-\u9fff]", instruction), task
        prompt = prompt_for(task, {}, SCHEMA)
        assert prompt.count(LANGUAGE_INSTRUCTION) == 1, task
    assert "explicitly requests a different output language" in LANGUAGE_INSTRUCTION
    assert "original user request" in LANGUAGE_INSTRUCTION
    assert "Do not translate protocol keys" in LANGUAGE_INSTRUCTION


def test_explicit_workbench_language_overrides_generic_reference():
    english = language_instruction({"ui_language": "en"})
    chinese = language_instruction({"ui_language": "zh-CN"})
    assert LANGUAGE_INSTRUCTION in english and "Interface language" in english and "in English" in english
    assert LANGUAGE_INSTRUCTION in chinese and "in Simplified Chinese" in chinese
    assert normalize_language("en-US,en;q=0.9") == "en"
    assert normalize_language("zh-CN") == "zh-CN"


def test_request_context_keeps_explicit_language_for_work_tasks():
    workflow = RequestWorkflow()
    workflow.request = SimpleNamespace(
        user_request="Write the report.", writing_guide=None, language="en",
    )
    context = workflow.request_context("generate", {})
    assert context["document_language"] == "en"
    assert "ui_language" not in context
    assert "Interface language" not in language_instruction(context)
    assert LANGUAGE_INSTRUCTION in language_instruction(context)


@pytest.mark.parametrize("document_language,ui_language", [("en", "zh-CN"), ("zh-CN", "en")])
def test_request_context_keeps_document_language_separate_from_interface(document_language, ui_language):
    workflow = RequestWorkflow()
    workflow.request = SimpleNamespace(
        user_request="Original user instructions remain unchanged.",
        writing_guide=None, language=document_language, ui_language=ui_language,
    )
    original = {"internal_note": "A tool result in English."}
    for task in ("generate", "repair", "research_step", "understand_work", "format_document"):
        context = workflow.request_context(task, original)
        assert context["document_language"] == document_language
        assert context["ui_language"] == ui_language
        assert context["user_request"] == workflow.request.user_request
    assert original == {"internal_note": "A tool result in English."}


@pytest.mark.parametrize("user_request", USER_REQUESTS)
def test_wire_builders_preserve_original_language_reference(user_request):
    # Exercise actual wire assembly without creating a network client or calling a model.
    runtime = object.__new__(LLMRuntime)
    runtime.config = {"structured_output": "json_object", "provider_mode": "openai"}
    runtime.model = "test"
    for task in (
        "intent", "plan", "repair_plan", "directory_research", "research_step",
        "style_analysis", "generate", "repair", "understand_work", "review",
        "design_ui", "design_flowchart", "format_document",
    ):
        context = {
            "user_request": user_request,
            "requirements": user_request,
            "latest_user_message": user_request,
            "style_analysis_text": "Original sample: 原文引用 A-123.",
        }
        original = json.dumps(context, ensure_ascii=False)
        messages, *_ = runtime.request_parts(task, context, SCHEMA)
        system = messages[0]["content"]
        assert system.count(LANGUAGE_INSTRUCTION) == 1, task
        assert not re.search(r"[\u3400-\u9fff]", system), task
        assert any(user_request in str(message["content"]) for message in messages[1:]), task
        assert json.dumps(context, ensure_ascii=False) == original


@pytest.mark.parametrize("user_request", USER_REQUESTS)
def test_later_stages_receive_original_request_without_duplicating_planning_data(user_request):
    workflow = RequestWorkflow()
    workflow.request = SimpleNamespace(user_request=user_request, writing_guide=None)
    for task in (
        "generate", "repair", "integrate_supplement", "understand_work",
        "select_context", "review", "validate", "design_ui", "design_flowchart", "format_document",
    ):
        original = {"internal_note": "An English note from the preceding stage."}
        context = workflow.request_context(task, original)
        assert context["user_request"] == user_request, task
        assert "user_request" not in original
    for task in ("plan", "directory_research"):
        context = workflow.request_context(task, {"requirements": user_request})
        assert context["requirements"] == user_request
        assert "user_request" not in context


@pytest.mark.parametrize("user_request", USER_REQUESTS)
async def test_chat_uses_english_instructions_without_rewriting_user_messages(monkeypatch, user_request):
    captured = []

    class Runtime:
        model = "test"

        def __init__(self, config):
            pass

        async def stream(self, messages):
            captured.extend(messages)
            yield "completed", {"text": "Model reply"}

        async def aclose(self):
            pass

    monkeypatch.setattr(workspace, "LLMRuntime", Runtime)
    messages = [
        {"role": "assistant", "content": "Previous English response"},
        {"role": "user", "content": user_request},
    ]
    events = [event async for event in workspace.chat_events(None, messages, [])]
    assert events == [("message.completed", {"text": "Model reply"})]
    assert captured[1:] == messages
    system = captured[0]["content"]
    assert system.count(LANGUAGE_INSTRUCTION) == 1
    assert not re.search(r"[\u3400-\u9fff]", system.split("Workspace data:", 1)[0])


def test_chat_style_guidance_is_english_and_preserves_rules_and_display_summary():
    rule = "每段先交代背景，再说明观察结果。"
    profile = {"documents": [{
        "status": "analyzed", "rules": [{
            "dimension": "writing", "scope": "body", "description": rule,
            "confidence": "observed",
        }],
        "measurements": {"paragraph_count": 5, "median_paragraph_chars": 90,
                         "table_count_reliable": False},
        "layout": {"body": {"font_size_pt": 12, "bold": False, "alignment": "justify"}},
    }]}
    visible = profile_context(profile)
    prompt = profile_context(profile, for_model=True)
    assert rule in prompt and rule in visible
    assert "仅为详略参考，不是字数配额" in visible
    assert "reference for level of detail only, not a length quota" in prompt
    assert "do not infer that the original has no tables" in prompt
    assert "font_size_pt" in prompt and "12" in prompt
    assert not re.search(r"[\u3400-\u9fff]", prompt.replace(rule, ""))
