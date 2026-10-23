"""Source text is readable, auditable, and counted exactly as sent."""

import hashlib
import json
import re
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from native_calls import native_call

from folio.research.contracts import ResearchStep
from folio.research.native_tools import native_result, tool_definitions
from folio.research.planning import PlanningResearchStep
from folio.research.transport import (
    research_messages,
    research_prompt_chars,
    research_text,
)
from folio.runtime.base import RuntimeResult, prompt_chars, prompt_for
from folio.runtime.capacity import Capacity
from folio.runtime.image_inputs import text_characters
from folio.runtime.language import LANGUAGE_INSTRUCTION
from folio.runtime.llm import LLMRuntime


def runtime(mode="none", provider="custom"):
    instance = object.__new__(LLMRuntime)
    instance.config = {"structured_output": mode, "provider_mode": provider, "model": "gpt-sol"}
    instance.model = "gpt-sol"
    instance.capacity = Capacity(instance)
    return instance


def test_source_text_is_sent_once_without_raw_json_wrapper_or_finish_contract():
    original = '[E1] 原文：必须记录审批日志。\n第二段包含 "引号" 和反斜杠 \\。'
    context = {
        "requirements": "生成需求说明书",
        "manifest": {"items": [{"path": "internal-long-path", "version": "version-secret"}]},
        "manifest_text": "F1：业务要求.docx",
        "finish_contract": {"properties": {"irrelevantSchema": {"type": "string"}}},
        "observations": [{
            "request_id": "004", "tool": "fs.read", "model_text": original,
            "result": {"evidence": [{"text": original, "metadata": {"padding": "x" * 50000}}]},
        }],
        "read_progress": [{"range_id": "long-internal-range"}],
        "read_progress_text": "R1：已发送第 1～2 段；尚未读完",
        "history": {"raw": "never-send"},
        "history_text": "004：读取 F1；可按编号取回",
    }
    source = deepcopy(context)
    text = research_text(context)
    assert context == source
    assert original in text
    assert text.count(original) == 1
    assert "F1：业务要求.docx" in text
    assert "R1：已发送第 1～2 段；尚未读完" in text
    assert "004：读取 F1；可按编号取回" in text
    for hidden in ("padding", "version-secret", "internal-long-path", "irrelevantSchema", "never-send", "long-internal-range"):
        assert hidden not in text
    assert len(text) < 500


def test_all_native_tools_use_non_strict_function_schemas():
    context = {"requirements": "核对业务范围"}
    schema = PlanningResearchStep.model_json_schema()
    tools = tool_definitions("directory_research", context, schema)

    assert tools
    assert all(tool["function"]["strict"] is False for tool in tools)


def test_template_constraints_and_complete_multiline_user_text_remain_visible():
    request = "写第一期需求。\n必须包含审批和审计。"
    text = research_text({
        "requirements": request,
        "template": {
            "name": "需求模板.docx", "version": "hash-not-needed", "sections": [{
                "section_id": "T1", "title": "业务需求", "level": 1, "allow_children": False,
                "instruction": "按原表填写", "tables": [{"table_id": "TBL1", "headers": ["编号", "需求"],
                    "min_data_rows": 2, "header_rows": 1, "body_index": 123}],
            }],
            "rules": ["保留固定章节，不可改名。"],
        },
    })
    for expected in (request, "T1 业务需求", "Child sections allowed: no", "按原表填写", "编号 | 需求", "minimum data rows: 2", "保留固定章节，不可改名。"):
        assert expected in text
    assert "hash-not-needed" not in text
    assert "body_index" not in text


def test_legacy_evidence_fallback_preserves_quotes_references_and_page_progress():
    text = research_text({"observations": [{"tool": "fs.read", "request_id": "r1", "result": {
        "evidence": [{"evidence_id": "ev_1", "text": "必须在撤单后释放库存。", "metadata": {"source_hash": "internal"}}],
        "range_id": "range_1", "next_offset": 5, "source_complete": False,
        "warnings": ["文本框未提取"],
    }}]})
    assert "[ev_1] 必须在撤单后释放库存。" in text
    assert "range_1" not in text and "Continue reading: offset=5" in text
    assert "文本框未提取" in text and "source_complete：no" in text
    assert "source_hash" not in text


