"""Optional figures yield a durable allowance to writing and required reviews."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_callscheduling import BlockingRuntime, call, cancel
from test_request_context import engine_for

from folio.models import Review
from folio.runtime.base import RuntimeResult
from folio.runtime.call_budget import (
    CallBudgetPolicy,
    VisualPrefetchDeferred,
    call_budget_scope,
)
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallBudgetExceeded, ModelCallFailed
from folio.store import Store


def test_parallel_reservations_cannot_overspend_optional_bucket(store, demo_job):
    policy = CallBudgetPolicy(bucket="visual_prefetch:100", max_calls=3, reserve_calls=25)

    def reserve(index):
        try:
            store.begin_task(demo_job, f"visual-{index}", 100, budget_policy=policy)
        except VisualPrefetchDeferred:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(16)))
    assert sum(results) == 3
    assert store.get(demo_job)["calls"] == 3
    assert sum(store.task(demo_job, f"visual-{i}") is not None for i in range(16)) == 3

    restored = Store(store.root)
    with pytest.raises(VisualPrefetchDeferred) as caught:
        restored.begin_task(demo_job, "after-restart", 100, budget_policy=policy)
    assert caught.value.used == caught.value.limit == 3
    assert restored.get(demo_job)["calls"] == 3
    assert restored.task(demo_job, "after-restart") is None
    assert restored.begin_task(demo_job, "required-review", 100) == 1
    assert restored.get(demo_job)["calls"] == 4


def test_optional_work_cannot_consume_reserved_core_calls(store, demo_job):
    policy = CallBudgetPolicy(bucket="visual_prefetch:10", max_calls=10, reserve_calls=3)
    for index in range(7):
        store.begin_task(demo_job, f"core-{index}", 10)
    with pytest.raises(VisualPrefetchDeferred) as caught:
        store.begin_task(demo_job, "optional", 10, budget_policy=policy)
    assert caught.value.used == 0 and caught.value.calls == 7
    assert caught.value.reserve_calls == 3
    assert store.task(demo_job, "optional") is None
    for index in range(3):
        store.begin_task(demo_job, f"last-core-{index}", 10)
    with pytest.raises(ModelCallBudgetExceeded):
        store.begin_task(demo_job, "beyond-total", 10)
    assert store.get(demo_job)["calls"] == 10


async def test_engine_retries_use_soft_budget_but_cache_and_final_review_do_not(store, inputs):
    class RetryRuntime(DemoRuntime):
        def __init__(self):
            self.calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            if self.calls == 1:
                raise ModelCallFailed("temporary failure", "rate_limit")
            return RuntimeResult('{"findings": []}')

    runtime = RetryRuntime()
    engine = engine_for(store, inputs, runtime)
    engine.limits.call_attempts = 3
    policy = CallBudgetPolicy(bucket="visual_prefetch:2000", max_calls=1, reserve_calls=500)
    context = {"sections": []}
    with call_budget_scope(policy), pytest.raises(VisualPrefetchDeferred):
        await engine._call("review", context, Review)
    assert runtime.calls == store.get(engine.job_id)["calls"] == 1
    started = [e for e in store.events(engine.job_id) if e["kind"] == "task_started"]
    assert len(started) == 1

    # The final, required caller resumes the same failed request against the
    # main allowance; a subsequent cache hit costs neither budget any calls.
    assert await engine._call("review", context, Review) == Review()
    assert runtime.calls == store.get(engine.job_id)["calls"] == 2
    with call_budget_scope(policy):
        assert await engine._call("review", context, Review) == Review()
    assert runtime.calls == store.get(engine.job_id)["calls"] == 2


async def test_background_figures_do_not_take_core_concurrency_slots(store, inputs):
    runtime = BlockingRuntime(decision="absent")
    engine = engine_for(store, inputs, runtime)
    policy = CallBudgetPolicy(bucket="visual_prefetch:2000", max_calls=120, reserve_calls=500)
    with call_budget_scope(policy):
        optional = [call(engine, "review", index) for index in range(4)]
    core = []
    try:
        await runtime.started(2)
        assert runtime.main_active == 2
        core = [call(engine, "review", index + 10) for index in range(16)]
        await runtime.started(16)
        assert runtime.main_active == 18
        assert runtime.entered.empty()
        runtime.main_release.set()
        await asyncio.wait_for(asyncio.gather(*optional, *core), 3)
    finally:
        await cancel([*optional, *core])
