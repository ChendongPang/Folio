"""A job's HTTP allowance includes internal retries, pages and concurrent workers."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from decision_wire import transport as decision_transport

from folio.decision_client import evaluate
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import JobInput, Limits
from folio.runtime.errors import ModelCallBudgetExceeded
from folio.runtime.interaction_audit import interaction_scope, provider_attempt
from folio.runtime.llm import LLMRuntime
from folio.store import Store


def create_job(store, inputs, max_calls):
    return store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_calls=max_calls)))


def provider_starts(store, job_id):
    return [event["data"] for event in store.events(job_id)
            if event["kind"] == "provider_started"
            and event["data"]["provider"] in {"llm", "decision"}]


def test_internal_retries_share_http_budget_without_double_recording(store, inputs):
    job_id = create_job(store, inputs, 2)
    store.begin_task(job_id, "logical-call", 2)
    sent = []
    with interaction_scope(store, job_id, "select_context", "logical-call"):
        with pytest.raises(ValueError):
            with provider_attempt("llm", "chat.completions"):
                sent.append("first")
                raise ValueError("Incomplete response")
        with provider_attempt("decision", "evaluate"):
            sent.append("retry")
        with pytest.raises(ModelCallBudgetExceeded) as rejected:
            with provider_attempt("llm", "chat.completions"):
                sent.append("must not send")
    assert sent == ["first", "retry"]
    assert rejected.value.used == rejected.value.limit == 2
    assert store.get(job_id)["calls"] == 1
    assert len(provider_starts(store, job_id)) == 2
    assert len([event for event in store.events(job_id) if event["kind"] == "provider_completed"]) == 2


def test_provider_budget_is_atomic_across_concurrent_threads(store, inputs):
    job_id = create_job(store, inputs, 7)

    def attempt(index):
        try:
            with interaction_scope(store, job_id, "generate", f"request:{index}"):
                with provider_attempt("llm" if index % 2 else "decision", "inference"):
                    return True
        except ModelCallBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(attempt, range(32)))
    assert sum(results) == 7
    assert len(provider_starts(store, job_id)) == 7


def test_old_provider_events_and_restart_preserve_spending(store, inputs):
    job_id = create_job(store, inputs, 2)
    # Historical audit entries count without migration or a second budget table.
    store.audit_event(job_id, "provider_started", {"provider": "llm", "interaction_id": "old"})
    restarted = Store(store.root)
    with interaction_scope(restarted, job_id, "generate", "resumed-request"):
        with provider_attempt("decision", "evaluate"):
            pass
    restarted = Store(store.root)
    with interaction_scope(restarted, job_id, "generate", "another-resume"):
        with pytest.raises(ModelCallBudgetExceeded):
            with provider_attempt("llm", "chat.completions"):
                pytest.fail("Budget must survive restart")
    assert len(provider_starts(restarted, job_id)) == 2


def test_auxiliary_calls_and_other_jobs_do_not_consume_model_allowance(store, inputs):
    job_id = create_job(store, inputs, 1)
    with interaction_scope(store, job_id, "generate", "request"):
        for provider in ("llm_auxiliary", "tavily", "ragflow"):
            with provider_attempt(provider, "metadata-or-search"):
                pass
        with provider_attempt("llm", "chat.completions"):
            pass
    other = create_job(store, inputs, 1)
    with interaction_scope(store, other, "generate", "request"):
        with provider_attempt("decision", "evaluate"):
            pass
    assert len(provider_starts(store, job_id)) == len(provider_starts(store, other)) == 1


def test_cancelled_provider_attempt_still_consumes_budget(store, inputs):
    job_id = create_job(store, inputs, 1)
    with interaction_scope(store, job_id, "generate", "request"):
        with pytest.raises(asyncio.CancelledError):
            with provider_attempt("llm", "chat.completions"):
                raise asyncio.CancelledError()
        with pytest.raises(ModelCallBudgetExceeded):
            with provider_attempt("decision", "evaluate"):
                pytest.fail("Cancellation must not refund an already-started request")


async def test_real_llm_and_decision_transports_stop_before_http(store, inputs, tmp_path, monkeypatch):
    job_id = create_job(store, inputs, 2)
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.host == "model.test":
            event = {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json={"answers": {"ready": {"type": "noul", "noul": 1}}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=decision_transport(handler), **kwargs,
    ))
    settings = ModelSettingsStore(tmp_path / "configuration")
    settings.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    decision = {"provider_mode": "custom", "decision_model": "jev", "decision_base_url": "https://judge.test/v1", "decision_api_key": "test"}
    questions = {"ready": {"type": "noul", "instructions": "Is the service ready?"}}
    try:
        with interaction_scope(store, job_id, "generate", "mixed-logical-call"):
            async for _ in runtime.stream([{"role": "user", "content": "hello"}]):
                pass
            assert (await evaluate(decision, {}, questions))["answers"]["ready"]["noul"] == 1
            with pytest.raises(ModelCallBudgetExceeded):
                async for _ in runtime.stream([{"role": "user", "content": "blocked"}]):
                    pass
            with pytest.raises(ModelCallBudgetExceeded):
                await evaluate(decision, {}, questions)
    finally:
        await runtime.aclose()
    assert len(sent) == len(provider_starts(store, job_id)) == 2