@pytest.mark.parametrize("task,model", [("directory_research", PlanningResearchStep), ("research_step", ResearchStep)])
@pytest.mark.parametrize("mode", ["none", "json_schema"])
def test_native_messages_and_tools_are_counted_as_sent(task, model, mode):
    call = native_call("fs_read", {"file": "F1", "summary": "读取要求"}).tool_calls[0]
    context = {"requirements": "检查业务范围", "tools": [{"name": "obsolete_tool"}],
               "observations": [{"tool_call": call, "text": "[E1] 原文\n下一行"}]}
    schema = model.model_json_schema()
    instance = runtime(mode)
    messages, _, *_ = instance.request_parts(task, context, schema)
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    assert messages[2]["tool_calls"] == [call]
    assert messages[3]["tool_call_id"] == call["id"]
    assert "[E1] 原文\n下一行" in messages[3]["content"]
    assert "[E1]" not in messages[1]["content"]
    assert "obsolete_tool" not in messages[1]["content"]
    assert "JSON Schema:" not in messages[0]["content"]
    expected = text_characters(messages) + len(json.dumps(tool_definitions(task, context, schema), ensure_ascii=False, separators=(",", ":")))
    assert research_prompt_chars(context, schema, task=task) == expected
    assert prompt_chars(object(), task, context, schema) == expected
    assert instance.prompt_chars(task, context, schema) == expected
    assert "原文" in prompt_for(task, context, schema)


def test_tool_schemas_are_individual_and_disallow_unknown_parameters():
    for task, model in (("directory_research", PlanningResearchStep), ("research_step", ResearchStep)):
        tools = tool_definitions(task, {}, model.model_json_schema())
        for tool in tools:
            function = tool["function"]
            assert "." not in function["name"]
            Draft202012Validator.check_schema(function["parameters"])
            assert function["parameters"]["additionalProperties"] is False
        read = next(tool["function"]["parameters"] for tool in tools if tool["function"]["name"] == "fs_read")
        validator = Draft202012Validator(read)
        validator.validate({"file": "F1", "node": "N005", "summary": "读取业务要求"})
        assert not validator.is_valid({"file": "F1", "summary": "读取", "shell": "arbitrary"})
        assert not validator.is_valid({"file": "F1", "summary": "读取", "page": "1-15"})


def test_only_native_function_is_executed_and_complete_arguments_are_required():
    schema = PlanningResearchStep.model_json_schema()
    response = native_call("fs_read", {"file": "F1", "summary": "读取"})
    response.content = '{"tool":"finish","arguments":{}}'
    batch = native_result("directory_research", response, {}, schema)
    assert batch["assistant_content"] == response.content
    parsed = batch["steps"][0]
    assert parsed["tool"] == "fs.read"
    assert parsed["arguments"] == {"file": "F1"}
    assert parsed["tool_call"] == response.tool_calls[0]
    with pytest.raises(ValueError, match="native tool_call"):
        native_result("directory_research", RuntimeResult(response.content), {}, schema)
    response.tool_calls[0]["function"]["arguments"] += '{"file":"F2"}'
    with pytest.raises(ValueError):
        native_result("directory_research", response, {}, schema)


def test_directory_finish_preserves_full_summary_and_validates_evidence():
    schema = PlanningResearchStep.model_json_schema()
    conclusion = {"summary": "充分说明" * 800, "requirements": [{"requirement_id": "Q1", "text": "需" * 3500, "evidence_ids": ["E1"]}]}
    parsed = native_result("directory_research", native_call("finish", conclusion), {}, schema)["steps"][0]
    assert parsed["arguments"] == conclusion
    PlanningResearchStep.model_validate(parsed)
    conclusion["requirements"][0]["text"] += "求"
    with pytest.raises(ValueError, match="too long"):
        native_result("directory_research", native_call("finish", conclusion), {}, schema)
    conclusion["requirements"][0]["text"] = "覆盖审批"
    conclusion["requirements"][0]["evidence_ids"] = []
    with pytest.raises(ValueError):
        native_result("directory_research", native_call("finish", conclusion), {}, schema)


def test_research_gap_limit_matches_native_and_saved_step_validation():
    schema = ResearchStep.model_json_schema()
    gap = "缺" * 3500
    arguments = {"file": "F1", "summary": "核对缺失信息", "missing_information": [gap]}
    parsed = native_result(
        "research_step", native_call("fs_read", arguments), {}, schema,
    )["steps"][0]
    assert ResearchStep.model_validate(parsed).missing_information == [gap]

    arguments["missing_information"] = [gap + "失"]
    with pytest.raises(ValueError, match="too long"):
        native_result("research_step", native_call("fs_read", arguments), {}, schema)
    with pytest.raises(ValueError, match="Research gap exceeds"):
        ResearchStep.model_validate(parsed | {"missing_information": arguments["missing_information"]})


