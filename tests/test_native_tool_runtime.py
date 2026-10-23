import hashlib
import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.activity import model_activity
from folio.runtime.diagnostics import partial_output_for_audit, safe_failure_diagnostics
from folio.runtime.errors import ModelCallFailed
from folio.runtime.image_inputs import image_messages, text_characters
from folio.runtime.llm import LLMRuntime
from folio.runtime.tool_calls import MAX_TOOL_CALLS_PER_BATCH, native_retry_context, tool_characters

TOOLS = [{
    "type": "function",
    "function": {
        "name": "fs_read",
        "description": "Read selected source text.",
        "parameters": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
            "additionalProperties": False,
        },
        "strict": False,
    },
}]


def response(chunks):
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
    )


def call_delta(**overrides):
    return {"index": 0, "id": "call_1", "type": "function", "function": {"name": "fs_read", "arguments": '{"file":"F1"}'}} | overrides


async def runtime_for(tmp_path, handler):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(
        base_url="http://example.test/v1", model="local", no_auth=True,
        structured_output="json_schema",
    ))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return runtime


async def test_native_stream_assembles_arguments_and_preserves_call_id(tmp_path):
    requests, activity = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        return response([
            {"choices": [{"delta": {"content": "我将读取原文。", "reasoning_content": "private"}}]},
            {"choices": [{"delta": {"tool_calls": [call_delta(function={"name": "fs_read", "arguments": '{"file":'})]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"F1"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}},
        ])

    runtime = await runtime_for(tmp_path, handler)
    try:
        with model_activity(activity.append):
            events = [event async for event in runtime.stream(
                [{"role": "user", "content": "读取 F1"}], tools=TOOLS, schema={"type": "object"}
            )]
        completed = events[-1][1]
        assert completed["tool_calls"] == [{key: value for key, value in call_delta().items() if key != "index"}]
        assert completed["text"] == "我将读取原文。"
        assert completed["usage"]["total_tokens"] == 40
        assert requests[0]["tools"] == TOOLS
        assert requests[0]["tool_choice"] == "required"
        assert requests[0]["parallel_tool_calls"] is False
        assert "response_format" not in requests[0]
        assert completed["reasoning_content"] == "private"
        assert "private" not in json.dumps([event for event in events if event[0] != "completed"] + activity)
        assert any(item["status"] == "responding" for item in activity)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("parallel_requested", [False, True])
async def test_native_stream_collects_interleaved_calls_and_late_usage(tmp_path, parallel_requested):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response([
            {"choices": [{"delta": {"tool_calls": [
                call_delta(index=1, id="call_B", function={"name": "fs_", "arguments": '{"file":'}),
                call_delta(index=0, id="call_A", function={"name": "fs_read", "arguments": '{"file":'}),
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"F1"}'}},
                {"index": 1, "function": {"name": "read", "arguments": '"F2"}'}},
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"total_tokens": 42}},
        ])

    runtime = await runtime_for(tmp_path, handler)
    try:
        events = [event async for event in runtime.stream(
            [{"role": "user", "content": "read"}], tools=TOOLS,
            parallel_tool_calls=parallel_requested,
        )]
        completed = events[-1][1]
        assert [call["id"] for call in completed["tool_calls"]] == ["call_A", "call_B"]
        assert [json.loads(call["function"]["arguments"])["file"]
                for call in completed["tool_calls"]] == ["F1", "F2"]
        assert completed["usage"] == {"total_tokens": 42}
        assert requests[0]["parallel_tool_calls"] is parallel_requested
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("case", [
    "text_json", "missing_finish", "wrong_finish", "length", "duplicate_ids", "unknown_tool",
    "missing_id", "invalid_arguments", "multiple_json", "array_arguments", "bad_index", "legacy_call",
    "after_finish", "sparse_index", "batch_limit", "missing_index",
])
async def test_native_stream_rejects_ambiguous_or_incomplete_actions(tmp_path, case):
    call = call_delta()
    finish = "tool_calls"
    delta = {"tool_calls": [call]}
    if case == "text_json":
        delta = {"content": '{"tool":"fs.read","arguments":{"file":"F1"}}'}
        finish = "stop"
    elif case == "wrong_finish":
        finish = "stop"
    elif case == "length":
        finish = "length"
    elif case == "duplicate_ids":
        delta["tool_calls"].append(call_delta(index=1))
    elif case == "sparse_index":
        delta["tool_calls"].append(call_delta(index=2, id="call_2"))
    elif case == "batch_limit":
        delta["tool_calls"].append(call_delta(index=MAX_TOOL_CALLS_PER_BATCH, id="call_2"))
    elif case == "missing_index":
        del call["index"]
    elif case == "unknown_tool":
        call["function"]["name"] = "delete_all"
    elif case == "missing_id":
        del call["id"]
    elif case in {"invalid_arguments", "multiple_json", "array_arguments"}:
        call["function"]["arguments"] = {"invalid_arguments": '{"file":', "multiple_json": '{} {}', "array_arguments": '[]'}[case]
    elif case == "bad_index":
        call["index"] = -1
    elif case == "legacy_call":
        delta = {"function_call": call["function"]}
    chunks = [{"choices": [{"delta": delta}]}]
    if case != "missing_finish":
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    if case == "after_finish":
        chunks.append({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " "}}]}}]})
    runtime = await runtime_for(tmp_path, lambda request: response(chunks))
    try:
        with pytest.raises(ModelCallFailed) as failure:
            _ = [event async for event in runtime.stream([{"role": "user", "content": "read"}], tools=TOOLS)]
        assert failure.value.code == ("length_limit" if case == "length" else "protocol")
        expected = {
            "text_json": "tool_finish_invalid", "missing_finish": "tool_finish_invalid",
            "wrong_finish": "tool_finish_invalid", "length": "tool_finish_invalid",
            "duplicate_ids": "tool_id_duplicate", "unknown_tool": "tool_unknown",
            "missing_id": "tool_id_missing", "invalid_arguments": "tool_arguments_json",
            "multiple_json": "tool_arguments_json", "array_arguments": "tool_arguments_type",
            "bad_index": "tool_index_invalid", "legacy_call": "legacy_function_call",
            "after_finish": "tool_after_finish",
            "sparse_index": "tool_index_sparse", "batch_limit": "tool_batch_limit",
            "missing_index": "tool_index_missing",
        }
        safe = safe_failure_diagnostics(failure.value)
        assert safe["protocol_issue"] == expected[case]
        assert safe["status_code"] == 200
        if case == "text_json":
            retry = native_retry_context("research_step", {}, error=failure.value)
            assert retry["native_retry_response"] == delta["content"]
            assert retry["observations"] == []
            assert "No tool was executed" in retry["format_feedback"]
        if case == "missing_finish":
            assert safe["finish_reason"] == "missing"
            assert safe["done_received"] is True
        if case == "duplicate_ids":
            assert safe["failed_tool_index"] == 1
            assert safe["tool_calls_received"] == 2
            assert safe["done_received"] is True
            private = json.loads(partial_output_for_audit(failure.value)["content_head"])
            assert private["recent_tool_deltas"][-1][1]["id"] == "call_1"
            assert len(private["tool_calls"]) == 2
    finally:
        await runtime.aclose()


