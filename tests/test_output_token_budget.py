"""Configured output allowances reach every task and the full streaming transport."""

import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.diagnostics import safe_failure_diagnostics
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime
from folio.runtime.token_estimate import DEFAULT_OUTPUT_TOKEN_BUDGET


def runtime_for(tmp_path, **settings):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True,
                                  **settings))
    return LLMRuntime(store.path)


@pytest.mark.parametrize("task", [
    "style_analysis", "directory_research", "review_directory_research", "intent",
    "audit_work_dependencies", "design_flowchart", "validate_visual", "extract",
    "next_action", "query", "web_query", "select_context", "understand_work",
    "read_gap_materials", "plan_review", "read_review_materials", "review_focus",
    "drill_down", "integrate_supplement", "check_supplement_conflicts",
    "repair_supplement_conflicts", "format_document", "review_layout",
    "verify_extraction", "verify_visual_extraction", "plan", "repair_plan",
    "generate", "repair", "validate", "review_work", "review", "work_plan", "research_step",
])
async def test_every_task_inherits_the_default_output_budget(tmp_path, task):
    runtime = runtime_for(tmp_path)
    try:
        assert runtime.output_tokens(task, {}) == DEFAULT_OUTPUT_TOKEN_BUDGET == 128000
    finally:
        await runtime.aclose()


async def test_understanding_business_materials_sends_full_budget(tmp_path):
    runtime = runtime_for(tmp_path)
    await runtime.client.aclose()
    requests = []

    def handler(request):
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404)
        requests.append(json.loads(request.content))
        event = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("understand_work", {}, {}, "budget-regression")
        assert json.loads(result.content) == {}
        assert requests[0]["max_tokens"] == 128000
    finally:
        await runtime.aclose()


async def test_planning_retry_can_grow_past_the_old_64k_ceiling(tmp_path):
    runtime = runtime_for(tmp_path, planning_max_tokens=96000)

    async def measure(*args):
        return {"input_tokens": 500000, "context_window": 922000, "margin_tokens": 512}

    runtime.measure = measure
    try:
        result = await runtime.planning_retry_budget("plan", {}, {}, 600000)
        assert result["retry_output_tokens"] == 128000
        assert runtime.output_tokens("generate") == 128000
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("output_tokens", [8192, 128000])
async def test_stream_envelope_budget_scales_with_configured_output(tmp_path, output_tokens):
    runtime = runtime_for(tmp_path)
    await runtime.client.aclose()
    # A provider may wrap each generated token in a complete SSE envelope.
    # These 40K small deltas fit 128K output, but their wire text exceeds 8 MB.
    event = {"id": "completion-" + "a" * 180, "model": "test",
             "choices": [{"delta": {"content": "x"}}]}
    stream = ("data: " + json.dumps(event) + "\n\n") * 40000
    stream += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    assert len(stream) > 8_000_000
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)))

    async def consume():
        last = None
        async for kind, data in runtime.stream([{"role": "user", "content": "write"}],
                                                max_tokens=output_tokens):
            if kind == "completed":
                last = data
        return last

    try:
        if output_tokens == 128000:
            completed = await consume()
            assert completed["text"] == "x" * 40000
        else:
            with pytest.raises(ModelCallFailed) as failure:
                await consume()
            details = safe_failure_diagnostics(failure.value)
            assert failure.value.code == "length_limit"
            assert details["requested_output_tokens"] == output_tokens
            assert details["stream_event_chars"] > details["stream_character_limit"]
    finally:
        await runtime.aclose()
