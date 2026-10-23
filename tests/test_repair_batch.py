import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_business_work import document, task
from test_request_context import engine_for

from folio.models import Block, DrillDown, Extraction, Finding, SectionContent, SectionState
from folio.orchestration.repair_batch import apply_repair_batch, independent_repairs
from folio.orchestration.work import WorkPlan
from folio.runtime.demo import DemoRuntime


def candidate(sid, *, targets=None, key="check"):
    return (
        Finding(kind="coverage", key=key, description="明确要求尚未满足", section_ids=[sid]),
        DrillDown(
            confirmed=True,
            reason="已核对依据",
            affected_section_ids=targets or [sid],
            repair_instruction="修正此处并保留其他内容",
        ),
    )


def test_independent_repairs_share_readonly_upstream_but_never_changed_dependencies():
    plan = WorkPlan(
        tasks=[
            task("a", ["a"]),
            task("b", ["b"], ["a"]),
            task("c", ["c"], ["a"]),
            task("d", ["d"], ["b"]),
        ]
    )
    a, b, c, d = [candidate(sid) for sid in "abcd"]
    assert independent_repairs([b, c, a, d], plan) == [b, c]
    assert independent_repairs([a, b, c, d], plan) == [a]


def test_repairs_of_same_owner_or_other_finding_read_set_wait():
    plan = WorkPlan(tasks=[task("ab", ["a", "b"]), task("c", ["c"])])
    a, b, c = [candidate(sid) for sid in "abc"]
    c[0].section_ids.append("a")
    assert independent_repairs([a, b, c], plan) == [a]
    assert independent_repairs([c, a], plan) == [c]


def test_invalid_repair_target_is_not_silently_ignored():
    with pytest.raises(ValueError, match="Invalid repair target"):
        independent_repairs(
            [candidate("a", targets=["b"])], WorkPlan(tasks=[task("a", ["a"]), task("b", ["b"])])
        )


def setup_engine(store, inputs):
    engine = engine_for(store, inputs, DemoRuntime())
    engine.plan = document()
    engine.work_plan = WorkPlan(tasks=[task("a", ["a"]), task("b", ["b"])])
    states = [
        SectionState(
            section_id=sid,
            content=SectionContent(
                blocks=[Block(block_id="body", text="原正文" + sid, origin="model_proposal")]
            ),
            extraction=Extraction(summary=sid),
            evidence=[],
        )
        for sid in "ab"
    ]
    for state in states:
        store.save_section(engine.job_id, state)
    return engine, states


async def test_repairs_overlap_without_publishing_until_all_readers_finish(store, inputs):
    engine, states = setup_engine(store, inputs)
    started, both = set(), asyncio.Event()

    async def generate(section, previous, finding, instruction):
        started.add(section.section_id)
        if len(started) == 2:
            both.set()
        await both.wait()
        assert all(s.revision == 1 for s in store.states(engine.job_id))
        await asyncio.sleep(0 if section.section_id == "a" else 0.01)
        assert all(s.revision == 1 for s in store.states(engine.job_id))
        return previous.model_copy(update={"revision": 2})

    engine.generate = generate
    async with asyncio.timeout(2):
        assert await apply_repair_batch(
            engine, [candidate("a"), candidate("b")], states, {"blocking_issues": []}
        )
    assert [s.revision for s in store.states(engine.job_id)] == [2, 2]


async def test_failed_worker_drains_peers_and_preserves_every_saved_section(store, inputs):
    engine, states = setup_engine(store, inputs)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(section, previous, finding, instruction):
        if section.section_id == "a":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await started.wait()
        raise ValueError("invalid revised content")

    engine.generate = generate
    with pytest.raises(ValueError, match="invalid revised content"):
        await apply_repair_batch(
            engine, [candidate("a"), candidate("b")], states, {"blocking_issues": []}
        )
    assert cancelled.is_set()
    assert [s.model_dump() for s in store.states(engine.job_id)] == [s.model_dump() for s in states]


async def test_malformed_repair_receipt_cannot_commit_any_part_of_the_batch(store, inputs):
    engine, states = setup_engine(store, inputs)
    engine.generate = AsyncMock(
        side_effect=lambda section, previous, *_: previous.model_copy(
            update={"section_id": "unassigned", "revision": 2}
        )
    )
    with pytest.raises(ValueError, match="assigned section and revision"):
        await apply_repair_batch(
            engine, [candidate("a"), candidate("b")], states, {"blocking_issues": []}
        )
    assert [s.model_dump() for s in store.states(engine.job_id)] == [s.model_dump() for s in states]


async def test_no_progress_table_rejection_keeps_entire_batch_uncommitted(store, inputs):
    engine, states = setup_engine(store, inputs)
    a, b = candidate("a", key="required-table"), candidate("b")
    states[0].local_findings.append(a[0])
    store.save_section(engine.job_id, states[0])
    engine.generate = AsyncMock(
        side_effect=lambda section, previous, *_: previous.model_copy(update={"revision": 2})
    )
    snapshot = {"blocking_issues": [a[0].model_dump()], "review_passed": False}
    assert not await apply_repair_batch(engine, [a, b], states, snapshot)
    assert [s.revision for s in store.states(engine.job_id)] == [1, 1]
    assert any(f["key"].startswith("repair-no-progress:") for f in snapshot["blocking_issues"])


async def test_business_review_parallelizes_only_dependency_ready_tasks(store, inputs):
    engine, _ = setup_engine(store, inputs)
    engine.work_plan = WorkPlan(
        tasks=[task("a", ["a"]), task("b", ["b"]), task("c", ["c"], ["a", "b"])]
    )
    entered, complete, both = set(), set(), asyncio.Event()

    async def review(task):
        if task.task_id == "c":
            assert complete == {"a", "b"}
        else:
            entered.add(task.task_id)
            if len(entered) == 2:
                both.set()
            await both.wait()
        complete.add(task.task_id)
        return [SimpleNamespace(key=task.task_id)]

    engine.review_business = review
    async with asyncio.timeout(2):
        assert [f.key for f in await engine.business_findings()] == ["a", "b", "c"]
