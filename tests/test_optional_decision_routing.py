import json

import httpx
import pytest

from folio.decision_client import DecisionServiceError
from folio.decisions import ActionDecision, IntentDecision
from folio.decisions.routing import DecisionRouter
from folio.model_settings import probe_fingerprint
from folio.orchestration.context import ContextSelection, apply_selection, dependency_catalog
from folio.runtime.llm import LLMRuntime


@pytest.fixture(autouse=True)
def short_recovery_waits(monkeypatch):
    monkeypatch.setattr("folio.decisions.routing.DECISION_COOLDOWN_SECONDS", 0.001)


def enabled_config():
    config = {
        "provider_mode": "custom",
        "decision_model": "jev",
        "decision_base_url": "https://judge.test/v1", "decision_api_key": "test-secret",
        "decision_enabled": True,
    }
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    return config


async def test_disabled_or_stale_verification_makes_no_decision_calls(monkeypatch):
    async def fail(*args, **kwargs):
        pytest.fail("An unverified optional service was called")

    monkeypatch.setattr("folio.decision_client.evaluate", fail)
    config = enabled_config()
    config["decision_api_key"] = "changed"
    assert await DecisionRouter(config).try_complete(
        "intent", {}, IntentDecision.model_json_schema()
    ) == (None, None)
    config = enabled_config() | {"decision_enabled": False}
    assert await DecisionRouter(config).try_complete(
        "intent", {}, IntentDecision.model_json_schema()
    ) == (None, None)


async def test_intent_uses_typed_choices_and_stores_actual_model(monkeypatch):
    async def evaluate(config, state, questions):
        assert questions["intent"]["type"] == "choice"
        assert set(questions["intent"]["criteria"]) == {
            "chat",
            "analyze",
            "analyze_style",
            "generate",
            "resume_generation",
            "requirements",
            "clarify",
        }
        return {
            "answers": {"intent": {"choice": "generate", "confidence": 0.98}},
            "model": "jev-test",
            "usage": {"input_tokens": 12},
        }

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    result, fallback = await DecisionRouter(enabled_config()).try_complete(
        "intent", {"latest_user_message": "生成文档"}, IntentDecision.model_json_schema()
    )
    assert not fallback and json.loads(result.content)["intent"] == "generate"
    assert result.usage["decision_model"] == "jev-test"


