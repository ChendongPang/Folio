"""Pausing drains execution, preserves checkpoints, and works across Store instances."""

import asyncio

import pytest
from filelock import FileLock, Timeout

from folio.runtime.demo import DemoRuntime
from folio.service import execute
from folio.store import Store


def test_control_transitions_preserve_budget_and_resume_stage(store, demo_job):
    store.begin_task(demo_job, "saved", 100)
    store.finish_task(demo_job, "saved", result='{"ok":true}')
    assert store.request_pause(demo_job)["status"] == "paused"
    assert store.request_pause(demo_job)["status"] == "paused"
    assert demo_job not in store.candidates()
    assert store.start_execution(demo_job)["status"] == "paused"
    assert store.resume_job(demo_job)["status"] == "queued"
    assert store.resume_job(demo_job)["status"] == "queued"
    assert store.start_execution(demo_job)["status"] == "running"
    store.update(demo_job, stage="review")
    assert store.request_pause(demo_job)["status"] == "pausing"
    assert demo_job in store.candidates()
    with pytest.raises(ValueError):
        store.resume_job(demo_job)
    assert store.acknowledge_pause(demo_job)["status"] == "paused"
    store.resume_job(demo_job)
    resumed = store.start_execution(demo_job)
    assert resumed["stage"] == "review" and resumed["calls"] == 1
    assert store.task(demo_job, "saved")["result"] == '{"ok":true}'


@pytest.mark.parametrize("status", ["published", "draft", "needs_confirmation"])
def test_terminal_job_controls_rejected(store, demo_job, status):
    store.update(demo_job, status=status)
    with pytest.raises(ValueError):
        store.request_pause(demo_job)
    with pytest.raises(ValueError):
        store.resume_job(demo_job)
    assert store.acknowledge_pause(demo_job)["status"] == status


def test_failed_resume_grants_once_and_keeps_checkpoints_and_spend(store, demo_job):
    store.begin_task(demo_job, "saved", 100)
    store.finish_task(demo_job, "saved", result='{"ok":true}')
    store.begin_task(demo_job, "failed", 100)
    store.finish_task(demo_job, "failed", error="Model HTTP 502")
    store.update(demo_job, status="failed", stage="generation", error="Model HTTP 502")
    before = store.task(demo_job, "failed")
    assert store.start_execution(demo_job)["status"] == "failed"
    with pytest.raises(ValueError):
        store.request_pause(demo_job)
    reopened = Store(store.root)
    result = reopened.resume_job(demo_job)
    assert result["status"] == "queued" and result["stage"] == "generation"
    assert result["calls"] == 2 and result["error"] is None
    assert store.task(demo_job, "failed") == before
    assert store.task(demo_job, "saved")["result"] == '{"ok":true}'
    assert store.task_retry_baseline(demo_job, "failed") == 1
    assert store.task_retry_baseline(demo_job, "saved") == 0
    store.resume_job(demo_job)
    store.start_execution(demo_job)
    store.resume_job(demo_job)
    grants = [e for e in store.events(demo_job) if e["kind"] == "task_retry_authorized"]
    assert len(grants) == 1


def test_pause_with_failed_parallel_task_grants_once_per_consumed_attempt(store, demo_job):
    store.begin_task(demo_job, "saved", 100)
    store.finish_task(demo_job, "saved", result='{"ok":true}')
    store.begin_task(demo_job, "failed", 100)
    store.finish_task(demo_job, "failed", error="native tool protocol error")
    store.begin_task(demo_job, "interrupted", 100)
    store.update(demo_job, status="running", stage="generation")
    store.request_pause(demo_job)
    store.acknowledge_pause(demo_job)
    before = {key: store.task(demo_job, key) for key in ("saved", "failed", "interrupted")}
    store.resume_job(demo_job)
    assert store.task_retry_baseline(demo_job, "failed") == 1
    assert store.task_retry_baseline(demo_job, "saved") == 0
    assert store.task_retry_baseline(demo_job, "interrupted") == 0
    # Toggling controls without another failed attempt must not multiply grants.
    store.request_pause(demo_job)
    store.resume_job(demo_job)
    assert store.get(demo_job)["calls"] == 3
    assert store.get(demo_job)["stage"] == "generation"
    assert before == {key: store.task(demo_job, key) for key in before}
    assert sum(e["kind"] == "task_retry_authorized" for e in store.events(demo_job)) == 1


@pytest.mark.parametrize("status", ["paused", "pausing"])
async def test_startup_never_restarts_a_requested_pause(store, demo_job, monkeypatch, status):
    store.update(demo_job, status=status)

    def unexpected():
        raise AssertionError("Paused jobs must not construct a runtime")

    monkeypatch.setattr("folio.service.DemoRuntime", unexpected)
    assert (await execute(store, demo_job))["status"] == "paused"


