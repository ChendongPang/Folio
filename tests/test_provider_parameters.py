import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.llm import LLMRuntime


@pytest.mark.parametrize(
    "mode,model,thinking,expected",
    [
        ("custom", "glm", None, {"temperature": 0.1}),
        ("custom", "kimi-k3", None, {"temperature": 0.1}),
        (
            "custom",
            "qwen",
            False,
            {"temperature": 0.1, "chat_template_kwargs": {"enable_thinking": False}},
        ),
        (
            "custom",
            "qwen",
            True,
            {"temperature": 0.1, "chat_template_kwargs": {"enable_thinking": True}},
        ),
        ("custom", "other-model", None, {"temperature": 0.1}),
    ],
)
async def test_provider_parameters_reach_wire_without_leaking_qwen_options(
    tmp_path, mode, model, thinking, expected
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            provider_mode=mode,
            base_url="https://example.test/v1",
            model=model,
            api_key="test-secret",
            enable_thinking=thinking,
            temperature=0.1,
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        events = [e async for e in runtime.stream([{"role": "user", "content": "test"}])]
        assert events[-1][0] == "completed"
        payload = requests[0]
        assert {
            k: payload[k]
            for k in ("temperature", "reasoning_effort", "chat_template_kwargs")
            if k in payload
        } == expected
        assert payload["model"] == model
        assert runtime.identity["generation_parameters"] == expected
    finally:
        await runtime.aclose()
