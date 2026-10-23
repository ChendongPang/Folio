"""Public activity tracks transport facts without exposing structured output or reasoning."""

import asyncio
import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.activity import model_activity
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime


def sse(delta=None, finish=None):
    return (
        "data: "
        + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]})
        + "\n\n"
    ).encode()


def runtime_for(tmp_path, monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    # These fixtures model chat-completion streams, not /models or /tokenize.
    # Keep capacity checks local so cancellation targets the intended stream.
    runtime.capacity.ready = True
    return runtime


async def test_activity_exposes_only_actual_phases_and_never_model_content(tmp_path, monkeypatch):
    class Body(httpx.AsyncByteStream):
        def __init__(self, native):
            self.native = native

        async def __aiter__(self):
            yield sse({"reasoning_content": "private reasoning"})
            yield sse({"reasoning_content": "more private reasoning"})
            if self.native:
                yield sse({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
                    "name": "fs_read", "arguments": '{"file":"private document text"}',
                }}]})
            else:
                yield sse({"content": '{"answer": "private document text"}'})
            # Providers can interleave reasoning/content; the visible phase must
            # not regress or produce a row for each token.
            yield sse({"reasoning": "private later reasoning"})
            yield sse({"content": " "})
            yield sse(finish="tool_calls" if self.native else "stop")

    runtime = runtime_for(tmp_path, monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Body(bool(json.loads(request.content).get("tools")))
    ))
    events = []
    try:
        with model_activity(events.append):
            result = await runtime.complete("directory_research", {}, {"type": "object"}, "test")
        assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"file": "private document text"}
        assert [event["status"] for event in events] == [
            "waiting", "connected", "thinking", "responding"
        ]
        assert all(event["task"] == "directory_research" for event in events)
        assert events[-1]["response_chars"] == len('fs_read{"file":"private document text"}')
        assert all(set(event) <= {"status", "task", "response_chars"} for event in events)
        assert "private" not in json.dumps(events)
        assert "answer" not in json.dumps(events)
        # Subscription ends when its scope exits, even for this reused runtime.
        await runtime.complete("validate", {}, {"type": "object"}, "outside")
        assert len(events) == 4
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("status,content_type", [(401, "text/event-stream"), (200, "text/plain")])
async def test_rejected_response_never_claims_model_connected(
    tmp_path, monkeypatch, status, content_type
):
    runtime = runtime_for(tmp_path, monkeypatch, lambda _: httpx.Response(
        status, headers={"content-type": content_type}, text="private provider failure"
    ))
    events = []
    try:
        with model_activity(events.append), pytest.raises(ModelCallFailed):
            await runtime.complete("plan", {}, {"type": "object"}, "failed")
        assert events == [{"status": "waiting", "task": "plan"}]
    finally:
        await runtime.aclose()


async def test_concurrent_operations_keep_callbacks_and_task_labels_isolated(tmp_path, monkeypatch):
    async def handler(request):
        await asyncio.sleep(0)
        if json.loads(request.content).get("tools"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse({
                "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "fs_list", "arguments": "{}"}}],
            }) + sse(finish="tool_calls"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse({"content": "{}"}) + sse(finish="stop"))

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    research, review = [], []

    async def call(task, events):
        with model_activity(events.append):
            # API spawns the operation while the subscriber is in scope.
            operation = asyncio.create_task(runtime.complete(task, {}, {"type": "object"}, task))
        return await operation

    try:
        await asyncio.gather(call("directory_research", research), call("review_directory_research", review))
        for task, events in [("directory_research", research), ("review_directory_research", review)]:
            assert [event["status"] for event in events] == ["waiting", "connected", "responding"]
            assert all(event["task"] == task for event in events)
    finally:
        await runtime.aclose()


async def test_cancellation_restores_outer_subscription_and_task_label(tmp_path, monkeypatch):
    waiting = asyncio.Event()
    blocked = True

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if blocked:
                waiting.set()
                await asyncio.Event().wait()
            yield sse({"content": "{}"})
            yield sse(finish="stop")

    runtime = runtime_for(tmp_path, monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Body()
    ))
    outer, inner = [], []

    async def cancel_inner():
        nonlocal blocked
        with model_activity(outer.append):
            try:
                with model_activity(inner.append):
                    await runtime.complete("directory_research", {}, {"type": "object"}, "cancel")
            except asyncio.CancelledError:
                blocked = False
            # A raw stream must not inherit the cancelled request's task label.
            return [item async for item in runtime.stream([{"role": "user", "content": "test"}])]

    try:
        operation = asyncio.create_task(cancel_inner())
        await waiting.wait()
        operation.cancel()
        result = await operation
        assert result[-1][0] == "completed"
        assert inner == [
            {"status": "waiting", "task": "directory_research"},
            {"status": "connected", "task": "directory_research"},
        ]
        assert [event["status"] for event in outer] == ["waiting", "connected", "responding"]
        assert all("task" not in event for event in outer)
    finally:
        await runtime.aclose()


async def test_disconnected_progress_consumer_does_not_retry_or_fail_model(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse({"content": "{}"}) + sse(finish="stop"))

    def disconnected(_):
        raise RuntimeError("progress channel disconnected")

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        with model_activity(disconnected):
            result = await runtime.complete("plan", {}, {"type": "object"}, "disconnected")
        assert result.content == "{}" and len(requests) == 1
    finally:
        await runtime.aclose()
