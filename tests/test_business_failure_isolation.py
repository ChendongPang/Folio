"""Keep unrelated business checkpoints when one model request cannot finish."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_business_work import document, task

from folio.models import JobInput
from folio.orchestration.agents import BusinessCoordinator, BusinessResult
from folio.orchestration.work import WorkPlan, WorkState
from folio.runtime.errors import ModelCallBudgetExceeded, ModelCallFailed


def coordinator_host(store, inputs, monkeypatch):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    host = SimpleNamespace(
        store=store, job_id=job_id, plan=document(("a", "b", "c", "d")),
        limits=SimpleNamespace(max_parallel_tasks=2),
        prepare_work_plan=AsyncMock(),
        work_plan=WorkPlan(tasks=[
            task("first", ["a"]), task("second", ["b"]),
            task("queued", ["c"]), task("dependent", ["d"], ["second"]),
        ]),
    )
    failure_observed = asyncio.Event()
    original = store.work_event

    def work_event(job_id, text, **kwargs):
        original(job_id, text, **kwargs)
        if "暂时失败" in text:
            failure_observed.set()

    monkeypatch.setattr(store, "work_event", work_event)
    return host, failure_observed


def save_result(host, work_task):
    key = "review-" + work_task.task_id
    host.store.save_work_state(host.job_id, WorkState(
        task_id=work_task.task_id, researched=True, writable=True, review_key=key,
        review_revisions=dict.fromkeys(work_task.section_ids, 1),
    ))
    return BusinessResult(task_id=work_task.task_id, review_key=key,
                          section_revisions=dict.fromkeys(work_task.section_ids, 1))


def attributed_validation():
    error = ValueError("Invalid model citation")
    error.folio_failure_context = {
        "task": "understand_work", "task_key": "first-request", "code": "validation_error",
    }
    return error


@pytest.mark.parametrize("error", [
    ModelCallFailed("Timed out", "timeout"),
    ModelCallFailed("Model HTTP 502", "model_error"),
    ModelCallFailed("Incomplete result", "length_limit"),
    TimeoutError(), httpx.ReadTimeout("read timed out"), attributed_validation(),
])
async def test_local_failure_drains_independent_worker_and_stops_new_tasks(
    store, inputs, monkeypatch, error,
):
    host, failed = coordinator_host(store, inputs, monkeypatch)
    started, finish = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def execute(work_task):
        calls.append(work_task.task_id)
        if work_task.task_id == "first":
            await started.wait()
            raise error
        assert work_task.task_id == "second"
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return save_result(host, work_task)

    host.execute_business_task = execute
    runner = asyncio.create_task(BusinessCoordinator(host).run())
    try:
        await asyncio.wait_for(failed.wait(), timeout=2)
        assert not runner.done() and not cancelled.is_set()
        finish.set()
        with pytest.raises(type(error)) as observed:
            await asyncio.wait_for(runner, timeout=2)
        assert observed.value is error
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert calls == ["first", "second"]
    assert not cancelled.is_set()
    assert store.work_states(host.job_id)["second"].review_key == "review-second"
    failures = [event for event in store.events(host.job_id) if event["kind"] == "business_task_failed"]
    assert len(failures) == 1 and failures[0]["data"]["task_id"] == "first"


async def test_later_failure_does_not_replace_first_failure_context(store, inputs, monkeypatch):
    host, failed = coordinator_host(store, inputs, monkeypatch)
    started = asyncio.Event()
    first = ModelCallFailed("First timeout", "timeout")
    first.folio_failure_context = {"task": "complete_gaps", "task_key": "first-request"}
    second = attributed_validation()
    second.folio_failure_context["task_key"] = "second-request"

    async def execute(work_task):
        if work_task.task_id == "first":
            await started.wait()
            raise first
        started.set()
        await failed.wait()
        raise second

    host.execute_business_task = execute
    with pytest.raises(ModelCallFailed) as observed:
        await asyncio.wait_for(BusinessCoordinator(host).run(), timeout=2)
    assert observed.value is first
    assert first.folio_failure_context == {"task": "complete_gaps", "task_key": "first-request"}
    failures = [event["data"] for event in store.events(host.job_id) if event["kind"] == "business_task_failed"]
    assert [item["failure_context"]["task_key"] for item in failures] == ["first-request", "second-request"]


@pytest.mark.parametrize("error", [
    ModelCallFailed("Credentials rejected", "authentication"),
    ModelCallFailed("Configuration invalid", "configuration"),
    ModelCallFailed("Billing unavailable", "billing"),
    ModelCallFailed("Schema unsupported", "schema_unsupported"),
    ModelCallBudgetExceeded(3500, 3500),
    ValueError("Invalid assignment"), OSError("Storage failed"),
])
async def test_global_or_internal_failure_still_cancels_other_workers(
    store, inputs, monkeypatch, error,
):
    host, _ = coordinator_host(store, inputs, monkeypatch)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def execute(work_task):
        if work_task.task_id == "first":
            await started.wait()
            raise error
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    host.execute_business_task = execute
    with pytest.raises(type(error)) as observed:
        await asyncio.wait_for(BusinessCoordinator(host).run(), timeout=2)
    assert observed.value is error
    assert cancelled.is_set()
    assert not store.work_states(host.job_id)


async def test_user_cancellation_still_interrupts_draining_workers(store, inputs, monkeypatch):
    host, failed = coordinator_host(store, inputs, monkeypatch)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def execute(work_task):
        if work_task.task_id == "first":
            await started.wait()
            raise ModelCallFailed("Timed out", "timeout")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    host.execute_business_task = execute
    runner = asyncio.create_task(BusinessCoordinator(host).run())
    await asyncio.wait_for(failed.wait(), timeout=2)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert cancelled.is_set()


async def test_unreviewed_result_cannot_release_a_dependency(store, inputs, monkeypatch):
    host, _ = coordinator_host(store, inputs, monkeypatch)
    host.limits.max_parallel_tasks = 1
    host.work_plan = WorkPlan(tasks=[task("first", ["a"]), task("dependent", ["b"], ["first"])])
    calls = []

    async def execute(work_task):
        calls.append(work_task.task_id)
        return BusinessResult(task_id=work_task.task_id, review_key="", section_revisions={"a": 1})

    host.execute_business_task = execute
    with pytest.raises(ValueError, match="reviewed result"):
        await BusinessCoordinator(host).run()
    assert calls == ["first"]


async def test_successful_dependency_still_runs_after_reviewed_receipt(store, inputs, monkeypatch):
    host, _ = coordinator_host(store, inputs, monkeypatch)
    host.work_plan = WorkPlan(tasks=[task("first", ["a"]), task("dependent", ["b"], ["first"])])
    calls = []

    async def execute(work_task):
        calls.append(work_task.task_id)
        if work_task.task_id == "dependent":
            assert store.work_states(host.job_id)["first"].review_key == "review-first"
        return save_result(host, work_task)

    host.execute_business_task = execute
    await BusinessCoordinator(host).run()
    assert calls == ["first", "dependent"]
