"""Decision outages recover without losing strict routing or HTTP accounting."""

import asyncio
import json
import time

import httpx
import pytest
from decision_wire import payload as decision_payload
from decision_wire import transport as decision_transport

from folio.decision_client import DecisionServiceError
from folio.decisions.routing import DecisionRouter
from folio.model_settings import probe_fingerprint
from folio.orchestration.context import ContextSelection
from folio.runtime.interaction_audit import interaction_scope


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr("folio.decisions.routing.DECISION_COOLDOWN_SECONDS", 0.01)
    config = {
        "provider_mode": "custom",
        "decision_model": "jev",
        "decision_base_url": "https://judge.test/v1", "decision_api_key": "TEST_PRIVATE_KEY",
        "decision_enabled": True,
    }
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    return DecisionRouter(config)


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=decision_transport(handler), **kw)
    )


def response(request, probability=0.91):
    payload = decision_payload(request)
    return httpx.Response(
        200,
        json={
            "answers": {key: {"type": "noul", "noul": probability} for key in payload["questions"]}
        },
    )


async def select(router, name="source"):
    return await router.try_complete(
        "select_context",
        {"purpose": name, "catalog": [{"context_ref": name, "kind": "evidence", "value": name}]},
        ContextSelection.model_json_schema(),
    )


async def test_transient_retry_is_counted_as_a_real_http_call_and_keeps_strict_threshold(
    router, monkeypatch
):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, text="TEST_PRIVATE_KEY PRIVATE_PROVIDER_BODY")
        return response(request, 0.8)

    mock_http(monkeypatch, handler)
    result, reason = await select(router)
    assert reason is None and json.loads(result.content)["selected_refs"] == []
    assert result.usage["decision_calls"] == len(requests) == 2
    entries = result.usage["decision_http_attempts"]
    assert [e["attempt"] for e in entries] == [1, 2]
    assert [e["status_code"] for e in entries] == [503, 200]
    assert [e["outcome"] for e in entries] == ["upstream", "success"]
    assert all(e["duration_ms"] >= 0 and e["started_at"] for e in entries)
    assert len({e["request_id"] for e in entries}) == 2
    assert "TEST_PRIVATE_KEY" not in json.dumps(result.usage)
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps(result.usage)
    assert router.failures == 0


async def test_concurrent_outage_allows_one_probe_then_restores_waiting_callers(
    router, monkeypatch
):
    requests = []
    first_wave = asyncio.Event()
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def handler(request):
        requests.append(request)
        number = len(requests)
        if number <= 2:
            if number == 2:
                first_wave.set()
            await first_wave.wait()
            return httpx.Response(503)
        if number == 3:
            probe_started.set()
            await release_probe.wait()
        return response(request)

    mock_http(monkeypatch, handler)
    initial = [asyncio.create_task(select(router, f"initial-{i}")) for i in range(2)]
    await asyncio.wait_for(probe_started.wait(), 1)
    waiting = [asyncio.create_task(select(router, f"waiting-{i}")) for i in range(8)]
    await asyncio.sleep(0.03)
    assert len(requests) == 3, "Only the half-open probe may contact a recovering provider"
    release_probe.set()
    results = await asyncio.gather(*initial, *waiting)
    assert all(value is not None and reason is None for value, reason in results)
    entries = [entry for value, _ in results for entry in value.usage["decision_http_attempts"]]
    assert len(requests) == len(entries) == 12
    assert len({entry["request_id"] for entry in entries}) == len(entries)
    assert all(len(value.usage["decision_http_attempts"]) <= 3 for value, _ in results)
    assert router.failures == 0 and not router._probe_inflight and router._retry_at == 0


