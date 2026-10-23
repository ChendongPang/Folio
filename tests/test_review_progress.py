"""Progress reports actual per-group work without changing review or document content."""

import asyncio
import json
from collections import Counter

import pytest
from test_parallel_review import material

from folio.models import Finding, JobInput, Limits
from folio.orchestration.engine import Engine
from folio.orchestration.review import review_groups
from folio.orchestration.work import WorkState
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


def configured_engine(store, inputs, runtime, *, width=2, attempts=2):
    engine = Engine(store, runtime)
    engine.request = JobInput(
        **inputs,
        limits=Limits(max_parallel_reviews=2, review_batch_size=width, call_attempts=attempts),
    )
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan, engine.work_plan, states = material()
    store.save_plan(engine.job_id, engine.plan)
    store.save_work_plan(engine.job_id, engine.work_plan)
    for state in states:
        store.save_section(engine.job_id, state)
    for task in engine.work_plan.tasks:
        store.save_work_state(engine.job_id, WorkState(task_id=task.task_id))
    return engine, states


def review_events(engine, *, after=0, phase="consistency"):
    return [
        event["data"]["review"]
        for event in engine.store.events(engine.job_id, after)
        if event["data"].get("review", {}).get("phase") == phase
    ]


def completed_units(events):
    return [event for event in events if event["status"] == "completed" and "unit_id" in event]


def summary(events):
    return next(
        event for event in events if event["status"] == "summary" and event["step"] == "completed"
    )


def test_reasons_describe_real_rules_without_changing_groups():
    plan, work, states = material()
    states[0].extraction.entities = ["Approval", "交易"]
    states[1].extraction.entities = ["approval"]
    states[3].extraction.entities = ["交易"]
    groups = review_groups(plan, work, states, 2)
    assert [[state.section_id for state in group.sections] for group in groups] == [
        ["a", "b"],
        ["b", "c"],
        ["c", "d"],
        ["a", "d"],
    ]
    assert len(groups[0].reasons) == 2
    assert any("Adjacent" in reason for reason in groups[0].reasons)
    assert any("approval" in reason for reason in groups[0].reasons)
    assert len(groups[-1].reasons) == 1 and "交易" in groups[-1].reasons[0]


async def test_concurrent_group_calls_failures_and_second_run_cache_are_attributed(store, inputs):
    class Runtime(DemoRuntime):
        def __init__(self):
            self.counts = Counter()
            self.active = self.peak = 0
            self.overlap = asyncio.Event()

        async def complete(self, task, context, schema, session):
            assert task == "review"
            pair = tuple(section["section_id"] for section in context["sections"])
            self.counts[pair] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.overlap.set()
            try:
                await asyncio.wait_for(self.overlap.wait(), 1)
                await asyncio.sleep(0)
                if pair == ("a", "b") and self.counts[pair] == 1:
                    return RuntimeResult("{invalid-json")
                return RuntimeResult('{"findings":[]}')
            finally:
                self.active -= 1

    runtime = Runtime()
    engine, states = configured_engine(store, inputs, runtime)
    original = [state.model_dump() for state in states]
    assert await engine.global_review(states) == []
    assert runtime.peak == 2 and sum(runtime.counts.values()) == 4
    first = review_events(engine)
    by_sections = {
        tuple(section["section_id"] for section in event["sections"]): event
        for event in completed_units(first)
    }
    assert set(by_sections) == {("a", "b"), ("b", "c"), ("c", "d")}
    for pair, event in by_sections.items():
        expected = 2 if pair == ("a", "b") else 1
        assert event["calls"]["model_calls"] == expected
        assert event["calls"]["failed_calls"] == expected - 1
        assert event["calls"]["cache_hits"] == 0
        assert event["calls"]["tasks"]["review"]["model_calls"] == expected
        assert event["reasons"] and event["elapsed_ms"] >= 0
        assert all(section["title"] and section["revision"] == 1 for section in event["sections"])
    assert summary(first)["calls"]["model_calls"] == 4
    assert summary(first)["calls"]["failed_calls"] == 1
    trace = engine.store.root / "jobs" / engine.job_id / "review.jsonl"
    assert "完整模型建议" not in trace.read_text()

    after = engine.store.events(engine.job_id)[-1]["id"]
    assert await engine.global_review(states) == []
    second = review_events(engine, after=after)
    assert sum(runtime.counts.values()) == 4
    assert summary(second)["calls"]["model_calls"] == 0
    assert summary(second)["calls"]["cache_hits"] == 3
    assert all(
        event["calls"]["model_calls"] == 0 and event["calls"]["cache_hits"] == 1
        for event in completed_units(second)
    )
    assert summary(first)["round_id"] != summary(second)["round_id"]
    assert [state.model_dump() for state in states] == original