@pytest.mark.parametrize("tool", ["fs_read", "finish"])
@pytest.mark.parametrize(("task", "model", "limit"), [
    ("directory_research", PlanningResearchStep, 3500),
    ("research_step", ResearchStep, 1000),
])
def test_review_reason_limit_matches_native_and_saved_step_validation(tool, task, model, limit):
    schema = model.model_json_schema()
    reason = "核" * limit
    arguments = {"summary": "复核原文", "review_reason": reason}
    if tool == "fs_read":
        arguments["file"] = "F1"
    parsed = native_result(task, native_call(tool, arguments), {}, schema)["steps"][0]
    assert model.model_validate(parsed).review_reason == reason

    arguments["review_reason"] += "对"
    with pytest.raises(ValueError, match="too long"):
        native_result(task, native_call(tool, arguments), {}, schema)
    with pytest.raises(ValueError, match="at most"):
        model.model_validate(parsed | {"review_reason": arguments["review_reason"]})


@pytest.mark.parametrize(("serialized", "expected"), [
    ('["读取合同条款", "补齐功能清单"]', ["读取合同条款", "补齐功能清单"]),
    ("[]", []),
    ("null", None),
])
def test_directory_finish_normalizes_stringified_local_research_actions(
    serialized, expected,
):
    args = {"summary": "研究结论", "local_research_actions": serialized}
    response = native_call("finish", args)
    original = deepcopy(response.tool_calls[0])

    parsed = native_result(
        "directory_research", response, {}, PlanningResearchStep.model_json_schema(),
    )["steps"][0]

    assert parsed["arguments"]["local_research_actions"] == expected
    assert parsed["tool_call"] == original == response.tool_calls[0]
    assert json.loads(original["function"]["arguments"]) == args


@pytest.mark.parametrize("serialized", ["not-json", '{"unexpected": true}', '["valid", 1]'])
def test_directory_finish_still_rejects_other_invalid_local_research_actions(serialized):
    args = {"summary": "研究结论", "local_research_actions": serialized}
    with pytest.raises(ValueError, match="Invalid arguments for finish"):
        native_result(
            "directory_research", native_call("finish", args), {},
            PlanningResearchStep.model_json_schema(),
        )


@pytest.mark.parametrize("task", ["directory_research", "research_step"])
def test_model_cites_evidence_without_bookkeeping_fields(task):
    functions = {item["function"]["name"]: item["function"]
                 for item in tool_definitions(task, {}, {})}
    serialized = json.dumps(functions, ensure_ascii=False)
    assert "request_ids" not in serialized
    assert "selected_ranges" not in serialized
    note = {"topic": "审批", "text": "审批后记录操作日志。", "kind": "source_statement",
            "evidence_ids": ["E1"]}
    params = functions["research_save"]["parameters"]
    Draft202012Validator(params).validate({"summary": "保存发现", "research_notes": [note]})
    assert not Draft202012Validator(params).is_valid({
        "summary": "保存发现", "research_notes": [note | {"unknown": "not accepted"}],
    })


@pytest.mark.parametrize("task", ["directory_research", "research_step"])
def test_legacy_bookkeeping_is_ignored_but_native_call_remains_auditable(task):
    note = {"topic": "审批", "text": "审批后记录操作日志。", "kind": "source_statement",
            "evidence_ids": ["E1"], "request_ids": ["wrong-old-batch"]}
    args = {"summary": "提交研究结果", "research_notes": [note]}
    if task == "directory_research":
        args |= {"requirements": [{"requirement_id": "req1", "text": "包含审批",
                                   "evidence_ids": ["E1"]}],
                 "selected_ranges": "malformed obsolete ranges"}
    response = native_call("finish", args)
    original = deepcopy(response.tool_calls[0])
    parsed = native_result(task, response, {}, {})["steps"][0]
    assert "request_ids" not in parsed["research_notes"][0]
    assert "selected_ranges" not in parsed["arguments"]
    assert parsed["tool_call"] == original == response.tool_calls[0]
    assert json.loads(original["function"]["arguments"]) == args
    with pytest.raises(ValueError, match="Invalid arguments"):
        native_result(task, native_call("finish", args | {"unknown": True}), {}, {})


