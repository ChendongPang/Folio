import asyncio

import pytest
from test_business_work import document, task

from folio.models import (
    Block,
    Extraction,
    JobInput,
    Limits,
    Review,
    SectionContent,
    SectionState,
)
from folio.orchestration.engine import Engine
from folio.orchestration.review import parallel_reviews, review_groups
from folio.orchestration.work import WorkPlan
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


def material():
    plan = document(("a", "b", "c", "d"))
    work = WorkPlan(tasks=[task(s.section_id, [s.section_id]) for s in plan.sections])
    states = [
        SectionState(
            section_id=s.section_id,
            content=SectionContent(
                blocks=[Block(block_id="p", text="完整模型建议", origin="model_proposal")]
            ),
            extraction=Extraction(summary=s.title),
            evidence=[],
        )
        for s in plan.sections
    ]
    return plan, work, states


def test_proposals_alone_do_not_create_pairs_and_related_proposals_remain_readable():
    plan, work, states = material()
    groups = review_groups(plan, work, states, 2)
    assert [[s.section_id for s in group.sections] for group in groups] == [
        ["a", "b"],
        ["b", "c"],
        ["c", "d"],
    ]
    states[0].extraction.entities = ["Approval"]
    states[3].extraction.entities = ["approval"]
    groups = review_groups(plan, work, states, 2)
    assert len(groups) == 4
    assert [s.section_id for s in groups[-1].sections] == ["a", "d"]
    assert groups[-1].sections[0].content.blocks[0].text == "完整模型建议"
    # Siblings were already checked by business review; no cross-task pair for them.
    work = WorkPlan(tasks=[task("one", ["a", "d"]), task("two", ["b", "c"])])
    assert ["a", "d"] not in [
        [s.section_id for s in g.sections] for g in review_groups(plan, work, states, 2)
    ]


@pytest.mark.parametrize("limit", [1, 3])
async def test_pool_bounds_overlap_merges_stably_and_reports_progress(limit):
    active = peak = 0
    barrier = asyncio.Event()
    progress = []

    async def review(index):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == limit:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 1)
        await asyncio.sleep((6 - index) * 0.001)
        active -= 1
        return index

    result = await parallel_reviews(
        list(range(6)), limit, review, lambda *args: progress.append(args)
    )
    assert peak == limit and active == 0
    assert result == list(range(6))
    assert [p[0] for p in progress] == list(range(1, 7))
    assert {p[2] for p in progress} == set(range(1, 7))


async def test_failure_drains_workers_without_starting_remaining_groups():
    started = []
    waiting = asyncio.Event()
    drained = asyncio.Event()

    async def review(index):
        started.append(index)
        if index == 0:
            await waiting.wait()
            raise RuntimeError("review failed")
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    with pytest.raises(RuntimeError, match="review failed"):
        await parallel_reviews(list(range(6)), 2, review, lambda *args: None)
    assert started == [0, 1]
    assert drained.is_set()
    assert not any(t.get_name().startswith("review-worker-") for t in asyncio.all_tasks())


async def test_external_cancellation_drains_all_started_reviews():
    active = 0
    ready = asyncio.Event()

    async def review(index):
        nonlocal active
        active += 1
        if active == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    operation = asyncio.create_task(parallel_reviews(list(range(6)), 2, review, lambda *args: None))
    await asyncio.wait_for(ready.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert active == 0


async def test_global_review_uses_pool_and_reuses_durable_results(store, inputs):
    class Concurrent(DemoRuntime):
        def __init__(self):
            self.active = self.peak = self.calls = 0
            self.overlap = asyncio.Event()

        async def complete(self, name, context, schema, session):
            assert name == "review"
            assert all(
                s["model_proposals"][0]["text"] == "完整模型建议" for s in context["sections"]
            )
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.overlap.set()
            await asyncio.wait_for(self.overlap.wait(), 1)
            await asyncio.sleep(0.01)
            self.active -= 1
            return RuntimeResult('{"findings":[]}')

    runtime = Concurrent()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, limits=Limits(max_parallel_reviews=2, review_batch_size=2))
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan, engine.work_plan, states = material()
    assert await engine.global_review(states) == []
    assert runtime.peak == 2 and runtime.calls == 3
    assert await engine.global_review(states) == []
    assert runtime.calls == 3
    messages = [
        e["data"]["text"] for e in store.events(engine.job_id) if e["kind"] == "work_update"
    ]
    assert any("已完成 3/3 组" in m for m in messages)


async def test_identical_concurrent_requests_make_one_model_call(store, inputs):
    class Waiting(DemoRuntime):
        calls = 0

        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def complete(self, name, context, schema, session):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return RuntimeResult('{"findings":[]}')

    runtime = Waiting()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    context = {"sections": [{"section_id": "a"}, {"section_id": "b"}]}
    operation = asyncio.gather(*(engine._call("review", context, Review) for _ in range(3)))
    await asyncio.wait_for(runtime.started.wait(), 1)
    await asyncio.sleep(0)
    assert runtime.calls == 1
    runtime.release.set()
    results = await operation
    assert len(results) == 3 and runtime.calls == 1
    with store.connect() as conn:
        row = conn.execute(
            "SELECT attempts,result FROM tasks WHERE job_id=?", (engine.job_id,)
        ).fetchone()
        assert row["attempts"] == 1 and row["result"]


def test_review_preserves_long_comparison_explanations():
    from folio.models import Finding

    text = "Detailed comparison with relevant conditions. " * 100
    finding = Finding(kind="pending", key="comparison", description="Check", section_ids=["a"],
                      comparison_scope=text, incompatibility=text)
    restored = Review.model_validate_json(Review(findings=[finding]).model_dump_json())
    assert restored.findings[0].comparison_scope == text
    assert restored.findings[0].incompatibility == text
    properties = Finding.model_json_schema()["properties"]
    assert "maxLength" not in properties["comparison_scope"]
    assert "maxLength" not in properties["incompatibility"]


async def test_invalid_review_group_preserves_other_results_and_retries_only_failure(store, inputs):
    class InvalidGroup(DemoRuntime):
        invalid = True

        def __init__(self):
            self.calls = []

        async def complete(self, name, context, schema, session):
            group = tuple(s["section_id"] for s in context["sections"])
            self.calls.append(group)
            if self.invalid and group[0] == "a":
                return RuntimeResult('{"findings":[{"kind":"invalid"}]}')
            return RuntimeResult('{"findings":[]}')

    runtime = InvalidGroup()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, limits=Limits(max_parallel_reviews=2, review_batch_size=2))
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan, engine.work_plan, states = material()
    findings = await engine.global_review(states)
    assert findings and all(f.kind == "pending" for f in findings)
    assert all(f.key.startswith("cross-review-incomplete:") for f in findings)
    successful_groups = {group for group in runtime.calls if group[0] != "a"}
    assert successful_groups
    previous = len(runtime.calls)
    store.update(engine.job_id, status="failed", stage="review")
    store.resume_job(engine.job_id)
    runtime.invalid = False
    assert await engine.global_review(states) == []
    assert len(runtime.calls) > previous
    assert all(group[0] == "a" for group in runtime.calls[previous:])