async def test_complete_native_task_keeps_text_as_text_and_requires_tool_calls(tmp_path, monkeypatch):
    runtime = await runtime_for(tmp_path, lambda request: response([]))
    monkeypatch.setattr(runtime, "request_parts", lambda *args: ([{"role": "user", "content": "read"}], {}, {}, None, {}))
    monkeypatch.setattr(runtime, "request_tools", lambda *args: TOOLS)

    async def forbidden_router(*args):
        raise AssertionError("Native tasks must bypass classification services")

    monkeypatch.setattr(runtime.decision_router, "try_complete", forbidden_router)
    calls = [{key: value for key, value in call_delta().items() if key != "index"}]

    async def native_stream(*args, **kwargs):
        assert kwargs["tools"] == TOOLS
        assert kwargs["parallel_tool_calls"] is True
        yield "text.delta", {"text": '{"old":"action"}' + " " * 512}
        yield "completed", {"text": '{"old":"action"}' + " " * 512, "usage": {"total_tokens": 12}, "tool_calls": calls}

    monkeypatch.setattr(runtime, "stream", native_stream)
    try:
        result = await runtime.complete("research_step", {}, {}, "native")
        assert result.tool_calls == calls
        assert result.content.endswith(" " * 512)
        assert result.usage == {"total_tokens": 12}
    finally:
        await runtime.aclose()