def test_research_text_hides_internal_batches_but_retains_evidence_and_progress():
    text = research_text({
        "memory_checkpoint_required": True,
        "memory_checkpoint_note": "请归纳下方原文中与任务相关的发现。",
        "memory_checkpoint_request_ids": ["internal-checkpoint"],
        "memory_pending_requests": ["internal-pending"],
        "memory_delivered_requests": ["internal-delivered"],
        "pending_research_batches": ["internal-batches"],
        "observations": [{"request_id": "internal-observation", "tool": "fs.read",
                          "model_text": "[E1] 审批后记录操作日志。"}],
    })
    assert "internal-" not in text
    assert "[E1] 审批后记录操作日志。" in text
    assert "请归纳下方原文" in text


def test_native_batch_validates_all_calls_before_returning_and_rejects_duplicate_ids():
    calls = [native_call("fs_read", {"file": f"F{index}", "summary": "读取"}, call_id=f"call_{index}").tool_calls[0]
             for index in (1, 2)]
    result = RuntimeResult("读取两份资料", tool_calls=calls)
    parsed = native_result("directory_research", result, {}, {})
    assert [step["arguments"]["file"] for step in parsed["steps"]] == ["F1", "F2"]
    assert parsed["assistant_content"] == result.content
    calls[1]["function"]["arguments"] = '{"file":"F2","summary":"读取","unknown":true}'
    with pytest.raises(ValueError, match="Invalid arguments"):
        native_result("directory_research", result, {}, {})
    calls[1]["id"] = calls[0]["id"]
    with pytest.raises(ValueError, match="Duplicate"):
        native_result("directory_research", result, {}, {})


def test_single_decision_task_rejects_multiple_calls():
    calls = [native_call("search_sources", {"query": "要求"}).tool_calls[0] for _ in range(2)]
    with pytest.raises(ValueError, match="exactly one"):
        native_result("query", RuntimeResult("", tool_calls=calls), {}, {"type": "object"})


@pytest.mark.parametrize("trim_first", [False, True])
def test_batch_history_groups_assistant_calls_and_pairs_results_by_id(trim_first):
    calls = [native_call("fs_read", {"file": f"F{index}", "summary": "读取"}, call_id=f"call_{index}").tool_calls[0]
             for index in (1, 2)]
    observations = [{"batch_id": "research_001", "batch_tool_calls": calls,
                     "assistant_content": "读取两份资料", "tool_call": call,
                     "model_text": f"第{index}份原文"}
                    for index, call in enumerate(calls, 1)]
    # Execution completion order must not change the correspondence or protocol.
    messages = research_messages({"observations": observations[1:] if trim_first else observations[::-1]}, {})
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "tool"]
    assert messages[2] == {"role": "assistant", "content": "读取两份资料", "tool_calls": calls}
    assert [message["tool_call_id"] for message in messages[3:]] == ["call_1", "call_2"]
    assert "第2份原文" in messages[4]["content"]
    if trim_first:
        assert "archived" in messages[3]["content"]
        assert "第1份原文" not in messages[3]["content"]
    else:
        assert "第1份原文" in messages[3]["content"]


def test_business_functions_cover_execution_registration_and_finish():
    schema = ResearchStep.model_json_schema()
    for name, args, expected in [
        ("sandbox_exec", {"operation": "python", "code": "print(1)"}, "sandbox.exec"),
        ("artifact_save", {"operation": "coverage.resolve", "coverage_id": "cov1", "resolutions": [{"item_id": "i1", "status": "covered", "reason": "正文要求", "references": [{"section_id": "s1", "block_id": "b1"}]}]}, "artifact.save"),
        ("finish", {}, "finish"),
    ]:
        parsed = native_result("research_step", native_call(name, args | {"summary": "核对结果"}), {}, schema)["steps"][0]
        assert parsed["tool"] == expected
        assert parsed["arguments"] == args
        ResearchStep.model_validate(parsed)


def test_coverage_navigation_is_sent_once_and_legacy_pages_remain_readable():
    from folio.research.coverage_obligations import CoverageObligations

    call = native_call("inspect_coverage_sources", {
        "reason": "需要核对完整范围", "catalog_requests": [{"offset": 20}],
    }).tool_calls[0]
    legacy = {"catalog": [{"source_id": "old-source-only"}], "offset": 0}
    current = {"catalog": [{"source_id": "new-source-only"}], "offset": 20}
    context = {"structure_pages": [legacy, current], "observations": [{
        "tool_call": call, "result": {"pages": [current]},
        "model_text": json.dumps(current, ensure_ascii=False),
    }]}
    messages = research_messages(context, CoverageObligations.model_json_schema(), task="coverage_obligations")
    assert "old-source-only" in messages[1]["content"]
    assert "new-source-only" not in messages[1]["content"]
    assert "new-source-only" in messages[-1]["content"]


