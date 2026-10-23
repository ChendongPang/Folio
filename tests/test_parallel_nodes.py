"""One researched business task writes all its nodes with bounded concurrency."""

import asyncio
import json

import pytest
from test_business_work import GroupRuntime

from folio.models import JobInput, Limits
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult


@pytest.fixture(autouse=True)
def no_office_process(monkeypatch):
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


async def test_first_node_overlaps_siblings_and_review_waits_for_every_saved_node(store, inputs):
    inputs["user_request"] = "\n".join(f"# 交易规则{i}" for i in range(8))

    class ConcurrentNodes(GroupRuntime):
        def __init__(self):
            super().__init__()
            self.ids = []
            self.started = []
            self.validated = set()
            self.active = self.peak = 0
            self.first_wave = asyncio.Event()

        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                self.ids = [s["section_id"] for s in context["sections"]]
            if name == "generate":
                sid = context["section"]["section_id"]
                assert "peer_sections" not in context
                self.started.append(sid)
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    if len(self.started) == 8:
                        assert not store.states(job_id)
                        self.first_wave.set()
                    await asyncio.wait_for(self.first_wave.wait(), 3)
                    return await super().complete(name, context, schema, session)
                finally:
                    self.active -= 1
            result = await super().complete(name, context, schema, session)
            if name == "validate":
                self.validated.add(context["section"]["section_id"])
            if name == "review_work":
                assert self.validated == set(self.ids)
                assert {s.section_id for s in store.states(job_id)} == set(self.ids)
            return result

    runtime = ConcurrentNodes()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft" and runtime.peak == 8
    assert runtime.started == runtime.ids
    assert set(runtime.started) == set(runtime.ids)
    assert len(runtime.started) == len(store.states(job_id)) == 8
    writers = [context for name, context in runtime.calls if name == "generate"]
    assert all(c["business_record"] == writers[0]["business_record"] for c in writers)
    assert all(c["evidence"] == writers[0]["evidence"] for c in writers)
    assert sum(name == "query" for name, _ in runtime.calls) == 1
    assert sum(name == "review_work" for name, _ in runtime.calls) == 1


async def test_eight_sibling_writers_merge_distinct_gaps_without_losing_proposals(store, inputs):
    inputs["user_request"] = "\n".join(f"# 交易规则{i}" for i in range(8))

    class ConcurrentGaps(GroupRuntime):
        def __init__(self):
            super().__init__()
            self.started = []
            self.ready = asyncio.Event()

        async def complete(self, name, context, schema, session):
            if name == "generate":
                sid = context["section"]["section_id"]
                self.started.append(sid)
                if len(self.started) == 8:
                    self.ready.set()
                await asyncio.wait_for(self.ready.wait(), 3)
                result = await super().complete(name, context, schema, session)
                data = json.loads(result.content)
                data["pending_items"] = ["补充规则：" + sid]
                return RuntimeResult(json.dumps(data))
            return await super().complete(name, context, schema, session)

    runtime = ConcurrentGaps()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    work = store.work_states(job_id)["business"]
    assert job["status"] == "draft"
    assert len(set(runtime.started)) == len(store.states(job_id)) == 8
    assert len(work.searches) == 3
    assert len({proposal.gap_id for proposal in work.proposals}) == 8
    assert {sid for proposal in work.proposals for sid in proposal.section_ids} == set(
        runtime.started
    )
    assert not Engine.unfilled_gaps(work)


@pytest.mark.parametrize("cancel", [False, True])
async def test_same_task_node_failure_or_cancellation_drains_all_writers(store, inputs, cancel):
    inputs["user_request"] = "\n".join(f"# 交易规则{i}" for i in range(8))

    class InterruptedNodes(GroupRuntime):
        def __init__(self):
            super().__init__()
            self.started = []
            self.exited = set()
            self.all_started = asyncio.Event()

        async def complete(self, name, context, schema, session):
            if name == "generate":
                sid = context["section"]["section_id"]
                self.started.append(sid)
                if len(self.started) == 8:
                    self.all_started.set()
                try:
                    await asyncio.wait_for(self.all_started.wait(), 3)
                    if not cancel and sid == self.started[0]:
                        raise RuntimeError("first node failed while sibling was running")
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    self.exited.add(sid)
            return await super().complete(name, context, schema, session)

    runtime = InterruptedNodes()
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(call_attempts=1)))
    operation = asyncio.create_task(Engine(store, runtime).run(job_id))
    await asyncio.wait_for(runtime.all_started.wait(), 3)
    if cancel:
        operation.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await operation
    assert runtime.exited == set(runtime.started) and len(runtime.exited) == 8
    assert not store.states(job_id)
    assert store.get(job_id)["status"] == ("running" if cancel else "failed")
    assert not any(name == "review_work" for name, _ in runtime.calls)
    assert not any(t.get_name().startswith("review-worker-") for t in asyncio.all_tasks())
