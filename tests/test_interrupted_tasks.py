"""Interrupted provider calls keep total cost without consuming failure retries."""

import asyncio

import pytest

from folio.models import JobInput, Limits, Review
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult


class Outcomes:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def complete(self, *args):
        self.calls += 1
        outcome = next(self.outcomes)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "error":
            raise ValueError("actual provider failure")
        return RuntimeResult('{"findings":[]}', usage={"total_tokens": 7})


def resumed(store, job_id, runtime):
    engine = Engine(store, runtime)
    engine.job_id = job_id
    engine.request = JobInput.model_validate(store.get(job_id)["input"])
    engine.limits = engine.request.limits
    return engine


async def review(engine):
    return await engine._call("review", {"sections": [{"section_id": "s"}]}, Review)


def only_task(store, job_id):
    with store.connect() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE job_id=?", (job_id,)).fetchone())


async def test_two_interruptions_resume_without_spending_failure_retries(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=1)))
    runtime = Outcomes(["cancel", "cancel", "ok"])
    for count in (1, 2):
        with pytest.raises(asyncio.CancelledError):
            await review(resumed(store, job_id, runtime))
        task = only_task(store, job_id)
        assert task["attempts"] == 1 and task["result"] is None and task["error"] is None
        assert store.get(job_id)["calls"] == count
    assert (await review(resumed(store, job_id, runtime))).findings == []
    assert runtime.calls == 3 and store.get(job_id)["calls"] == 3
    task = only_task(store, job_id)
    assert task["attempts"] == 1 and task["result"] and task["error"] is None
    events = store.events(job_id)
    assert sum(e["kind"] == "task_started" for e in events) == 3
    assert sum(e["kind"] == "task_interruption_recovered" for e in events) == 2
    assert (await review(resumed(store, job_id, runtime))).findings == []
    assert runtime.calls == 3  # A completed cache is never recovered/replayed.


async def test_recovery_keeps_prior_real_failure_consumed(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=2)))
    runtime = Outcomes(["error", "cancel", "ok"])
    with pytest.raises(asyncio.CancelledError):
        await review(resumed(store, job_id, runtime))
    assert only_task(store, job_id)["attempts"] == 2
    assert (await review(resumed(store, job_id, runtime))).findings == []
    assert only_task(store, job_id)["attempts"] == 2
    assert store.get(job_id)["calls"] == runtime.calls == 3
    assert sum(e["kind"] == "task_failed" for e in store.events(job_id)) == 1


async def test_real_failure_exhaustion_is_not_reset_on_resume(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=2)))
    runtime = Outcomes(["error", "error", "ok"])
    with pytest.raises(ValueError, match="actual provider failure"):
        await review(resumed(store, job_id, runtime))
    before = only_task(store, job_id)
    assert before["attempts"] == 2 and before["error"]
    with pytest.raises(RuntimeError, match="retries exhausted"):
        await review(resumed(store, job_id, runtime))
    assert only_task(store, job_id) == before
    assert store.get(job_id)["calls"] == runtime.calls == 2
    assert not any(e["kind"] == "task_interruption_recovered" for e in store.events(job_id))


async def test_explicit_failed_resume_grants_a_bounded_retry_window(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=2)))
    runtime = Outcomes(["error", "error", "error", "error", "ok"])
    for total in (2, 4):
        with pytest.raises(ValueError, match="actual provider failure"):
            await review(resumed(store, job_id, runtime))
        assert only_task(store, job_id)["attempts"] == total
        with pytest.raises(RuntimeError, match="retries exhausted"):
            await review(resumed(store, job_id, runtime))
        assert runtime.calls == total
        store.update(job_id, status="failed", error="actual provider failure")
        store.resume_job(job_id)
        store.resume_job(job_id)  # Duplicate controls cannot accumulate grants.
    assert (await review(resumed(store, job_id, runtime))).findings == []
    assert only_task(store, job_id)["attempts"] == 5
    assert store.get(job_id)["calls"] == runtime.calls == 5
    events = store.events(job_id)
    assert sum(e["kind"] == "task_failed" for e in events) == 4
    assert sum(e["kind"] == "task_retry_authorized" for e in events) == 2