async def test_optional_decision_routes_resume_using_real_job_state(monkeypatch):
    job = {"id": "existing-job", "status": "failed", "stage": "generation", "resumable": True}

    async def evaluate(config, state, questions):
        assert state["workflow"]["active_job"] == job
        assert "resume_generation" in questions["intent"]["criteria"]
        return {"answers": {"intent": {"choice": "resume_generation", "confidence": 0.99}}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    result, fallback = await DecisionRouter(enabled_config()).try_complete(
        "intent", {"latest_user_message": "接着上次任务继续", "workflow": {"active_job": job}},
        IntentDecision.model_json_schema(),
    )
    assert fallback is None
    assert IntentDecision.model_validate_json(result.content).intent == "resume_generation"


@pytest.mark.parametrize("confidence, choice", [(0.2, "generate"), (1, "unexpected")])
async def test_uncertain_or_illegal_choice_falls_back(monkeypatch, confidence, choice):
    async def evaluate(*args):
        return {"answers": {"intent": {"choice": choice, "confidence": confidence}}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "intent", {}, IntentDecision.model_json_schema()
    )
    assert result is None and reason == "ValueError"


@pytest.mark.parametrize("error", [DecisionServiceError, httpx.ConnectError, TimeoutError])
async def test_outage_recovery_is_bounded_and_never_returns_exception_contents(monkeypatch, error):
    calls = 0

    async def evaluate(*args):
        nonlocal calls
        calls += 1
        raise error("test-secret echoed by upstream")

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    router = DecisionRouter(enabled_config())
    for _ in range(4):
        result, reason = await router.try_complete("intent", {}, IntentDecision.model_json_schema())
        assert result is None and "secret" not in reason
    assert calls == (4 if error is DecisionServiceError else 12)


async def test_semantic_fallbacks_do_not_disable_a_healthy_decision_service(monkeypatch):
    calls = 0

    async def evaluate(*args):
        nonlocal calls
        calls += 1
        return {
            "answers": {"intent": {"choice": "chat", "confidence": 0.2 if calls <= 3 else 0.99}}
        }

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    router = DecisionRouter(enabled_config())
    for _ in range(3):
        result, reason = await router.try_complete("intent", {}, IntentDecision.model_json_schema())
        assert result is None and reason == "ValueError" and router.failures == 0
    result, reason = await router.try_complete("intent", {}, IntentDecision.model_json_schema())
    assert result is not None and reason is None and calls == 4


async def test_success_resets_consecutive_decision_transport_failures(monkeypatch):
    calls = 0

    async def evaluate(*args):
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            raise DecisionServiceError("temporary outage", "upstream")
        return {"answers": {"intent": {"choice": "chat", "confidence": 0.99}}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    router = DecisionRouter(enabled_config())
    for _ in range(2):
        result, reason = await router.try_complete("intent", {}, IntentDecision.model_json_schema())
        assert result is not None and reason is None
        assert router.failures == 0
    assert calls == 4


@pytest.mark.parametrize(
    "code",
    [
        "rate_limit",
        "authentication",
        "upstream",
        "timeout",
        "connection",
        "configuration",
        "protocol",
        "PRIVATE_SOURCE test-secret",
    ],
)
async def test_service_error_codes_are_allowlisted_not_copied_from_untrusted_errors(
    monkeypatch, code
):
    async def evaluate(*args):
        raise DecisionServiceError("PRIVATE_SOURCE test-secret", code)

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    router = DecisionRouter(enabled_config())
    result, reason = await router.try_complete("intent", {}, IntentDecision.model_json_schema())
    assert result is None
    assert reason == ("protocol" if "PRIVATE_SOURCE" in code else code)
    assert "PRIVATE_SOURCE" not in reason and "test-secret" not in reason
    assert router.failures == (
        3 if code in {"rate_limit", "upstream", "timeout", "connection"} else 1
    )


async def test_next_action_is_not_routed_through_decision_classification(monkeypatch):
    async def evaluate(*args):
        pytest.fail("Executable actions must use native main-model calls")

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    context = {
        "available_actions": ["write", "pending"],
        "business_record": {"facts": [{}], "open_questions": ["审批人是谁？"]},
        "evidence": [{"evidence_id": "ev1"}],
    }
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "next_action", context, ActionDecision.model_json_schema()
    )
    assert result is None and reason is None


async def test_relevance_requires_strict_threshold_for_sources_and_risks(monkeypatch):
    calls = []

    async def evaluate(config, state, questions):
        calls.append(questions)
        return {
            "answers": {
                key: {"noul": 0.8 if key in {"item_0", "item_1"} else 0.81} for key in questions
            }
        }

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    catalog = [
        {
            "context_ref": f"ref{i}",
            "kind": "risk" if i in {1, 2} else "evidence",
            "value": {"description": "审批角色尚未确认"},
        }
        for i in range(10)
    ]
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "select_context",
        {"catalog": catalog, "purpose": "test"},
        ContextSelection.model_json_schema(),
    )
    data = json.loads(result.content)
    assert not reason and len(calls) == 1 and len(calls[0]) == len(catalog)
    assert "ref0" not in data["selected_refs"] and data["risk_refs"] == ["ref2"]
    assert len(data["selected_refs"]) == 7
    assert result.usage["decision_relevance"][:2] == [
        {"context_ref": f"ref{i}", "probability": 0.8, "accepted": False, "threshold": 0.8}
        for i in range(2)
    ]


async def test_direct_relevance_router_counts_all_question_instructions_before_call(monkeypatch):
    # Exercise the complete-request boundary with an explicitly small budget;
    # the production compatibility default is now 600K characters.
    monkeypatch.setattr("folio.decisions.routing.DECISION_CONTEXT_CHARS", 32000)

    async def unexpected_call(*args, **kwargs):
        pytest.fail("Oversized full decision input must not reach the provider")

    monkeypatch.setattr("folio.decision_client.evaluate", unexpected_call)
    context = {
        "purpose": "检查限制",
        "catalog": [
            {"context_ref": f"r{i}", "kind": "evidence", "value": "规" * 1100} for i in range(24)
        ],
    }
    questions, _ = DecisionRouter.questions("select_context", context)
    assert len(json.dumps(context, ensure_ascii=False)) < 32000
    assert len(json.dumps({"state": context, "questions": questions}, ensure_ascii=False)) > 32000
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "select_context", context, ContextSelection.model_json_schema()
    )
    assert result is None and reason == "decision_context_too_large"


