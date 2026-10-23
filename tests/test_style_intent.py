import json

import pytest
from pydantic import ValidationError

from folio.decisions import IntentDecision
from folio.decisions.routing import (
    DecisionRouter,
    style_selection_output_tokens,
    validate_style_intent,
)
from folio.model_settings import probe_fingerprint
from folio.requirements import mark_requirement, sync_requirements
from folio.runtime.base import INSTRUCTIONS, prompt_for

A, B, C = "a" * 32, "b" * 32, "c" * 32


def context(message, files=None):
    return {
        "latest_user_message": message,
        "workflow": {
            "files": files
            if files is not None
            else [
                {"id": A, "name": "银行范文.docx", "role": "style_reference"},
                {"id": B, "name": "合同范文.pdf", "role": "style_reference"},
                {"id": C, "name": "业务依据.md", "role": "source"},
            ],
            "style_analysis_available": True,
        },
    }


def decision(targets=None, intent="analyze_style"):
    return IntentDecision(
        intent=intent, summary="用户请求分析风格。", style_source_ids=targets or []
    )


def test_style_intent_schema_retains_legacy_intents_and_bounds_target_ids():
    schema = IntentDecision.model_json_schema()
    assert "analyze_style" in schema["properties"]["intent"]["enum"]
    target = schema["properties"]["style_source_ids"]
    assert target["maxItems"] == 200
    assert target["items"]["pattern"] == "^[a-f0-9]{32}$"
    assert set(schema["required"]) == set(schema["properties"])
    assert IntentDecision(intent="chat", summary="讨论").style_source_ids == []
    for targets in [["wrong"], ["a" * 31], [A] * 201]:
        with pytest.raises(ValidationError):
            decision(targets)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("分析银行范文.docx的风格", [A]),
        ("分析《银行范文.docx》的风格", [A]),
        ("看看第二个文件的排版", [B]),
        ("分析第2个示例的风格", [B]),
        ("对银行范文.docx和合同范文.pdf提取风格", [A, B]),
        ("分析所有范文的风格", []),
        ("提取写法和风格说明", []),
    ],
)
def test_target_resolution_uses_uploaded_names_order_or_explicit_all(message, expected):
    result = validate_style_intent(decision(), context(message))
    assert result.intent == "analyze_style" and result.style_source_ids == expected


def test_history_reference_uses_valid_model_selection_without_guessing_from_pronouns():
    workflow = context("分析这个文件的风格")
    workflow["messages"] = [{"role": "user", "content": "我想先看合同范文.pdf"}]
    assert validate_style_intent(decision([B]), workflow).style_source_ids == [B]
    assert validate_style_intent(decision(), workflow).style_source_ids == []


@pytest.mark.parametrize(
    "message",
    [
        "不要分析银行范文.docx，分析合同范文.pdf的风格",
        "这份报告第一个示例不需要，看看合同范文.pdf",
        "不是业务依据.md，要看合同范文.pdf风格",
        "不存在.docx不是我说的，实际分析合同范文.pdf",
    ],
)
def test_valid_semantic_selection_precedes_literal_name_and_ordinal_fallback(message):
    state = context(message)
    assert validate_style_intent(decision([B]), state).style_source_ids == [B]
    questions, build = DecisionRouter.questions("intent", state)
    assert "style_target" in questions
    result = build(
        {
            "intent": {"choice": "analyze_style", "confidence": 0.99},
            "style_target": {"choice": B, "confidence": 0.99},
        }
    )
    assert result["intent"] == "analyze_style" and result["style_source_ids"] == [B]


@pytest.mark.parametrize(
    "message,targets",
    [
        ("分析不存在.docx的风格", []),
        ("分析第三份文件的风格", []),
        ("分析第九个示例的风格", []),
        ("分析业务依据.md的写法", []),
        ("提取风格说明", [C]),
        ("提取风格说明", ["d" * 32]),
        ("分析银行范文.docx的风格", [A, A]),
    ],
)
def test_invalid_or_mismatched_targets_clarify_without_changing_roles(message, targets):
    state = context(message)
    before = json.dumps(state, ensure_ascii=False)
    result = validate_style_intent(decision(targets), state)
    assert result.intent == "clarify" and result.style_source_ids == []
    assert result.summary.startswith("风格分析：")
    assert json.dumps(state, ensure_ascii=False) == before
    assert validate_style_intent(result, state) == result


def test_no_style_inventory_keeps_generic_request_for_workspace_guidance():
    result = validate_style_intent(decision(), context("请提取风格说明", files=[]))
    assert result.intent == "analyze_style" and result.style_source_ids == []