async def test_exhausted_request_does_not_permanently_disable_future_healthy_requests(
    router, monkeypatch
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(502) if len(requests) <= 3 else response(request)

    mock_http(monkeypatch, handler)
    value, reason = await select(router)
    assert value is None and reason == "upstream" and len(requests) == 3
    assert len(reason.diagnostics["decision_http_attempts"]) == 3
    value, reason = await select(router)
    assert value is not None and reason is None and len(requests) == 4
    assert value.usage["decision_calls"] == 1 and router.failures == 0


async def test_old_inflight_success_cannot_release_a_new_recovery_probe(router, monkeypatch):
    requests = []
    old_started, old_release = asyncio.Event(), asyncio.Event()
    probe_started, probe_release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        requests.append(request)
        number = len(requests)
        if number == 1:
            old_started.set()
            await old_release.wait()
        elif number in {2, 3}:
            return httpx.Response(503)
        elif number == 4:
            probe_started.set()
            await probe_release.wait()
        return response(request)

    mock_http(monkeypatch, handler)
    old = asyncio.create_task(select(router, "old"))
    await asyncio.wait_for(old_started.wait(), 1)
    failed = [asyncio.create_task(select(router, f"failed-{i}")) for i in range(2)]
    await asyncio.wait_for(probe_started.wait(), 1)
    old_release.set()
    assert (await old)[0] is not None
    waiting = asyncio.create_task(select(router, "waiting"))
    await asyncio.sleep(0.03)
    assert len(requests) == 4 and router._probe_inflight
    probe_release.set()
    results = await asyncio.gather(*failed, waiting)
    assert all(value is not None and reason is None for value, reason in results)
    assert len(requests) == 6 and not router._probe_inflight


@pytest.mark.parametrize(
    "status,code", [(401, "authentication"), (403, "authentication"), (402, "billing")]
)
async def test_permanent_failures_are_not_retried_or_recounted(router, monkeypatch, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="TEST_PRIVATE_KEY")

    mock_http(monkeypatch, handler)
    store = AuditStore()
    with interaction_scope(store, "job", "select_context", "select:permanent"):
        first, reason = await select(router)
        assert first is None and reason == code
        assert len(reason.diagnostics["decision_http_attempts"]) == 1
        for _ in range(3):
            value, reason = await select(router)
            assert value is None and reason == code
            assert reason.diagnostics["decision_http_attempts"] == []
    assert len(requests) == 1
    completed = [data for kind, data in store.events if kind == "provider_completed"]
    assert len(completed) == 1 and completed[0]["status_code"] == status
    assert "TEST_PRIVATE_KEY" not in json.dumps(reason.diagnostics)


@pytest.mark.parametrize("status", [307, 400, 404, 422])
async def test_request_errors_do_not_retry_but_do_not_poison_other_inputs(
    router, monkeypatch, status
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status) if len(requests) == 1 else response(request)

    mock_http(monkeypatch, handler)
    value, reason = await select(router)
    assert value is None and reason == ("endpoint_not_found" if status == 404 else "upstream") and len(requests) == 1
    assert not reason.diagnostics["decision_http_attempts"][0]["retryable"]
    value, reason = await select(router)
    assert value is not None and reason is None and len(requests) == 2


async def test_long_retry_after_is_respected_then_service_can_recover(router, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return response(request)

    mock_http(monkeypatch, handler)
    for _ in range(3):
        value, reason = await select(router)
        assert value is None and reason == "rate_limit"
    assert len(requests) == 1
    assert router._retry_at - time.monotonic() > 3500
    router._retry_at = time.monotonic() - 1  # Simulate expiry, not a real minute-long test.
    value, reason = await select(router)
    assert value is not None and reason is None and len(requests) == 2


class AuditStore:
    def __init__(self):
        self.events = []

    def audit_event(self, job_id, kind, data):
        self.events.append((kind, data))


async def test_cancelled_probe_releases_waiters_and_records_http_cancellation(router, monkeypatch):
    requests = []
    started = asyncio.Event()

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            started.set()
            await asyncio.Event().wait()
        return response(request)

    mock_http(monkeypatch, handler)
    router._retry_at = time.monotonic() - 1
    router._last_error = DecisionServiceError("outage", "upstream")
    store = AuditStore()
    with interaction_scope(store, "job", "select_context", "select:cancel"):
        task = asyncio.create_task(select(router))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        value, reason = await select(router)
    assert value is not None and reason is None and len(requests) == 2
    assert not router._probe_inflight
    completions = [data for kind, data in store.events if kind == "provider_completed"]
    assert [entry["outcome"] for entry in completions] == ["cancelled", "success"]


async def test_total_recovery_deadline_accounts_for_cancelled_http_without_latching(
    router, monkeypatch
):
    monkeypatch.setattr("folio.decisions.routing.DECISION_RECOVERY_SECONDS", 0.03)
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            await asyncio.sleep(1)
        return response(request)

    mock_http(monkeypatch, handler)
    value, reason = await select(router)
    assert value is None and reason == "timeout" and len(requests) == 1
    assert reason.diagnostics["decision_http_attempts"][0]["outcome"] == "cancelled"
    value, reason = await select(router)
    assert value is not None and reason is None and len(requests) == 2


async def test_client_initialization_failure_is_not_counted_as_an_http_call(router, monkeypatch):
    def bad_client(**kwargs):
        raise ValueError("local setup failure")

    monkeypatch.setattr(httpx, "AsyncClient", bad_client)
    router._retry_at = time.monotonic() - 1
    router._last_error = DecisionServiceError("outage", "upstream")
    store = AuditStore()
    with interaction_scope(store, "job", "select_context", "select:setup"):
        value, reason = await select(router)
    assert value is None and reason == "ValueError"
    assert reason.diagnostics["decision_http_attempts"] == []
    assert store.events == []
    assert not router._probe_inflight


@pytest.mark.parametrize("status,code", [(401, "authentication"), (402, "billing")])
async def test_engine_does_not_retry_permanent_decision_errors_and_saves_safe_http_metrics(
    tmp_path, monkeypatch, store, inputs, status, code
):
    from test_material_selection import evidence_catalog, runtime_for
    from test_request_context import engine_for

    from folio.runtime.errors import ModelCallFailed

    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "judge.test"
        return httpx.Response(status, text="PRIVATE_PROVIDER_BODY")

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    engine = engine_for(store, inputs, runtime)
    engine.limits.call_attempts = 3
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await engine._call(
                "select_context",
                {"purpose": "付款", "catalog": evidence_catalog(1)},
                ContextSelection,
            )
    finally:
        await runtime.aclose()
    assert caught.value.code == code and len(requests) == 1
    with store.connect() as conn:
        tasks = conn.execute(
            "SELECT attempts,usage FROM tasks WHERE job_id=?", (engine.job_id,)
        ).fetchall()
    assert len(tasks) == 1 and tasks[0]["attempts"] == 1
    diagnostic = json.loads(tasks[0]["usage"])["diagnostics"]
    assert len(diagnostic["decision_http_attempts"]) == 1
    assert diagnostic["status_code"] == status
    providers = [e for e in store.events(engine.job_id) if e["kind"] == "provider_completed"]
    assert len(providers) == 1
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps([diagnostic, providers])
