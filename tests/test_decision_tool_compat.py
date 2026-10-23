"""Reasoning-compatible decision calls retain native tools and HTTP accounting."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from folio.decision_client import DecisionServiceError, collect_http_attempts, evaluate
from folio.runtime.errors import ModelCallBudgetExceeded
from folio.runtime.interaction_audit import interaction_scope
from folio.store import Store

CONFIG = {
    "decision_base_url": "https://judge.example/v1",
    "decision_model": "arbitrary-reasoning-model",
    "decision_api_key": "PRIVATE_KEY",
}
QUESTIONS = {"ready": {"type": "noul"}}


def rejected(status=400, message="tool_choice is not supported in thinking mode PRIVATE_BODY"):
    return httpx.Response(status, json={"error": {"message": message}})


def answered():
    return httpx.Response(200, json={
        "choices": [{"message": {"tool_calls": [{"type": "function", "id": "decision-1", "function": {
            "name": "submit_decisions",
            "arguments": json.dumps({"answers": {"ready": {"type": "noul", "noul": 0.9}}}),
        }}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


async def test_reasoning_rejection_retries_auto_with_same_payload_and_two_audited_calls(monkeypatch, tmp_path):
    requests, attempts = [], []
    original_config = deepcopy(CONFIG)

    def handler(request):
        requests.append(json.loads(request.content))
        return rejected() if len(requests) == 1 else answered()

    mock_http(monkeypatch, handler)
    store = Store(tmp_path)
    with interaction_scope(store, "job", "selection", "selection:one"), collect_http_attempts(attempts):
        result = await evaluate(CONFIG, {"ready": True}, QUESTIONS)
    assert len(requests) == len(attempts) == 2
    assert requests[0]["tool_choice"] == {"type": "function", "function": {"name": "submit_decisions"}}
    assert requests[1] == requests[0] | {"tool_choice": "auto"}
    assert CONFIG == original_config
    assert result["answers"]["ready"]["noul"] == 0.9
    assert result["usage"]["total_tokens"] == 120
    assert [entry["status_code"] for entry in attempts] == [400, 200]
    assert [entry["outcome"] for entry in attempts] == ["tool_choice_unsupported", "success"]
    assert [entry["attempt"] for entry in attempts] == [1, 2]
    assert len({entry["request_id"] for entry in attempts}) == 2
    events = store.events("job")
    completed = [event["data"] for event in events if event["kind"] == "provider_completed"]
    assert len(completed) == 2
    assert [entry["outcome"] for entry in completed] == ["failed", "success"]
    assert completed[0]["error_code"] == "tool_choice_unsupported"
    assert completed[1]["total_tokens"] == 120
    assert "PRIVATE" not in json.dumps(attempts + events)


@pytest.mark.parametrize("status,message", [
    (400, "Invalid model name"),
    (400, "tool_choice invalid without reasoning restriction"),
    (401, "tool_choice is not supported in thinking mode"),
    (429, "tool_choice is not supported in thinking mode"),
    (500, "tool_choice is not supported in thinking mode"),
])
async def test_unrelated_http_errors_do_not_trigger_compatibility_retry(monkeypatch, status, message):
    requests, attempts = [], []

    def handler(request):
        requests.append(request)
        return rejected(status, message)

    mock_http(monkeypatch, handler)
    with collect_http_attempts(attempts), pytest.raises(DecisionServiceError):
        await evaluate(CONFIG, {}, QUESTIONS)
    assert len(requests) == len(attempts) == 1


@pytest.mark.parametrize("failure", ["same_rejection", "plain_text", "wrong_tool", "multiple_tools", "invalid_answers"])
async def test_auto_response_still_requires_valid_single_tool_and_has_no_third_attempt(monkeypatch, failure):
    requests, attempts = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1 or failure == "same_rejection":
            return rejected()
        body = answered().json()
        message = body["choices"][0]["message"]
        if failure == "plain_text":
            message.pop("tool_calls")
            message["content"] = '{"answers":{"ready":{"type":"noul","noul":0.9}}}'
        elif failure == "wrong_tool":
            message["tool_calls"][0]["function"]["name"] = "another_tool"
        elif failure == "multiple_tools":
            message["tool_calls"].append(deepcopy(message["tool_calls"][0]))
        else:
            message["tool_calls"][0]["function"]["arguments"] = '{"answers":{"ready":{"type":"noul","noul":5}}}'
        return httpx.Response(200, json=body)

    mock_http(monkeypatch, handler)
    with collect_http_attempts(attempts), pytest.raises(DecisionServiceError) as caught:
        await evaluate(CONFIG, {}, QUESTIONS)
    assert len(requests) == len(attempts) == 2
    assert caught.value.diagnostics["decision_http_attempts"] == attempts
    assert caught.value.code == ("tool_choice_unsupported" if failure == "same_rejection" else "protocol")
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.diagnostics)


async def test_compatibility_retry_reserves_budget_before_second_http_request(monkeypatch, tmp_path):
    requests, attempts = [], []
    store = Store(tmp_path)
    reserve = store.begin_provider_attempt

    def limited_reserve(job_id, entry):
        if any(event["kind"] == "provider_started" for event in store.events(job_id)):
            raise ModelCallBudgetExceeded(1, 1)
        reserve(job_id, entry)

    def handler(request):
        requests.append(request)
        return rejected()

    monkeypatch.setattr(store, "begin_provider_attempt", limited_reserve)
    mock_http(monkeypatch, handler)
    with interaction_scope(store, "job", "selection", "selection:one"), collect_http_attempts(attempts), pytest.raises(ModelCallBudgetExceeded):
        await evaluate(CONFIG, {}, QUESTIONS)
    assert len(requests) == len(attempts) == 1


async def test_cancelled_compatibility_retry_keeps_both_http_attempts(monkeypatch):
    requests, attempts = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return rejected()
        raise asyncio.CancelledError()

    mock_http(monkeypatch, handler)
    with collect_http_attempts(attempts), pytest.raises(asyncio.CancelledError):
        await evaluate(CONFIG, {}, QUESTIONS)
    assert len(requests) == len(attempts) == 2
    assert attempts[1]["outcome"] == "cancelled"