def test_large_explicit_target_lists_have_room_for_ids_but_generic_intents_keep_small_budget():
    files = [
        {"id": f"{number:032x}", "name": f"参考范文{number:03}.docx", "role": "style_reference"}
        for number in range(200)
    ]
    state = context("分析" + "、".join(item["name"] for item in files) + "的风格", files)
    assert style_selection_output_tokens(state) == 6912
    state["latest_user_message"] = "提取风格说明"
    assert style_selection_output_tokens(state) == 1024


def test_duplicate_filenames_do_not_silently_choose_one_upload():
    files = [
        {"id": identity, "name": "同名.docx", "role": "style_reference"} for identity in (A, B)
    ]
    assert (
        validate_style_intent(decision(), context("分析同名.docx风格", files)).intent == "clarify"
    )
    assert validate_style_intent(
        decision(), context("分析第2份文件的风格", files)
    ).style_source_ids == [B]


@pytest.mark.parametrize(
    "intent,message",
    [
        ("analyze", "分析业务依据的审批流程"),
        ("generate", "按范文风格生成需求说明书"),
        ("requirements", "以后正文改成范文的字号，先不生成"),
        ("chat", "不要分析示例，先讨论下一步"),
        ("chat", "如果上传范文，可以提取风格吗"),
        ("chat", "风格提取是什么，如何提取"),
    ],
)
def test_ordinary_intents_do_not_consume_style_selection_answers(intent, message):
    questions, build = DecisionRouter.questions("intent", context(message))
    assert set(questions) == {"intent", "style_target"}
    result = build({"intent": {"choice": intent, "confidence": 0.98}})
    assert result["intent"] == intent and result["style_source_ids"] == []
    assert (
        validate_style_intent(decision([A], intent=intent), context(message)).style_source_ids == []
    )


@pytest.mark.parametrize("model", ["jev", "luna-decisions"])
async def test_jev_and_luna_route_intent_and_target_in_one_request(monkeypatch, model):
    config = {
        "provider_mode": "airouter",
        "base_url": "http://decision.test/v1",
        "api_key": "test",
        "decision_model": model,
        "decision_enabled": True,
    }
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    calls = []

    async def evaluate(config, state, questions):
        calls.append(questions)
        assert set(questions) == {"intent", "style_target"}
        assert all(question["type"] == "choice" for question in questions.values())
        assert C not in questions["style_target"]["criteria"]
        return {
            "answers": {
                "intent": {"choice": "analyze_style", "confidence": 0.99},
                "style_target": {"choice": B, "confidence": 0.99},
            },
            "model": model,
        }

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    result, fallback = await DecisionRouter(config).try_complete(
        "intent", context("看看合同范文.pdf的排版"), IntentDecision.model_json_schema()
    )
    assert fallback is None and len(calls) == 1
    parsed = IntentDecision.model_validate_json(result.content)
    assert parsed.intent == "analyze_style" and parsed.style_source_ids == [B]


def test_decision_provider_supports_named_multiple_selection_without_extra_questions():
    state = context("分析银行范文.docx和合同范文.pdf的写法")
    questions, build = DecisionRouter.questions("intent", state)
    assert len(questions) == 2
    assert "specified" in questions["style_target"]["criteria"]
    assert build(
        {
            "intent": {"choice": "analyze_style", "confidence": 0.99},
            "style_target": {"choice": "specified", "confidence": 0.99},
        }
    )["style_source_ids"] == [A, B]


def test_unknown_style_clarification_prefix_is_not_trusted_model_prose():
    result = validate_style_intent(
        IntentDecision(intent="clarify", summary="风格分析：任意模型输出"), context("分析风格")
    )
    assert not result.summary.startswith("风格分析：")


def test_style_request_is_discussion_and_keeps_frozen_directory_requirements():
    state = {
        "messages": [{"role": "user", "content": "编写一期需求说明书"}],
        "directory_review": {"revision": "keep", "confirmable": True},
    }
    original = sync_requirements(state)
    message = {"role": "user", "content": "分析这个示例的排版风格", "action": "chat"}
    state["messages"].append(message)
    mark_requirement(message, intent="analyze_style")
    assert message["requirement"]["status"] == "discussion"
    assert sync_requirements(state) == original
    assert state["directory_review"] == {"revision": "keep", "confirmable": True}


def test_visible_intent_contract_defines_style_boundaries_and_three_field_output():
    rendered = prompt_for("intent", context("提取范文写法"), IntentDecision.model_json_schema())
    assert "style_source_ids" in rendered
    assert "exactly seven categories" in INSTRUCTIONS["intent"]
    for phrase in [
        "role=style_reference",
        "Negated execution",
        "Generate a requirements specification in the sample's style",
        "Use the sample's font size for future body text",
        "a general question",
        "style_analysis_available",
    ]:
        assert phrase in INSTRUCTIONS["intent"]