async def test_pause_waits_for_cancel_and_provider_close_before_unlock(store, demo_job, monkeypatch):
    started, cleanup, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    allow_cleanup, allow_close = asyncio.Event(), asyncio.Event()

    class Runtime:
        async def aclose(self):
            closed.set()
            await allow_close.wait()

    class Engine:
        def __init__(self, *args):
            pass

        async def run(self, job_id, **kwargs):
            store.start_execution(job_id)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await allow_cleanup.wait()

    monkeypatch.setattr("folio.service.Engine", Engine)
    monkeypatch.setattr("folio.service.DemoRuntime", Runtime)
    monkeypatch.setattr("folio.service.PAUSE_POLL_SECONDS", 0.001)
    worker = asyncio.create_task(execute(store, demo_job))
    await asyncio.wait_for(started.wait(), 3)
    # An independently constructed Store models a control request from another process.
    remote = Store(store.root)
    assert remote.request_pause(demo_job)["status"] == "pausing"
    await asyncio.wait_for(cleanup.wait(), 3)
    assert not worker.done() and store.get(demo_job)["status"] == "pausing"
    with pytest.raises(Timeout):
        FileLock(store.root / f"{demo_job}.lock").acquire(timeout=0)
    allow_cleanup.set()
    await asyncio.wait_for(closed.wait(), 3)
    assert store.get(demo_job)["status"] == "pausing"
    allow_close.set()
    assert (await asyncio.wait_for(worker, 3))["status"] == "paused"
    with FileLock(store.root / f"{demo_job}.lock").acquire(timeout=0):
        pass


async def test_real_engine_pause_resume_reuses_completed_body(store, demo_job, monkeypatch):
    reviewing = asyncio.Event()
    calls = []
    cancelled = asyncio.Event()
    block_review = True

    class Runtime(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            calls.append(task)
            if task == "review" and block_review:
                reviewing.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().complete(task, context, schema, session_key)

    monkeypatch.setattr("folio.service.DemoRuntime", Runtime)
    monkeypatch.setattr("folio.service.PAUSE_POLL_SECONDS", 0.001)
    worker = asyncio.create_task(execute(store, demo_job))
    await asyncio.wait_for(reviewing.wait(), 15)
    before = [s.model_dump() for s in store.states(demo_job)]
    assert before
    store.request_pause(demo_job)
    assert (await asyncio.wait_for(worker, 5))["status"] == "paused"
    assert cancelled.is_set()
    assert [s.model_dump() for s in store.states(demo_job)] == before
    writing_calls = calls.count("generate")
    block_review = False
    store.resume_job(demo_job)
    result = await asyncio.wait_for(execute(store, demo_job), 15)
    assert result["status"] == "draft"  # Demo output never claims production validation.
    assert result["artifact"]
    assert calls.count("generate") == writing_calls


async def test_real_engine_failed_resume_reuses_completed_body(store, demo_job, monkeypatch):
    from folio.models import Review
    from folio.orchestration.engine import Engine

    calls = []
    fail_review = True

    class Runtime(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            calls.append(task)
            if task == "review" and fail_review:
                raise ValueError("unusable review response")
            return await super().complete(task, context, schema, session_key)

    class FatalReviewEngine(Engine):
        async def global_review(self, states):
            # Exercise a fatal downstream call after body checkpoints exist.
            # Ordinary consistency checks deliberately preserve an unchecked
            # draft on model errors, so they are not a failed-job fixture.
            result = await self._call(
                "review", {"sections": [state.model_dump() for state in states]}, Review,
            )
            return result.findings

    monkeypatch.setattr("folio.service.DemoRuntime", Runtime)
    monkeypatch.setattr("folio.service.Engine", FatalReviewEngine)
    with pytest.raises(ValueError, match="unusable review response"):
        await execute(store, demo_job)
    failed = store.get(demo_job)
    assert failed["status"] == "failed"
    before = [s.model_dump() for s in store.states(demo_job)]
    assert before
    writing_calls = calls.count("generate")
    fail_review = False
    resumed = store.resume_job(demo_job)
    assert resumed["calls"] == failed["calls"]
    assert [s.model_dump() for s in store.states(demo_job)] == before
    result = await execute(store, demo_job)
    assert result["status"] == "draft" and result["artifact"]
    assert calls.count("generate") == writing_calls
    with store.connect() as conn:
        review = conn.execute(
            "SELECT attempts,result FROM tasks WHERE job_id=? AND task_key LIKE 'review:%'",
            (demo_job,),
        ).fetchone()
    assert review["attempts"] == 3 and review["result"]


async def test_shutdown_cancellation_still_propagates(store, demo_job, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()

    class Runtime:
        async def aclose(self):
            closed.set()

    class Engine:
        def __init__(self, *args):
            pass

        async def run(self, job_id, **kwargs):
            store.start_execution(job_id)
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("folio.service.Engine", Engine)
    monkeypatch.setattr("folio.service.DemoRuntime", Runtime)
    worker = asyncio.create_task(execute(store, demo_job))
    await started.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert closed.is_set()
    # Ordinary process shutdown retains automatic recovery, unlike a user pause.
    assert store.get(demo_job)["status"] == "running"


async def test_engine_initialization_failure_still_closes_provider(store, demo_job, monkeypatch):
    closed = []

    class Runtime:
        async def aclose(self):
            closed.append(True)

    class Engine:
        def __init__(self, *args):
            raise ValueError("invalid optional service configuration")

    monkeypatch.setattr("folio.service.DemoRuntime", Runtime)
    monkeypatch.setattr("folio.service.Engine", Engine)
    with pytest.raises(ValueError, match="invalid optional"):
        await execute(store, demo_job)
    assert closed == [True]