async def test_capacity_counts_native_tools_and_history(tmp_path, monkeypatch):
    requests = []
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
        {"role": "assistant", "content": None, "tool_calls": [{key: value for key, value in call_delta().items() if key != "index"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "source"},
    ]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "local", "max_model_len": 922000}]})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"count": 200})

    runtime = await runtime_for(tmp_path, handler)
    monkeypatch.setattr(runtime, "request_parts", lambda *args: (messages, {}, {}, None, {}))
    monkeypatch.setattr(runtime, "request_tools", lambda *args: TOOLS)
    try:
        measured = await runtime.measure("research_step", {}, {}, 100000)
        assert requests[0]["tools"] == TOOLS
        assert requests[0]["messages"] == messages
        assert requests[0]["parallel_tool_calls"] is True
        assert measured["input_tokens"] == 200
        assert measured["tool_characters"] == tool_characters(TOOLS)
        assert measured["characters"] == text_characters(messages) + tool_characters(TOOLS)
        assert runtime.prompt_chars("research_step", {}, {}) == measured["characters"]
        assert measured["fits"]
    finally:
        await runtime.aclose()


async def test_image_request_keeps_native_history_and_tool_budget(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    context = {"page_images": [{
        "path": str(page), "sha256": hashlib.sha256(page.read_bytes()).hexdigest(), "page": 1,
    }]}
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{key: value for key, value in call_delta().items() if key != "index"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "source"},
    ]
    messages = image_messages(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "source question"}, *history],
        context, supported=True, preserve_user_text=True,
    )
    assert messages[2:] == history
    assert messages[1]["content"][0]["text"] == "source question"
    runtime = await runtime_for(tmp_path, lambda request: response([]))
    monkeypatch.setattr(runtime, "request_parts", lambda *args: (messages, {}, {}, None, {}))
    monkeypatch.setattr(runtime, "request_tools", lambda *args: TOOLS)
    try:
        measured = await runtime.measure("research_step", context, {}, 100000)
        assert measured["characters"] == text_characters(messages) + tool_characters(TOOLS)
        assert measured["tool_characters"] == tool_characters(TOOLS)
        assert measured["measurement"] == "default_tokenizer_estimate"
        assert measured["input_tokens"] is None and measured["fits"]
        from folio.runtime.image_inputs import IMAGE_TOKEN_RESERVE

        without_image = [dict(message) for message in messages]
        without_image[1]["content"] = [part for part in messages[1]["content"] if part["type"] == "text"]
        monkeypatch.setattr(runtime, "request_parts", lambda *args: (without_image, {}, {}, None, {}))
        text_only = runtime.capacity.local_measure("research_step", context, {}, 100000)
        assert measured["estimated_input_tokens"] == text_only["estimated_input_tokens"] + IMAGE_TOKEN_RESERVE
        runtime.config["max_input_tokens"] = measured["estimated_input_tokens"] - 1
        monkeypatch.setattr(runtime, "request_parts", lambda *args: (messages, {}, {}, None, {}))
        assert not (await runtime.measure("research_step", context, {}, 100000))["fits"]
    finally:
        await runtime.aclose()


