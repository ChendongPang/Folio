"""Native execution cannot fall back to action text or old text-action caches."""

import json

import pytest
from native_calls import action_call, research_call
from test_request_context import engine_for

from folio.decisions import ActionDecision
from folio.models import digest
from folio.research.contracts import ResearchBatch
from folio.runtime.base import RuntimeResult, prompt_for
from folio.runtime.diagnostics import capture_partial_output
from folio.runtime.errors import ModelCallFailed, protocol_failure


async def test_native_next_action_bypasses_decision_provider_and_old_cache(store, inputs):
    class Runtime:
        identity = {"provider": "fixture"}
        calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            return action_call("search", summary="读取真实资料")

    class Decisions:
        async def decide(self, *args):
            pytest.fail("Executable native actions cannot use decision classification")

    runtime = Runtime()
    engine = engine_for(store, inputs, runtime)
    engine.decisions = Decisions()
    context = {"available_actions": ["search"], "evidence": []}
    schema = engine.request_schema("next_action", context, ActionDecision)
    old_key = "next_action:" + digest(
        prompt_for("next_action", context, schema) + json.dumps(runtime.identity, sort_keys=True)
    )
    store.begin_task(engine.job_id, old_key, engine.limits.max_calls)
    store.finish_task(engine.job_id, old_key, result='{"action":"search","summary":"旧文本动作"}')
    result = await engine._call("next_action", context, ActionDecision)
    assert runtime.calls == 1 and result.summary == "读取真实资料"
    assert (await engine._call("next_action", context, ActionDecision)) == result
    assert runtime.calls == 1  # Validated native cache remains reusable.


async def test_native_validation_retry_pairs_rejected_call_with_tool_error(store, inputs):
    class Runtime:
        contexts = []
        rejected = research_call("fs.read", {"page": "1-15"})

        async def complete(self, task, context, schema, session):
            self.contexts.append(context)
            if len(self.contexts) == 1:
                return self.rejected
            return research_call("fs.read", {"page": 1})

    runtime = Runtime()
    engine = engine_for(store, inputs, runtime)
    result = await engine._call("research_step", {}, ResearchBatch)
    assert len(result.steps) == 1 and result.steps[0].arguments == {"page": 1}
    failure = runtime.contexts[1]["observations"][-1]
    assert failure["tool_call"] == runtime.rejected.tool_calls[0]
    assert failure["request_id"] == runtime.rejected.tool_calls[0]["id"]
    assert failure["result"]["status"] == "failed"
    assert "the tool was not executed" in failure["model_text"]


async def test_native_task_rejects_text_action_even_when_json_is_valid(store, inputs):
    class Runtime:
        async def complete(self, *args):
            return RuntimeResult('{"tool":"finish","arguments":{},"summary":"文本不能触发工具"}')

    engine = engine_for(store, inputs, Runtime())
    engine.limits.call_attempts = 1
    with pytest.raises(ValueError, match="native tool_call"):
        await engine._call("research_step", {}, ResearchBatch)


@pytest.mark.parametrize("stream_failure", [False, True])
async def test_native_prose_retry_preserves_text_and_memory_and_requests_real_finish(store, inputs, stream_failure):
    from folio.research.transport import research_messages

    prose = "研究已完成，剩余企业资质只能由用户补充。"

    class Runtime:
        contexts = []

        async def complete(self, task, context, schema, session):
            self.contexts.append(context)
            if len(self.contexts) == 1:
                if stream_failure:
                    error = protocol_failure(
                        "tool_finish_invalid", tool_calls_received=0, finish_reason="stop",
                    )
                    capture_partial_output(error, prose)
                    raise error
                return RuntimeResult(prose)
            return research_call("finish", {}, missing_information=["企业资质"])

    runtime = Runtime()
    engine = engine_for(store, inputs, runtime)
    existing = {"research_notes": "已阅读全部资格章节。", "observations": []}
    result = await engine._call("research_step", existing, ResearchBatch)
    assert result.steps[0].tool == "finish"
    retry = runtime.contexts[1]
    assert retry["research_notes"] == existing["research_notes"]
    assert retry["observations"] == []
    assert "No tool was executed" in retry["format_feedback"]
    assert "call finish" in retry["format_feedback"]
    messages = research_messages(retry, {}, task="research_step")
    assert messages[-2] == {"role": "assistant", "content": prose}
    assert messages[-1] == {"role": "user", "content": retry["format_feedback"]}
    assert not any("tool_calls" in message for message in messages)
    assert "native_retry_response" not in messages[1]["content"]


async def test_native_prose_retry_stops_at_existing_attempt_limit(store, inputs):
    class Runtime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            error = protocol_failure("tool_finish_invalid", tool_calls_received=0, finish_reason="stop")
            capture_partial_output(error, '{"tool": "finish", "arguments": {}}')
            raise error

    runtime = Runtime()
    engine = engine_for(store, inputs, runtime)
    engine.limits.call_attempts = 2
    with pytest.raises(ModelCallFailed, match="completed native tool call"):
        await engine._call("research_step", {}, ResearchBatch)
    assert runtime.calls == 2
