"""Research tool quota admission stays atomic and retry-safe."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from native_calls import research_call
from test_callscheduling import BlockingRuntime, call, cancel
from test_request_context import engine_for

from folio.research.contracts import ResearchBatch, ResearchConfig, ResearchStep
from folio.research.ledger import ResearchLedger, ResearchLimitReached
from folio.research.workflow import ResearchTools
from folio.runtime.base import RuntimeResult
from folio.runtime.call_budget import (
    CallBudgetPolicy,
    ResearchModelDeferred,
    call_budget_scope,
    current_call_budget,
)
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed
from folio.store import Store


@pytest.fixture
def ledger(tmp_path):
    return ResearchLedger(tmp_path / "research" / "ledger.sqlite")


def config(*, calls=50, seconds=600):
    return SimpleNamespace(max_tool_calls=calls, max_execution_seconds=seconds)


def begin(ledger, request_id="request", *, task="business", round_id="round", step=0,
          tool="sandbox.exec", arguments=None, reserve=10, limits=None):
    return ledger.begin(
        request_id, task, round_id, step, tool, arguments or {"code": "print(1)"},
        config=limits or config(), reserve=reserve,
    )


def test_concurrent_requests_cannot_overspend_call_allowance(ledger):
    barrier = Barrier(12)

    def compete(index):
        barrier.wait()
        try:
            begin(ledger, f"call-{index}", reserve=0, limits=config(calls=3))
            return True
        except ResearchLimitReached:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        result = list(pool.map(compete, range(12)))
    assert sum(result) == 3
    assert ledger.usage("business") == {"calls": 3, "seconds": 0}


def test_running_reservations_atomically_prevent_parallel_time_overspend(ledger):
    barrier = Barrier(12)

    def compete(index):
        barrier.wait()
        try:
            begin(ledger, f"seconds-{index}", reserve=7, limits=config(seconds=10))
            return True
        except ResearchLimitReached:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        result = list(pool.map(compete, range(12)))
    assert sum(result) == 1
    assert ledger.usage("business") == {"calls": 1, "seconds": 7}


def test_duplicate_concurrent_admission_spends_once(ledger):
    barrier = Barrier(8)

    def compete(_):
        barrier.wait()
        return begin(ledger, reserve=10, limits=config(calls=1, seconds=10))

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(compete, range(8)))
    assert sum(result) == 1
    assert ledger.usage("business") == {"calls": 1, "seconds": 10}


@pytest.mark.parametrize("changes", [
    {"tool": "fs.read"}, {"arguments": {"code": "print(2)"}},
    {"task": "other-business"}, {"round_id": "other-round"}, {"step": 1},
])
def test_execution_identity_cannot_be_reassigned(ledger, changes):
    assert begin(ledger)
    with pytest.raises(ValueError, match="identity"):
        begin(ledger, **changes)
    assert ledger.usage("business") == {"calls": 1, "seconds": 10}


def test_finished_execution_releases_unused_reserved_time(ledger):
    begin(ledger, reserve=10, limits=config(seconds=10))
    with pytest.raises(ResearchLimitReached):
        begin(ledger, "another", reserve=1, limits=config(seconds=10))
    ledger.finish("request", {"status": "completed", "stdout": "1"}, elapsed=2)
    assert ledger.usage("business") == {"calls": 1, "seconds": 2}
    assert begin(ledger, "another", reserve=8, limits=config(seconds=10))
    assert ledger.usage("business") == {"calls": 2, "seconds": 10}


def test_repeated_completion_never_rewrites_result_or_usage(ledger):
    begin(ledger)
    result = {"status": "completed", "stdout": "first result"}
    ledger.finish("request", result, elapsed=3)
    ledger.finish("request", {"status": "failed"}, elapsed=99, status="failed")
    recovered = ResearchLedger(ledger.path)
    record = recovered.get("request")
    assert record["status"] == "completed"
    assert record["result"] == result
    assert recovered.usage("business") == {"calls": 1, "seconds": 3}


def test_interrupted_recovery_replays_identity_without_renewing_allowance(ledger):
    limits = config(calls=1, seconds=10)
    begin(ledger, reserve=10, limits=limits)
    recovered = ResearchLedger(ledger.path)
    assert not begin(recovered, reserve=10, limits=limits)
    assert recovered.usage("business") == {"calls": 1, "seconds": 10}
    result = {"status": "interrupted", "message": "The execution did not commit."}
    recovered.finish("request", result, elapsed=10, status="interrupted")
    assert not begin(recovered, reserve=10, limits=limits)
    assert recovered.get("request")["result"] == result
    assert recovered.usage("business") == {"calls": 1, "seconds": 10}
    with pytest.raises(ResearchLimitReached):
        begin(recovered, "fresh-request", reserve=1, limits=limits)


def test_independent_task_allowances_and_round_accounting(ledger):
    limits = config(calls=1, seconds=10)
    begin(ledger, "task-a-request", task="task-a", limits=limits)
    assert begin(ledger, "task-b-request", task="task-b", limits=limits)
    with pytest.raises(ResearchLimitReached):
        begin(ledger, "new-round", task="task-a", round_id="new-round", limits=limits)
    assert ledger.usage("task-a") == ledger.usage("task-b") == {"calls": 1, "seconds": 10}


def test_session_rounds_are_persistent_and_isolated(ledger):
    initial = ledger.load_session("task", "round-a")
    assert initial["step"] == 0 and not initial["finished"]
    value = initial | {"step": 3, "notes": "already inspected", "pending_step": {"tool": "fs.read"}}
    ledger.save_session("task", "round-a", value)
    recovered = ResearchLedger(ledger.path)
    assert recovered.load_session("task", "round-a") == value
    assert recovered.load_session("task", "round-b") == initial
    assert recovered.load_session("another-task", "round-a") == initial


def test_parallel_research_model_admission_preserves_core_reserve(store, demo_job):
    for index in range(6):
        store.begin_task(demo_job, f"earlier-{index}", 10)
    barrier = Barrier(12)

    def compete(index):
        barrier.wait()
        policy = CallBudgetPolicy(
            bucket=f"business_research:task-{index}", max_calls=20,
            reserve_calls=3, purpose="research",
        )
        try:
            store.begin_task(demo_job, f"research-{index}", 10, budget_policy=policy)
            return True
        except ResearchModelDeferred:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(compete, range(12))) == 1
    assert store.get(demo_job)["calls"] == 7
    for index in range(3):
        store.begin_task(demo_job, f"required-writing-{index}", 10)
    assert store.get(demo_job)["calls"] == 10


def test_research_model_bucket_survives_restart_and_counts_retry_attempts(store, demo_job):
    policy = CallBudgetPolicy(bucket="business_research:owner", max_calls=2, purpose="research")
    assert store.begin_task(demo_job, "same-request", 100, budget_policy=policy) == 1
    assert store.begin_task(demo_job, "same-request", 100, budget_policy=policy) == 2
    restored = Store(store.root)
    with pytest.raises(ResearchModelDeferred) as caught:
        restored.begin_task(demo_job, "same-request", 100, budget_policy=policy)
    assert caught.value.used == 2
    assert restored.call_budget_usage(demo_job, policy.bucket) == 2
    assert restored.task(demo_job, "same-request")["attempts"] == 2
    assert restored.get(demo_job)["calls"] == 2


async def test_research_wrapper_counts_actual_retries_and_allows_exhausted_cache(store, inputs, ledger):
    class RetryRuntime(DemoRuntime):
        def __init__(self):
            self.calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            if self.calls == 1:
                raise ModelCallFailed("temporary service failure", "rate_limit")
            return research_call("finish", notes="done")

    runtime = RetryRuntime()
    engine = engine_for(store, inputs, runtime)
    engine.limits.call_attempts = 3
    tools = ResearchTools.__new__(ResearchTools)
    tools.engine, tools.ledger = engine, ledger
    tools.config = ResearchConfig(max_model_calls=1)
    tools.task = SimpleNamespace(task_id="owner")
    tools.session = {"round_id": "round", "quota_task_id": "owner", "model_calls": 0}
    context = {"step": 0, "task": "test"}
    with pytest.raises(ResearchLimitReached, match='body writing'):
        await tools.model("research_step", context, ResearchBatch)
    assert runtime.calls == store.get(engine.job_id)["calls"] == 1
    assert tools.session["model_calls"] == 1
    assert ledger.load_session("owner", "round")["model_calls"] == 1
    assert (await engine.call("research_step", context, ResearchBatch)).steps[0].tool == "finish"
    assert runtime.calls == 2
    # The completed response can be recovered despite the exhausted research grant.
    assert (await tools.model("research_step", context, ResearchBatch)).steps[0].tool == "finish"
    assert runtime.calls == 2 and tools.session["model_calls"] == 1
    # A different research round keeps the same task allowance.
    tools.session["round_id"] = "writing-gap-round"
    with pytest.raises(ResearchLimitReached):
        await tools.model("research_step", {"step": 1, "task": "new-gap"}, ResearchBatch)
    assert runtime.calls == 2
    # An explicit continuation can have its own bounded grant.
    tools.session["quota_task_id"] = "owner:human-confirmation"
    assert (await tools.model("research_step", {"step": 1, "task": "new-gap"}, ResearchBatch)).steps[0].tool == "finish"
    assert runtime.calls == 3 and tools.session["model_calls"] == 1


async def test_research_does_not_enter_optional_visual_concurrency_slots(store, inputs):
    runtime = BlockingRuntime(decision="absent")
    engine = engine_for(store, inputs, runtime)
    policy = CallBudgetPolicy(bucket="business_research:owner", max_calls=20, purpose="research")
    with call_budget_scope(policy):
        tasks = [call(engine, "review", index) for index in range(3)]
    try:
        await runtime.started(3)
        assert runtime.main_active == 3
        assert engine._prefetch_model_slots._value == 2
        runtime.main_release.set()
        for task in tasks:
            await task
    finally:
        await cancel(tasks)


async def test_local_agent_cannot_bypass_coordinator_web_phase(store, inputs, ledger, monkeypatch):
    class CountingRuntime(DemoRuntime):
        def __init__(self):
            self.calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            return RuntimeResult('{"tool":"finish","notes":"done"}')

    runtime = CountingRuntime()
    engine = engine_for(store, inputs, runtime)
    tools = ResearchTools.__new__(ResearchTools)
    tools.engine, tools.ledger = engine, ledger
    tools.config = ResearchConfig(max_model_calls=1)
    tools.task = SimpleNamespace(task_id="owner")
    tools.work = SimpleNamespace(evidence=[], missing=[])
    tools.session = {"round_id": "round", "quota_task_id": "owner", "model_calls": 0}

    async def nested_web_models(task, work):
        for index in range(2):
            await engine.call("research_step", {"step": index, "task": "nested-web"}, ResearchBatch)

    monkeypatch.setattr(engine, "web_search_available", lambda work: True)
    monkeypatch.setattr(engine, "supplement_web", nested_web_models)
    step = ResearchStep(tool="fs.search", arguments_json='{"scope":"web","query":"official documentation"}')
    with pytest.raises(ValueError, match='cannot search the web early'):
        await tools.dispatch(step, "web-request", 0)
    assert runtime.calls == store.get(engine.job_id)["calls"] == 0
    assert tools.session["model_calls"] == 0
    assert current_call_budget.get() is None


def test_subsecond_operator_execution_limit_is_rejected_before_running():
    with pytest.raises(ValueError):
        ResearchConfig(max_seconds_per_execution=0.5)