@pytest.mark.parametrize("status", ["failed", "paused"])
async def test_explicit_resume_does_not_reset_global_budget(store, inputs, status):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=2, max_calls=2)))
    runtime = Outcomes(["error", "error", "ok"])
    with pytest.raises(ValueError, match="actual provider failure"):
        await review(resumed(store, job_id, runtime))
    store.update(job_id, status=status, error="actual provider failure")
    store.resume_job(job_id)
    with pytest.raises(RuntimeError, match="model-call budget exhausted"):
        await review(resumed(store, job_id, runtime))
    assert only_task(store, job_id)["attempts"] == 2
    assert store.get(job_id)["calls"] == runtime.calls == 2


@pytest.mark.parametrize("status", ["failed", "paused"])
async def test_explicit_resume_keeps_research_bucket_spending(store, inputs, status):
    from folio.runtime.call_budget import (
        CallBudgetPolicy,
        ResearchModelDeferred,
        call_budget_scope,
    )

    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=1)))
    runtime = Outcomes(["error", "ok"])
    policy = CallBudgetPolicy(bucket="research:t1", max_calls=1, purpose="research")
    with call_budget_scope(policy):
        with pytest.raises(ValueError, match="actual provider failure"):
            await review(resumed(store, job_id, runtime))
        store.update(job_id, status=status, error="actual provider failure")
        store.resume_job(job_id)
        with pytest.raises(ResearchModelDeferred):
            await review(resumed(store, job_id, runtime))
    assert store.call_budget_usage(job_id, policy.bucket) == 1
    assert store.get(job_id)["calls"] == runtime.calls == 1


async def test_explicit_paused_resume_retries_failed_siblings_without_resetting_spend(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=1)))
    runtime = Outcomes(["error", "error", "ok"])
    with pytest.raises(ValueError, match="actual provider failure"):
        await review(resumed(store, job_id, runtime))
    store.update(job_id, status="failed", error="actual provider failure")
    store.resume_job(job_id)
    with pytest.raises(ValueError, match="actual provider failure"):
        await review(resumed(store, job_id, runtime))
    store.request_pause(job_id)
    store.resume_job(job_id)
    store.resume_job(job_id)
    assert (await review(resumed(store, job_id, runtime))).findings == []
    assert only_task(store, job_id)["attempts"] == 3
    assert store.get(job_id)["calls"] == runtime.calls == 3
    assert sum(e["kind"] == "task_retry_authorized" for e in store.events(job_id)) == 2


def test_store_recovers_exactly_once_without_resetting_history(store, demo_job):
    key = "review:interrupted"
    store.begin_task(demo_job, key, 10)
    store.finish_task(demo_job, key, error="real failure")
    assert store.recover_interrupted_task(demo_job, key)["attempts"] == 1
    store.begin_task(demo_job, key, 10)
    assert store.recover_interrupted_task(demo_job, key)["attempts"] == 1
    assert store.recover_interrupted_task(demo_job, key)["attempts"] == 1
    assert store.get(demo_job)["calls"] == 2
    events = store.events(demo_job)
    assert sum(e["kind"] == "task_started" for e in events) == 2
    assert sum(e["kind"] == "task_failed" for e in events) == 1
    assert sum(e["kind"] == "task_interruption_recovered" for e in events) == 1


async def test_interruption_recovery_cannot_bypass_global_call_budget(store, inputs):
    job_id = store.create(JobInput(**inputs, limits=Limits(call_attempts=1, max_calls=2)))
    runtime = Outcomes(["cancel", "cancel", "ok"])
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            await review(resumed(store, job_id, runtime))
    with pytest.raises(RuntimeError, match="model-call budget exhausted"):
        await review(resumed(store, job_id, runtime))
    assert store.get(job_id)["calls"] == runtime.calls == 2
