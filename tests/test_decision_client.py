import json

import httpx
import pytest

from folio.decision_client import DecisionServiceError, evaluate
from folio.model_settings import test_decision_connection as decision_probe


@pytest.mark.parametrize(
    "mode",
    [
        "missing_answer",
        "wrong_type",
        "outside_choice",
        "bad_probability",
        "nan_probability",
        "auth",
        "redirect",
        "timeout",
        "invalid_json",
    ],
)
async def test_decision_protocol_errors_are_safe_and_never_redirect_keys(monkeypatch, mode):
    requests = []

    def handler(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("jev-test-secret")
        if mode == "auth":
            return httpx.Response(401, text="jev-test-secret")
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://untrusted.test"})
        if mode == "invalid_json":
            return httpx.Response(200, text="jev-test-secret")
        answer = {
            "type": "choice",
            "choice": "a",
            "confidence": 0.9,
            "probabilities": {"a": 0.9, "b": 0.1},
        }
        if mode == "wrong_type":
            answer["type"] = "noul"
        if mode == "outside_choice":
            answer["choice"] = "unknown"
        if mode == "bad_probability":
            answer["confidence"] = 99
        if mode == "nan_probability":
            return httpx.Response(
                200,
                text='{"answers":{"q":{"type":"choice","choice":"a","confidence":NaN,"probabilities":{"a":1,"b":0}}}}',
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": "submit_decisions", "arguments": json.dumps({"answers": {} if mode == "missing_answer" else {"q": answer}})}}]}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(DecisionServiceError) as error:
        await evaluate(
            {"decision_base_url": "https://judge.test/v1", "decision_model": "judge", "decision_api_key": "jev-test-secret"},
            "context",
            {"q": {"type": "choice", "criteria": {"a": None, "b": None}}},
        )
    assert "jev-test-secret" not in str(error.value)
    assert len(requests) == 1


async def test_decision_connection_probe_requires_complete_typed_answer(monkeypatch):
    from folio import decision_client

    async def fail(*args, **kwargs):
        raise DecisionServiceError("决策服务未返回有效的完整判断。")

    monkeypatch.setattr(decision_client, "evaluate", fail)
    result = await decision_probe({"decision_base_url": "https://judge.test/v1", "decision_model": "judge", "decision_api_key": "jev-test-secret"})
    assert (
        not result["ok"]
        and result["code"] == "protocol"
        and "jev-test-secret" not in json.dumps(result)
    )