async def test_invalid_native_response_retains_private_audit_without_public_arguments(tmp_path):
    chunks = [
        {"choices": [{"delta": {"tool_calls": [call_delta(function={"name": "fs_read", "arguments": '{"file":"private-source"'})]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    runtime = await runtime_for(tmp_path, lambda request: response(chunks))
    try:
        with pytest.raises(ModelCallFailed) as failure:
            _ = [event async for event in runtime.stream([{"role": "user", "content": "read"}], tools=TOOLS)]
        safe = safe_failure_diagnostics(failure.value)
        assert safe["phase"] == "streaming_tool_call"
        assert safe["finish_reason"] == "tool_calls"
        assert safe["tool_call_chars"] > 0
        assert safe["protocol_issue"] == "tool_arguments_json"
        assert safe["tool_argument_error_position"] > 0
        assert "private-source" not in json.dumps(safe) + str(failure.value)
        assert "private-source" in partial_output_for_audit(failure.value)["content_head"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("shape", ["choices", "delta", "sse_error", "bad_json"])
async def test_protocol_failures_record_safe_stream_reason_without_reasoning(tmp_path, shape):
    chunks = [{"choices": [{"delta": {"reasoning_content": "PRIVATE_REASONING"}}]}]
    if shape == "choices":
        chunks.append({"choices": [None]})
    elif shape == "delta":
        chunks.append({"choices": [{"delta": ["PRIVATE_MALFORMED"]}]})
    elif shape == "sse_error":
        chunks.append({"error": {"message": "PRIVATE_PROVIDER_BODY", "type": "PRIVATE_TYPE"}})
    else:
        chunks.append("PRIVATE_BAD_EVENT")
    runtime = await runtime_for(tmp_path, lambda request: response(chunks))
    try:
        with pytest.raises(ModelCallFailed) as failure:
            _ = [e async for e in runtime.stream([{"role": "user", "content": "read"}], tools=TOOLS)]
        safe = safe_failure_diagnostics(failure.value)
        assert safe["protocol_issue"] == {
            "choices": "malformed_response", "delta": "malformed_response",
            "sse_error": "upstream_stream_error", "bad_json": "stream_shape_invalid",
        }[shape]
        assert safe["stream_event_index"] == 2
        assert safe["finish_reason"] == "missing"
        assert safe["reasoning_chars"] == len("PRIVATE_REASONING")
        assert partial_output_for_audit(failure.value) is None
        assert "PRIVATE" not in str(failure.value) + json.dumps(safe)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("archive_failure", [None, "event", "output"])
async def test_directory_protocol_failure_archives_rejected_delta_and_request(tmp_path, monkeypatch, archive_failure):
    from folio.research.planning import research_for_directory
    from folio.research.workspace import WorkspaceManager

    first = call_delta()
    second = call_delta(index=MAX_TOOL_CALLS_PER_BATCH, id="private-call", function={
        "name": "fs_read", "arguments": '{"file":"PRIVATE_SOURCE"}',
        "reasoning": "PRIVATE_REASONING", "headers": "PRIVATE_HEADER",
    })
    continuation = {"index": 0, "function": {"arguments": " "}}
    chunks = [{"choices": [{"delta": {"tool_calls": [first, continuation, second],
                                     "reasoning_content": "PRIVATE_REASONING"}}]}]
    runtime = await runtime_for(tmp_path, lambda request: response(chunks))
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "source.md").write_text("# 要求\n必须记录审批日志。\n")
    output = tmp_path / "research"
    original_write = WorkspaceManager._atomic_json

    def write(path, value):
        if (archive_failure == "event" and path.name.endswith("-error.json")
                or archive_failure == "output" and path.parent.name == "failed_outputs"):
            raise OSError("PRIVATE_DISK_ERROR")
        return original_write(path, value)

    monkeypatch.setattr(WorkspaceManager, "_atomic_json", staticmethod(write))
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await research_for_directory(runtime, sources, output, "整理审批流程")
        assert failure.value.diagnostics["protocol_issue"] == "tool_batch_limit"
        assert failure.value.diagnostics["research_request_id"] == "research_001"
        report = json.loads((output / "report.json").read_text())
        assert report["status"] == "partial" and report["calls"] == 1
        if archive_failure:
            return
        public = json.loads((output / "events/research_001-error.json").read_text())
        assert public["diagnostics"]["protocol_issue"] == "tool_batch_limit"
        assert public["diagnostics"]["research_request_id"] == "research_001"
        archive = json.loads((output / "failed_outputs/research_001.json").read_text())
        assert (output / archive["request_file"]).is_file()
        private = json.loads(archive["partial_output"]["content_head"])
        assert private["current_tool_delta"][0]["id"] == "private-call"
        assert "PRIVATE_SOURCE" in json.dumps(archive)
        assert "PRIVATE_REASONING" not in json.dumps(archive)
        assert "PRIVATE_HEADER" not in json.dumps(archive)
        assert "PRIVATE" not in json.dumps(public)
    finally:
        await runtime.aclose()