async def test_capacity_split_group_reports_mixed_cache_and_new_child_calls(store, inputs):
    class PairCapacity(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def measure(self, task, context, schema, limit):
            assert task == "review"
            return {"characters": 100, "fits": len(context["sections"]) <= 2}

        async def complete(self, task, context, schema, session):
            assert task == "review"
            self.calls.append(tuple(section["section_id"] for section in context["sections"]))
            return RuntimeResult('{"findings":[]}')

    runtime = PairCapacity()
    engine, states = configured_engine(store, inputs, runtime, width=3)
    assert await engine.global_review(states) == []
    assert set(runtime.calls) == {("a", "b"), ("a", "c"), ("b", "c"), ("c", "d")}
    # Simulate one missing durable child checkpoint while keeping its siblings.
    requests = engine.store.root / "jobs" / engine.job_id / "contexts" / "requests"
    missing = next(
        record["task_key"]
        for path in requests.glob("*.json")
        if (record := json.loads(path.read_text()))["task"] == "review"
        and [section["section_id"] for section in record["context"]["sections"]] == ["a", "c"]
    )
    with engine.store.connect() as connection:
        connection.execute(
            "DELETE FROM tasks WHERE job_id=? AND task_key=?", (engine.job_id, missing)
        )
    after = engine.store.events(engine.job_id)[-1]["id"]
    runtime.calls.clear()
    assert await engine.global_review(states) == []
    assert runtime.calls == [("a", "c")]
    events = review_events(engine, after=after)
    groups = {
        tuple(section["section_id"] for section in event["sections"]): event
        for event in completed_units(events)
    }
    assert groups[("a", "b", "c")]["calls"]["model_calls"] == 1
    assert groups[("a", "b", "c")]["calls"]["cache_hits"] == 2
    assert groups[("c", "d")]["calls"]["model_calls"] == 0
    assert groups[("c", "d")]["calls"]["cache_hits"] == 1
    assert summary(events)["calls"]["model_calls"] == 1
    assert summary(events)["calls"]["cache_hits"] == 3


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_global_review_emits_no_success(store, inputs, cancel):
    class Interrupted(DemoRuntime):
        def __init__(self):
            self.ready = asyncio.Event()
            self.active = self.started = 0

        async def complete(self, task, context, schema, session):
            self.started += 1
            self.active += 1
            if self.active == 2:
                self.ready.set()
            try:
                await self.ready.wait()
                if not cancel and context["sections"][0]["section_id"] == "a":
                    raise ModelCallFailed("test failure", "authentication")
                await asyncio.Event().wait()
            finally:
                self.active -= 1

    runtime = Interrupted()
    engine, states = configured_engine(store, inputs, runtime, attempts=1)
    operation = asyncio.create_task(engine.global_review(states))
    await asyncio.wait_for(runtime.ready.wait(), 1)
    if cancel:
        operation.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ModelCallFailed):
        await operation
    assert runtime.active == 0 and runtime.started == 2
    events = review_events(engine)
    assert not completed_units(events)
    assert not any(event.get("step") == "completed" for event in events)
    endings = [event for event in events if event["status"] in {"failed", "cancelled"}]
    assert len(endings) == 2
    assert all(
        event["calls"]["model_calls"] == 1 and event["calls"]["cache_hits"] == 0
        for event in endings
    )
    if not cancel:
        failed = next(event for event in endings if event["status"] == "failed")
        assert failed["calls"]["failed_calls"] == 1
    assert all(event["outcome"] == "needs_confirmation" for event in endings)


async def test_adjudication_reports_each_verdict_and_preserves_all_saved_body(store, inputs):
    class Adjudicator(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def complete(self, task, context, schema, session):
            assert task == "drill_down"
            key = context["finding"]["key"]
            self.calls.append(key)
            await asyncio.sleep(0)
            if key == "service_error":
                raise ModelCallFailed("service unavailable", "authentication")
            return RuntimeResult(
                json.dumps(
                    {
                        "confirmed": key == "confirmed",
                        "needs_confirmation": key == "pending",
                        "reason": "需核对相关原文条件"
                        if key == "pending"
                        else "完整原文对照后的结论",
                        "affected_section_ids": context["finding"]["section_ids"]
                        if key == "confirmed"
                        else [],
                        "repair_instruction": "按已确认原文修改对应规则"
                        if key == "confirmed"
                        else "",
                    }
                )
            )

    runtime = Adjudicator()
    engine, states = configured_engine(store, inputs, runtime, attempts=1)
    original = [state.model_dump() for state in engine.store.states(engine.job_id)]
    findings = [
        Finding(
            kind="unsupported",
            key=key,
            description="待精查疑点：" + key,
            section_ids=[states[index % len(states)].section_id],
        )
        for index, key in enumerate(
            [
                "confirmed",
                "dismissed",
                "pending",
                "hard",
                "service_error",
            ]
        )
    ]
    hard = next(finding for finding in findings if finding.key == "hard")
    results = await engine.adjudicate_findings(findings, states, hard_keys={hard.fingerprint})
    assert [finding.key for finding, _, _ in results] == [finding.key for finding in findings]
    assert results[-1][1] is None and results[-1][2] == "ModelCallFailed"
    events = review_events(engine, phase="drill_down")
    finished = [event for event in events if "completed" in event and "unit_id" in event]
    assert len(finished) == 5
    by_key = {event["findings"][0]["key"]: event for event in finished}
    assert {key: event["outcome"] for key, event in by_key.items()} == {
        "confirmed": "confirmed",
        "dismissed": "dismissed",
        "pending": "needs_confirmation",
        "hard": "hard_check_retained",
        "service_error": "needs_confirmation",
    }
    assert by_key["service_error"]["status"] == "failed"
    assert "model_verdict" not in by_key["service_error"]
    assert by_key["service_error"]["calls"]["failed_calls"] == 1
    assert all(
        event["calls"]["model_calls"] == 1 and event["calls"]["cache_hits"] == 0
        for event in finished
    )
    final = summary(events)
    assert final["outcomes"] == {
        "confirmed": 1,
        "dismissed": 1,
        "needs_confirmation": 2,
        "hard_check_retained": 1,
    }
    assert final["calls"]["model_calls"] == 5 and final["calls"]["failed_calls"] == 1
    assert [state.model_dump() for state in states] == original
    assert [state.model_dump() for state in engine.store.states(engine.job_id)] == original
