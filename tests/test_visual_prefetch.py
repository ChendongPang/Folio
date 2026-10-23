"""Background illustration work shares limits and never publishes partial manifests."""

import asyncio
import json
from pathlib import Path

import pytest
from docx import Document
from test_business_work import GroupRuntime, task
from test_visual_workflow import fake_renderer as fake_renderer
from test_visual_workflow import (
    material,
    request,
    review_result,
    several_figures,
)

from folio.models import Finding, JobInput, Limits
from folio.orchestration.engine import Engine
from folio.orchestration.visual_prefetch import VisualPrefetch
from folio.orchestration.work import WorkPlan, WorkState
from folio.runtime.base import RuntimeResult
from folio.runtime.call_budget import (
    VisualPrefetchDeferred,
    current_call_budget,
    visual_prefetch_policy,
)
from folio.runtime.errors import ModelCallBudgetExceeded
from folio.visuals.workflow import (
    VisualSession,
    VisualSource,
    build_visuals,
    load_visuals,
    visual_identity,
)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    import folio.orchestration.engine as module

    assert Path(module.__file__).is_relative_to(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


@pytest.fixture
def visual_inputs(inputs):
    # These end-to-end tests ask the writer for one figure before exercising
    # prefetch scheduling. A mocked unsolicited figure must still be rejected.
    inputs["user_request"] = (
        "# 外汇交易\n描述交易要求，并提供审批流程图。\n## 交易撤销\n描述撤销审批规则。\n"
    )
    return inputs


async def test_separate_prefetches_share_four_slots_and_leave_manifest_untouched(
    tmp_path, fake_renderer
):
    plan, states = several_figures(6)
    session = VisualSession()
    started, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    calls = []
    manifest = tmp_path / "visuals.json"
    manifest.write_text('"previous final manifest"')

    async def call(name, context, model):
        nonlocal active, peak
        calls.append(name)
        if name == "validate_visual":
            return review_result(problems=[])
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        try:
            await release.wait()
            return VisualSource(lines=["flowchart TD", "A-->B"])
        finally:
            active -= 1

    workers = [
        asyncio.create_task(
            build_visuals(
                plan,
                [state],
                tmp_path,
                call,
                lambda _: None,
                session=session,
                write_manifest=False,
            )
        )
        for state in states
    ]
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert active == 4 and manifest.read_text() == '"previous final manifest"'
        release.set()
        partials = await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    assert peak == 4 and active == 0 and all(not result.issues for result in partials)
    assert manifest.read_text() == '"previous final manifest"'
    assert calls.count("design_flowchart") == calls.count("validate_visual") == 6
    before = list(calls)
    final = await build_visuals(plan, states, tmp_path, call, lambda _: None, session=session)
    assert calls == before and len(fake_renderer) == 6
    assert load_visuals(tmp_path, states) == final and len(final) == 6


async def test_identical_figures_across_prefetch_calls_share_one_validated_asset(
    tmp_path, fake_renderer
):
    plan, states = several_figures(2, same_identity=True)
    session = VisualSession()
    calls = []

    async def call(name, context, model):
        calls.append(name)
        await asyncio.sleep(0)
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    results = await asyncio.gather(
        *(
            build_visuals(
                plan,
                [state],
                tmp_path,
                call,
                lambda _: None,
                session=session,
                write_manifest=False,
            )
            for state in states
        )
    )
    assert calls == ["design_flowchart", "validate_visual"] and len(fake_renderer) == 1
    assert results[0]["s1"][0]["image_path"] == results[1]["s2"][0]["image_path"]
    assert not (tmp_path / "visuals.json").exists()


async def test_final_export_cannot_grant_failed_prefetch_another_three_attempts(
    tmp_path, fake_renderer
):
    plan, states = material()
    session = VisualSession()
    calls = []

    async def call(name, context, model):
        calls.append(name)
        return (
            review_result(problems=["拒绝分支丢失"])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    partial = await build_visuals(
        plan, states, tmp_path, call, lambda _: None, session=session, write_manifest=False
    )
    assert len(partial.issues) == 1 and calls.count("design_flowchart") == 3
    before = list(calls)
    final = await build_visuals(plan, states, tmp_path, call, lambda _: None, session=session)
    assert calls == before and len(fake_renderer) == 3
    assert final.issues == partial.issues == load_visuals(tmp_path, states).issues
    assert not list((tmp_path / "visuals").glob("*/asset.json"))


async def test_changed_final_prose_regenerates_and_revalidates_only_its_identity(
    tmp_path, fake_renderer
):
    plan, states = several_figures(2)
    session = VisualSession()
    calls = []

    async def call(name, context, model):
        calls.append((name, context["section"]["number"]))
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    await build_visuals(
        plan, states, tmp_path, call, lambda _: None, session=session, write_manifest=False
    )
    old_id = visual_identity(plan, states[0], states[0].content.visuals[0])
    states[0].content.blocks[0].text += "审批必须由主管执行。"
    assert visual_identity(plan, states[0], states[0].content.visuals[0]) != old_id
    before = len(calls)
    final = await build_visuals(plan, states, tmp_path, call, lambda _: None, session=session)
    assert calls[before:] == [("design_flowchart", "1"), ("validate_visual", "1")]
    assert len(fake_renderer) == 3 and not final.issues
    assert load_visuals(tmp_path, states) == final


def saved_visual_engine(store, inputs, *, finding=None):
    engine = Engine(store, GroupRuntime())
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan, states = material()
    owner = task("figures", [states[0].section_id])
    store.save_plan(engine.job_id, engine.plan)
    store.save_work_plan(engine.job_id, WorkPlan(tasks=[owner]))
    if finding:
        states[0].local_findings = [finding]
    store.save_section(engine.job_id, states[0])
    store.save_work_state(
        engine.job_id, WorkState(task_id=owner.task_id, review_key="reviewed", researched=True)
    )
    return engine, owner, states


@pytest.mark.parametrize("kind", ["pending", "conflict"])
async def test_reviewed_pending_red_proposal_can_prefetch_but_known_conflict_cannot(
    store, inputs, kind
):
    finding = Finding(kind=kind, key="check", description="来源待确认", section_ids=["s1"])
    engine, owner, states = saved_visual_engine(store, inputs, finding=finding)
    started, drained = asyncio.Event(), asyncio.Event()

    async def call(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            drained.set()

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule(owner, [finding])
    if kind == "pending":
        await asyncio.wait_for(started.wait(), 1)
        assert len(manager.tasks) == 1
    else:
        assert not manager.tasks
    await manager.close()
    assert drained.is_set() == (kind == "pending")
    assert store.states(engine.job_id) == states


async def test_final_changed_identity_cancels_obsolete_prefetch_and_checks_new_body(
    store, inputs, fake_renderer
):
    engine, owner, states = saved_visual_engine(store, inputs)
    started, drained = asyncio.Event(), asyncio.Event()
    original = states[0].model_dump()

    async def old_call(name, context, model):
        assert context["source_blocks"][0]["text"] == original["content"]["blocks"][0]["text"]
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            drained.set()

    engine._call = old_call
    manager = VisualPrefetch(engine)
    manager.schedule(owner, [])
    await asyncio.wait_for(started.wait(), 1)
    states[0].content.blocks[0].text += "最新约束：不得越过主管审批。"
    await manager.settle(states)
    assert drained.is_set() and all(worker.cancelled() for worker in manager.tasks.values())
    calls = []

    async def new_call(name, context, model):
        calls.append(name)
        assert "最新约束" in context["source_blocks"][0]["text"]
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    folder = store.root / "jobs" / engine.job_id
    result = await build_visuals(
        engine.plan, states, folder, new_call, lambda _: None, session=manager.session
    )
    assert calls == ["design_flowchart", "validate_visual"] and len(fake_renderer) == 1
    assert not result.issues and load_visuals(folder, states) == result
    await manager.close()


class PrefetchRuntime(GroupRuntime):
    def __init__(self, store, *, mode="normal"):
        super().__init__()
        self.store, self.mode = store, mode
        self.figure_started = asyncio.Event()
        self.slow_started = asyncio.Event()
        self.figure_drained = False
        self.figure_calls = []

    async def complete(self, name, context, schema, session):
        if name == "work_plan":
            ids = [s["section_id"] for s in context["sections"]]
            return RuntimeResult(
                WorkPlan(tasks=[task("fast", [ids[0]]), task("slow", [ids[1]])]).model_dump_json()
            )
        if name == "generate" and context["work_task"]["task_id"] == "slow":
            self.slow_started.set()
            await asyncio.wait_for(self.figure_started.wait(), 3)
            if self.mode == "error":
                raise RuntimeError("writing failed while prefetch was active")
            if self.mode == "cancel":
                await asyncio.Future()
        if name in {"design_flowchart", "validate_visual"}:
            self.figure_calls.append(name)
            if name == "validate_visual":
                return RuntimeResult('{"problems":[]}')
            await asyncio.wait_for(self.slow_started.wait(), 3)
            assert self.store.get(self.job_id)["stage"] == "generation"
            self.figure_started.set()
            if self.mode in {"error", "cancel", "budget"}:
                try:
                    await asyncio.Future()
                finally:
                    await asyncio.sleep(0.01)
                    self.figure_drained = True
            return RuntimeResult('{"lines":["flowchart TD","A-->B"]}')
        result = await super().complete(name, context, schema, session)
        if name == "generate" and context["work_task"]["task_id"] == "fast":
            assert context["required_visual_kinds"] == ("flowchart",)
            assert context["figure_policy"]["allowed_visual_kinds"] == ["flowchart"]
            data = json.loads(result.content)
            block = data["blocks"][0]["block_id"]
            data["visuals"] = [
                request().model_dump() | {"source_block_ids": [block], "after_block_id": block}
            ]
            return RuntimeResult(json.dumps(data))
        return result


async def test_reviewed_task_prefetch_overlaps_other_writing_and_final_reuses_validation(
    store, visual_inputs, fake_renderer, monkeypatch
):
    final_join = asyncio.Event()
    stages = []

    class WaitingFigureRuntime(PrefetchRuntime):
        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "design_flowchart":
                await asyncio.wait_for(final_join.wait(), 3)
            return result

    original_settle = VisualPrefetch.settle

    async def settle_after_stage_change(manager, states, **kwargs):
        stages.append(store.get(manager.engine.job_id)["stage"])
        assert stages[-1] == "illustrating"
        assert any(not task.done() for task in manager.tasks.values())
        final_join.set()
        return await original_settle(manager, states, **kwargs)

    monkeypatch.setattr(VisualPrefetch, "settle", settle_after_stage_change)
    runtime = WaitingFigureRuntime(store)
    runtime.job_id = store.create(JobInput(**visual_inputs, runtime="demo"))
    engine = Engine(store, runtime)
    job = await engine.run(runtime.job_id)
    assert job["status"] == "draft" and job["snapshot"]["review_passed"]
    assert runtime.figure_calls == ["design_flowchart", "validate_visual"]
    assert len(Document(job["artifact"]).inline_shapes) == len(fake_renderer) == 1
    assert all(task.done() for task in engine._visual_prefetch.tasks.values())
    assert engine._visual_prefetch.closed
    assert stages == ["illustrating"]


@pytest.mark.parametrize("mode", ["error", "cancel"])
async def test_engine_failure_and_external_cancel_drain_prefetch_before_return(
    store, visual_inputs, fake_renderer, mode
):
    runtime = PrefetchRuntime(store, mode=mode)
    runtime.job_id = store.create(
        JobInput(**visual_inputs, runtime="demo", limits=Limits(call_attempts=1))
    )
    engine = Engine(store, runtime)
    operation = asyncio.create_task(engine.run(runtime.job_id))
    await asyncio.wait_for(runtime.figure_started.wait(), 3)
    if mode == "cancel":
        operation.cancel()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else RuntimeError):
        await operation
    assert runtime.figure_drained
    assert all(task.done() for task in engine._visual_prefetch.tasks.values())
    assert not any(task.get_name().startswith("folio-visual-") for task in asyncio.all_tasks())
    assert not (store.root / "jobs" / runtime.job_id / "visuals.json").exists()
    assert not fake_renderer and store.get(runtime.job_id)["artifact"] is None


async def test_budget_fallback_cancels_prefetch_and_marks_unverified_figure_pending(
    store, visual_inputs, fake_renderer
):
    class BudgetAtReview(Engine):
        async def global_review(self, states):
            self.stopped_calls = self.store.get(self.job_id)["calls"]
            self.limits = self.limits.model_copy(update={"max_calls": self.stopped_calls})
            raise ModelCallBudgetExceeded(self.stopped_calls, self.stopped_calls)

    runtime = PrefetchRuntime(store, mode="budget")
    runtime.job_id = store.create(JobInput(**visual_inputs, runtime="demo"))
    engine = BudgetAtReview(store, runtime)
    job = await engine.run(runtime.job_id)
    assert runtime.figure_drained and not fake_renderer
    assert job["status"] == "needs_confirmation" and not job["snapshot"]["review_passed"]
    assert job["calls"] == engine.stopped_calls
    assert len(job["snapshot"]["visual_issues"]) == 1
    assert "ModelCallBudgetExceeded" in job["snapshot"]["visual_issues"][0]["reason"]
    assert runtime.figure_calls == ["design_flowchart"]
    assert Path(job["artifact"]).exists()


@pytest.mark.parametrize("change", ["missing", "revision", "content", "extraction", "findings"])
async def test_section_prefetch_rejects_unsaved_or_stale_snapshots(store, inputs, change):
    engine, _, states = saved_visual_engine(store, inputs)
    snapshot = states[0].model_copy(deep=True)
    if change == "missing":
        snapshot.section_id = "not-saved"
    elif change == "revision":
        snapshot.revision += 1
    elif change == "content":
        snapshot.content.blocks[0].text += "未保存正文"
    elif change == "extraction":
        snapshot.extraction.summary += "未核验的新摘要"
    else:
        snapshot.local_findings = [
            Finding(kind="pending", key="new", description="未保存的审阅", section_ids=["s1"])
        ]
    manager = VisualPrefetch(engine)
    manager.schedule_section(snapshot)
    assert not manager.tasks
    await manager.close()


@pytest.mark.parametrize("kind", ["pending", "conflict", "coverage", "unsupported", "extraction"])
async def test_section_prefetch_requires_local_checks_and_reuses_whole_group_schedule(
    store, inputs, fake_renderer, kind
):
    finding = Finding(kind=kind, key="local", description="本节待处理", section_ids=["s1"])
    engine, owner, states = saved_visual_engine(store, inputs, finding=finding)
    store.save_work_state(engine.job_id, WorkState(task_id=owner.task_id, researched=True))
    calls = []

    async def call(name, context, model):
        calls.append(name)
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    assert len(manager.tasks) == (kind == "pending")
    # The later group review retains its own result but cannot duplicate a figure.
    store.save_work_state(
        engine.job_id, WorkState(task_id=owner.task_id, researched=True, review_key="reviewed")
    )
    manager.schedule(owner, [finding])
    await manager.settle(states)
    assert calls == (["design_flowchart", "validate_visual"] if kind == "pending" else [])
    assert len(fake_renderer) == (kind == "pending")
    assert not (store.root / "jobs" / engine.job_id / "visuals.json").exists()
    await manager.close()


@pytest.mark.parametrize("review_pending", [False, True])
async def test_single_section_prefetch_starts_before_slow_sibling_and_group_review(
    store, visual_inputs, fake_renderer, review_pending
):
    class SameGroupRuntime(GroupRuntime):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.figure_calls = []
            self.group_reviewed = False

        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                self.ids = [s["section_id"] for s in context["sections"]]
            if name == "generate" and context["section"]["section_id"] == self.ids[1]:
                # Previously this timed out: group review waited for this sibling,
                # and figures waited for group review.
                await asyncio.wait_for(self.started.wait(), 3)
            if name in {"design_flowchart", "validate_visual"}:
                self.figure_calls.append(name)
                if name == "validate_visual":
                    return RuntimeResult('{"problems":[]}')
                saved = {s.section_id: s for s in store.states(self.job_id)}
                assert self.ids[0] in saved and self.ids[1] not in saved
                assert not saved[self.ids[0]].local_findings
                assert any(
                    action == "validate" and item["section"]["section_id"] == self.ids[0]
                    for action, item in self.calls
                )
                assert not self.group_reviewed
                assert not store.work_states(self.job_id)["business"].review_key
                assert store.get(self.job_id)["stage"] == "generation"
                self.started.set()
                return RuntimeResult('{"lines":["flowchart TD","A-->B"]}')
            if name == "review_work":
                assert self.started.is_set()
                assert set(self.ids) <= {s.section_id for s in store.states(self.job_id)}
                self.group_reviewed = True
                if review_pending:
                    return RuntimeResult(
                        json.dumps(
                            {
                                "findings": [
                                    {
                                        "kind": "pending",
                                        "key": "approval-scope",
                                        "description": "审批范围需要用户确认",
                                        "section_ids": self.ids,
                                    }
                                ]
                            }
                        )
                    )
            result = await super().complete(name, context, schema, session)
            if name == "generate" and context["section"]["section_id"] == self.ids[0]:
                assert context["required_visual_kinds"] == ("flowchart",)
                assert context["figure_policy"]["allowed_visual_kinds"] == ["flowchart"]
                data = json.loads(result.content)
                block = data["blocks"][0]["block_id"]
                data["visuals"] = [
                    request().model_dump() | {"source_block_ids": [block], "after_block_id": block}
                ]
                return RuntimeResult(json.dumps(data))
            return result

    runtime = SameGroupRuntime()
    runtime.job_id = store.create(JobInput(**visual_inputs, runtime="demo"))
    engine = Engine(store, runtime)
    job = await asyncio.wait_for(engine.run(runtime.job_id), 10)
    assert runtime.group_reviewed
    assert job["snapshot"]["review_passed"] == (not review_pending)
    assert job["status"] == ("needs_confirmation" if review_pending else "draft")
    assert all(state.revision == 1 for state in store.states(runtime.job_id))
    if review_pending:
        assert any(f["key"] == "approval-scope" for f in job["snapshot"]["blocking_issues"])
    assert runtime.figure_calls == ["design_flowchart", "validate_visual"]
    assert len(fake_renderer) == len(Document(job["artifact"]).inline_shapes) == 1
    assert all(worker.done() for worker in engine._visual_prefetch.tasks.values())


async def test_early_section_prefetch_never_reuses_changed_persisted_body(
    store, inputs, fake_renderer
):
    engine, owner, states = saved_visual_engine(store, inputs)
    calls = []

    async def call(name, context, model):
        calls.append((name, context["source_blocks"][0]["text"]))
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    # Mutating a caller's snapshot cannot mutate the in-flight source copy.
    old_text = states[0].content.blocks[0].text
    states[0].content.blocks[0].text += "主管必须独立审批。"
    states[0].revision += 1
    await asyncio.gather(*manager.tasks.values())
    assert calls == [("design_flowchart", old_text), ("validate_visual", old_text)]
    store.save_section(engine.job_id, states[0])
    manager.schedule_section(states[0])
    store.save_work_state(
        engine.job_id, WorkState(task_id=owner.task_id, researched=True, review_key="new-review")
    )
    manager.schedule(owner, [])
    await manager.settle(states)
    folder = store.root / "jobs" / engine.job_id
    final = await build_visuals(
        engine.plan, states, folder, call, lambda _: None, session=manager.session
    )
    assert len(calls) == 4 and calls[-1][1] == states[0].content.blocks[0].text
    assert len(fake_renderer) == 2 and not final.issues
    assert load_visuals(folder, states) == final
    await manager.close()


async def test_early_prefetch_double_cancel_drains_cleanup_before_return(store, inputs):
    engine, _, states = saved_visual_engine(store, inputs)
    started, cleaning, release, drained = (asyncio.Event() for _ in range(4))

    async def call(*_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            drained.set()

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(manager.close())
    await asyncio.wait_for(cleaning.wait(), 1)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not drained.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    assert drained.is_set() and all(worker.done() for worker in manager.tasks.values())


async def test_early_prefetch_setup_failure_is_optional_and_finishes_without_deadlock(
    store, inputs, monkeypatch
):
    engine, _, states = saved_visual_engine(store, inputs)

    async def broken_setup(*args, **kwargs):
        raise ValueError("renderer setup failed")

    monkeypatch.setattr("folio.orchestration.visual_prefetch.build_visuals", broken_setup)
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    await asyncio.wait_for(manager.settle(states), 1)
    assert all(worker.done() and worker.result() is None for worker in manager.tasks.values())
    assert not (store.root / "jobs" / engine.job_id / "visuals.json").exists()
    await asyncio.wait_for(manager.close(), 1)


@pytest.mark.parametrize("defer_at", ["design_flowchart", "validate_visual"])
async def test_soft_budget_defers_without_poisoning_final_figure(
    store, inputs, fake_renderer, defer_at
):
    engine, _, states = saved_visual_engine(store, inputs)
    seen = []

    async def call(name, context, model):
        policy = current_call_budget.get()
        seen.append((name, policy))
        if policy is not None and name == defer_at:
            raise VisualPrefetchDeferred(
                bucket=policy.bucket,
                used=policy.max_calls,
                limit=policy.max_calls,
                calls=policy.max_calls,
                reserve_calls=policy.reserve_calls,
            )
        return (
            review_result(problems=[])
            if name == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    await asyncio.gather(*manager.tasks.values())
    identity = visual_identity(engine.plan, states[0], states[0].content.visuals[0])
    assert manager.budget_deferred
    assert manager.session.failures == {}
    assert manager.session.attempts.get(identity, 0) == 0
    assert all(policy == visual_prefetch_policy(engine.limits.max_calls) for _, policy in seen)
    assert current_call_budget.get() is None
    before = len(seen)
    await manager.settle(states)

    final = await build_visuals(
        engine.plan,
        states,
        store.root / "jobs" / engine.job_id,
        call,
        lambda _: None,
        session=manager.session,
    )

    assert not final.issues
    assert seen[before:] == [("design_flowchart", None), ("validate_visual", None)]
    assert manager.session.attempts[identity] == 1
    await manager.close()


@pytest.mark.parametrize("change", ["rewrite", "remove_visual", "new_conflict"])
async def test_saved_revision_cancels_obsolete_prefetch_before_final_export(store, inputs, change):
    engine, _, states = saved_visual_engine(store, inputs)
    started, drained = asyncio.Event(), asyncio.Event()

    async def call(*_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    await asyncio.wait_for(started.wait(), 1)
    old_worker = next(iter(manager.tasks.values()))
    current = states[0].model_copy(deep=True)
    current.revision += 1
    if change == "rewrite":
        current.content.blocks[0].text += "主管必须审批。"
    elif change == "remove_visual":
        current.content.visuals = []
    else:
        current.local_findings = [
            Finding(
                kind="conflict",
                key="approval",
                description="审批步骤冲突",
                section_ids=[current.section_id],
            )
        ]
    store.save_section(engine.job_id, current)
    # Even with no optional budget left, stale work must stop immediately.
    manager.budget_deferred = True
    manager.schedule_section(current)
    await asyncio.wait_for(drained.wait(), 1)
    await manager.refresh_and_drain()
    assert old_worker.cancelled()
    assert not manager.closed
    assert not (store.root / "jobs" / engine.job_id / "visuals.json").exists()
    await manager.close()


async def test_task_review_conflict_cancels_active_prefetch(store, inputs):
    engine, owner, states = saved_visual_engine(store, inputs)
    started, drained = asyncio.Event(), asyncio.Event()

    async def call(*_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()

    engine._call = call
    manager = VisualPrefetch(engine)
    manager.schedule_section(states[0])
    await asyncio.wait_for(started.wait(), 1)
    finding = Finding(
        kind="conflict", key="approval", description="任务审批流程冲突", section_ids=owner.section_ids
    )
    manager.schedule(owner, [finding])
    await asyncio.wait_for(drained.wait(), 1)
    await manager.close()
    assert all(task.cancelled() for task in manager.tasks.values())


def test_visual_prefetch_policy_reserves_core_work_and_caps_optional_calls():
    normal = visual_prefetch_policy(2000)
    assert normal.max_calls == 120 and normal.reserve_calls == 500
    small = visual_prefetch_policy(10)
    assert small.reserve_calls == 10
    assert visual_prefetch_policy(100).max_calls == 20
    assert visual_prefetch_policy(100).reserve_calls == 25
    assert visual_prefetch_policy(200).bucket != visual_prefetch_policy(100).bucket