def test_research_image_request_keeps_readable_text_and_attaches_verified_image(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-payload")
    context = {
        "sections": [{"section_id": "S1", "title": "流程"}],
        "observations": [{"model_text": "[E1] 退回后由申请人修改。", "result": {"metadata": "do-not-send"}}],
        "page_images": [{"path": str(image), "page": 2, "source_id": "F1", "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}],
    }
    wire_runtime = runtime()
    wire_runtime.config["supports_image_input"] = True
    schema = ResearchStep.model_json_schema()
    messages, *_ = wire_runtime.request_parts("research_step", context, schema)
    parts = messages[1]["content"]
    assert parts[0]["text"] == research_text(context)
    assert "do-not-send" not in parts[0]["text"]
    assert str(image) not in parts[0]["text"]
    assert parts[1]["text"] == "Source F1, page 2; region: full page"
    assert parts[2]["type"] == "image_url"
    assert wire_runtime.prompt_chars("research_step", context, schema) == text_characters(messages) + len(json.dumps(tool_definitions("research_step", context, schema), ensure_ascii=False, separators=(",", ":")))


@pytest.mark.asyncio
async def test_native_stream_has_tools_and_no_response_format_even_in_strict_output_mode():
    wire_runtime = runtime("json_schema", "airouter")
    wire_runtime.config |= {"api_key": "", "max_tokens": 1000, "temperature": 0.1}
    context = {"requirements": "核对要求"}
    schema = PlanningResearchStep.model_json_schema()
    messages, *_ = wire_runtime.request_parts("directory_research", context, schema)
    tools = tool_definitions("directory_research", context, schema)
    captured = []

    async def stream_once(payload, headers):
        captured.append(payload)
        yield "completed", {"text": "", "usage": {}, "tool_calls": []}

    wire_runtime._stream_once = stream_once
    assert [event async for event in wire_runtime.stream(messages, schema=schema, tools=tools)]
    assert captured[0]["messages"] == messages
    assert captured[0]["tools"] == tools
    assert captured[0]["tool_choice"] == "required"
    assert captured[0]["parallel_tool_calls"] is False
    assert "response_format" not in captured[0]


@pytest.mark.asyncio
async def test_capacity_tokenizer_receives_native_tools_and_history():
    import httpx

    wire_runtime = runtime("json_schema", "airouter")
    wire_runtime.config |= {"api_key": "", "max_tokens": 1000, "base_url": "http://unused.test/v1"}
    capacity = Capacity(wire_runtime)
    capacity.ready, capacity.counter, capacity.window = True, True, 50000
    context, schema = {"requirements": "核对业务范围"}, PlanningResearchStep.model_json_schema()
    captured = []

    async def count_request(*args, **kwargs):
        captured.append(kwargs["json"])
        return httpx.Response(200, json={"count": 10})

    capacity.request = count_request
    measured = await capacity.measure("directory_research", context, schema, 48000)
    assert captured[0]["tools"] == tool_definitions("directory_research", context, schema)
    assert measured["characters"] == wire_runtime.prompt_chars("directory_research", context, schema)
    assert measured["input_tokens"] == 10
    assert measured["fits"] is True


@pytest.mark.parametrize("task", ["directory_research", "research_step"])
@pytest.mark.parametrize("user_input", [
    "请根据英文资料写中文报告。",
    "Write an English report from the uploaded Chinese reference.",
    "请根据资料生成英文报告。",
])
def test_research_instructions_are_english_and_preserve_original_language_reference(task, user_input):
    original = "[E1] 原文必须逐字保留。"
    context = {"user_request": user_input, "observations": [{"model_text": original}]}
    before = deepcopy(context)
    messages = research_messages(context, {}, task=task)
    system = messages[0]["content"]
    assert not re.search(r"[\u3400-\u9fff]", system)
    assert system.count(LANGUAGE_INSTRUCTION) == 1
    assert "If the user explicitly requests a different output language" in system
    assert user_input in messages[1]["content"]
    assert original in messages[1]["content"]
    assert context == before
    functions = json.dumps(tool_definitions(task, {}, {}), ensure_ascii=False)
    assert not re.search(r"[\u3400-\u9fff]", functions)
