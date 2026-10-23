"""One configurable model deadline replaces the former hard 180-second cutoffs."""

import asyncio
import json
from contextlib import nullcontext

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import JobInput, Limits
from folio.orchestration.engine import Engine
from folio.orchestration.work import BusinessRecord
from folio.runtime.demo import DemoRuntime
from folio.runtime.diagnostics import safe_failure_diagnostics
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime
from folio.runtime.timeouts import (
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
    current_model_call_timeout,
    model_call_timeout,
)


def test_default_deadline_and_nested_override_restore_cleanly():
    assert Limits().call_timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS == 900
    assert current_model_call_timeout() == 900
    with model_call_timeout(1200):
        assert current_model_call_timeout() == 1200
        with model_call_timeout(30):
            assert current_model_call_timeout() == 30
        assert current_model_call_timeout() == 1200
    assert current_model_call_timeout() == 900


@pytest.mark.parametrize("seconds,accepted", [(None, True), (1200, True), (100, False)])
async def test_stream_uses_one_deadline_and_preserves_timeout_diagnostics(tmp_path, monkeypatch, seconds, accepted):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    assert runtime.client.timeout.read == 60  # Shared defaults remain for auxiliary requests.
    await runtime.client.aclose()
    durations = []
    original_timeout = asyncio.timeout

    def scaled_timeout(value):
        durations.append(value)
        # A 400-second model response is simulated in 40 ms. A leftover 180
        # second guard would interrupt it; the default 900-second budget will not.
        return original_timeout(value / 10000)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n\n'
            await asyncio.sleep(0.04)
            chunk = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
            yield ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())))
    monkeypatch.setattr("folio.runtime.llm.asyncio.timeout", scaled_timeout)
    try:
        with model_call_timeout(seconds) if seconds is not None else nullcontext():
            if accepted:
                events = [event async for event in runtime.stream([{"role": "user", "content": "write"}])]
                assert events[-1][1]["text"] == "{}"
            else:
                with pytest.raises(ModelCallFailed) as caught:
                    _ = [event async for event in runtime.stream([{"role": "user", "content": "write"}])]
                assert caught.value.code == "timeout"
                details = safe_failure_diagnostics(caught.value)
                assert details["reasoning_chars"] == len("private reasoning")
                assert details["phase"] == "first_content"
                assert details["call_timeout_seconds"] == seconds
                assert "private reasoning" not in json.dumps(details)
        assert durations == [900 if seconds is None else seconds]
        assert current_model_call_timeout() == 900
    finally:
        await runtime.aclose()


async def test_generation_has_no_separate_idle_deadline_or_shared_client_mutation(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    sent = []

    def handler(request):
        sent.append(request.extensions["timeout"])
        chunk = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(60, connect=17, write=23, pool=29),
    )
    try:
        with model_call_timeout(1200):
            assert [event async for event in runtime.stream([{"role": "user", "content": "write"}])]
        assert sent == [{"connect": 17, "read": None, "write": 23, "pool": 29}]
        assert runtime.client.timeout.as_dict() == {
            "connect": 17, "read": 60, "write": 23, "pool": 29,
        }
    finally:
        await runtime.aclose()


async def test_concurrent_jobs_propagate_their_own_deadlines_without_runtime_mutation(store, inputs):
    ready = asyncio.Event()
    observed = {}

    class Runtime(DemoRuntime):
        async def complete(self, task, context, schema, session):
            name = context["work_task"]["objective"]
            before = current_model_call_timeout()
            observed[name] = [before]
            if len(observed) == 2:
                ready.set()
            await ready.wait()
            observed[name].append(current_model_call_timeout())
            return await super().complete(task, context, schema, session)

    runtime = Runtime()

    async def call(seconds):
        engine = Engine(store, runtime)
        engine.request = JobInput(**inputs, runtime="demo", limits=Limits(call_timeout_seconds=seconds))
        engine.limits = engine.request.limits
        engine.job_id = store.create(engine.request)
        return await engine._call("understand_work", {
            "work_task": {"objective": str(seconds)}, "evidence": [],
        }, BusinessRecord)

    await asyncio.wait_for(asyncio.gather(call(30), call(1500)), timeout=2)
    assert observed == {"30": [30, 30], "1500": [1500, 1500]}
    assert current_model_call_timeout() == 900