@pytest.mark.parametrize(
    "probability", [True, False, float("nan"), float("inf"), -float("inf"), "0.9"]
)
async def test_relevance_rejects_invalid_probability_even_for_custom_evaluator(
    monkeypatch, probability
):
    async def evaluate(*args):
        return {"answers": {"item_0": {"noul": probability}}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "select_context",
        {"catalog": [{"context_ref": "e1", "kind": "evidence", "value": "原文"}]},
        ContextSelection.model_json_schema(),
    )
    assert result is None and reason == "ValueError"


@pytest.mark.parametrize("model,temperature", [("kimi-k3", 0.1), ("mimo-pro", 0.1)])
async def test_router_model_specific_temperature(tmp_path, monkeypatch, model, temperature):
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://router.invalid/v1",
                "provider_mode": "airouter",
                "model": model,
                "api_key": "fake-key",
                "temperature": 0.1,
            }
        )
    )
    runtime = LLMRuntime(path)

    async def once(payload, headers):
        assert payload["temperature"] == temperature
        yield "completed", {"text": "OK", "usage": {}}

    runtime._stream_once = once
    try:
        assert [
            kind async for kind, _ in runtime.stream([{"role": "user", "content": "test"}])
        ] == ["completed"]
    finally:
        await runtime.aclose()


async def test_selected_dependency_risk_reaches_writer_with_original_text(monkeypatch):
    async def evaluate(config, state, questions):
        return {"answers": {key: {"noul": 0.9} for key in questions}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    risk = {
        "kind": "conflict",
        "description": "总行审批与分行审批规则冲突；尚未决定，不能默认由分行审批。",
        "section_ids": ["s1"],
    }
    catalog, objects, views = dependency_catalog(
        [{"task_id": "t1", "title": "审批", "findings": [risk], "sections": []}]
    )
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "select_context",
        {"catalog": catalog, "purpose": "编写审批流程"},
        ContextSelection.model_json_schema(),
    )
    assert not reason
    selection = ContextSelection.model_validate_json(result.content)
    apply_selection(views, objects, selection)
    brief = views[0]["risk_briefs"][0]
    assert json.loads(brief["summary"]) == risk
    assert brief["source_refs"] == [catalog[0]["context_ref"]]


@pytest.mark.parametrize("value", [None, {}])
async def test_risk_without_original_reports_invalid_input_instead_of_being_dropped(
    monkeypatch, value
):
    async def evaluate(config, state, questions):
        return {"answers": {key: {"noul": 0.9} for key in questions}}

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    item = {"context_ref": "risk1", "kind": "risk"}
    if value is not None:
        item["value"] = value
    result, reason = await DecisionRouter(enabled_config()).try_complete(
        "select_context",
        {"catalog": [item], "purpose": "检查风险"},
        ContextSelection.model_json_schema(),
    )
    assert result is None and reason == "ValueError"


async def test_runtime_falls_back_to_normal_llm_on_invalid_decision(monkeypatch, tmp_path):
    config = enabled_config() | {
        "version": 1,
        "base_url": "http://example.invalid/v1",
        "model": "test",
        "no_auth": True,
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(config))
    calls = []

    async def evaluate(*args):
        return {"answers": {"intent": {"choice": "generate", "confidence": 0.2}}}

    async def stream(self, messages, **kwargs):
        calls.append(messages)
        yield (
            "completed",
            {
                "text": '{"intent":"chat","summary":"正常模型回退"}',
                "usage": {},
                "stop_reason": "stop",
            },
        )

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    monkeypatch.setattr(LLMRuntime, "stream", stream)
    runtime = LLMRuntime(path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    try:
        result = await runtime.complete("intent", {}, IntentDecision.model_json_schema(), "test")
        assert calls and json.loads(result.content)["intent"] == "chat"
        assert result.usage["decision_fallback"] == "ValueError"
    finally:
        await runtime.aclose()
