"""An exhausted application budget preserves a complete, explicitly unchecked draft."""

import json
from pathlib import Path

import pytest
from docx import Document

from folio.models import Finding, JobInput, Limits, Review, VisualRequest
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallBudgetExceeded, public_model_error


class OfflineRuntime(DemoRuntime):
    def __init__(self):
        self.calls = []
        self.stopped = False

    async def complete(self, task, context, schema, session_key):
        assert not self.stopped, "Delivery must not request another model response"
        self.calls.append(task)
        return await super().complete(task, context, schema, session_key)


@pytest.fixture(autouse=True)
def no_office_process(monkeypatch):
    # Exercise DOCX rendering, but never depend on a local Office installation.
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


class ExhaustedReviewEngine(Engine):
    def __init__(self, *args, with_visual=False):
        super().__init__(*args)
        self.with_visual = with_visual

    async def global_review(self, states):
        assert {s.section_id for s in self.store.states(self.job_id)} == {
            s.section_id for s in self.plan.sections
        }
        if self.with_visual:
            state = states[0]
            block_id = state.content.blocks[0].block_id
            state.content.visuals = [
                VisualRequest(
                    figure_id="unfinished_approval",
                    kind="flowchart",
                    caption="未完成的审批流程",
                    description="按原有业务正文展示审批流程",
                    source_block_ids=[block_id],
                    after_block_id=block_id,
                )
            ]
            self.store.save_section(self.job_id, state)
        self.saved = [s.model_dump() for s in self.store.states(self.job_id)]
        self.prior_issue = Finding(
            kind="pending",
            key="existing-business-question",
            description="原有审批范围仍待确认。",
            section_ids=[states[0].section_id],
        ).model_dump()
        self.store.update(
            self.job_id,
            snapshot={"review_passed": True, "blocking_issues": [self.prior_issue]},
        )
        self.stopped_calls = self.store.get(self.job_id)["calls"]
        self.limits = self.limits.model_copy(update={"max_calls": self.stopped_calls})
        self.runtime.stopped = True
        # The real request path must stop at Store.begin_task, before the runtime.
        return await self._call("review", {"sections": []}, Review)


async def test_budget_before_first_snapshot_keeps_saved_section_and_work_findings(store, inputs):
    class BeforeSnapshot(Engine):
        async def global_review(self, states):
            state = states[0]
            state.local_findings = [
                Finding(
                    kind="conflict",
                    key="saved-local",
                    description="本节与原文的审批条件冲突。",
                    section_ids=[state.section_id],
                )
            ]
            self.store.save_section(self.job_id, state)
            work = next(iter(self.store.work_states(self.job_id).values()))
            work.findings = [
                Finding(
                    kind="coverage",
                    key="saved-work",
                    description="任务遗漏异常处理。",
                    section_ids=[state.section_id],
                )
            ]
            self.store.save_work_state(self.job_id, work)
            self.store.update(self.job_id, snapshot={})
            self.limits = self.limits.model_copy(
                update={"max_calls": self.store.get(self.job_id)["calls"]}
            )
            return await self._call("review", {"sections": []}, Review)

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await BeforeSnapshot(store, DemoRuntime()).run(job_id)
    assert result["status"] == "needs_confirmation"
    assert {"saved-local", "saved-work", "review-incomplete:call-budget"} <= {
        f["key"] for f in result["snapshot"]["blocking_issues"]
    }
    assert result["snapshot"]["review_passed"] is False


@pytest.mark.parametrize("with_visual", [False, True])
async def test_complete_checkpoint_is_delivered_without_more_model_calls(
    store, inputs, with_visual
):
    runtime = OfflineRuntime()
    # Keep the production finish branch: disabled cosmetic calls must be caused by
    # the exhausted allowance, rather than the ordinary Demo-mode exemption.
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    engine = ExhaustedReviewEngine(store, runtime, with_visual=with_visual)
    job = await engine.run(job_id)
    assert job["status"] == "needs_confirmation" and job["stage"] == "complete"
    assert job["error"] is None and job["snapshot"]["review_passed"] is False
    assert job["calls"] == engine.stopped_calls == len(runtime.calls)
    assert job["snapshot"]["call_budget_exhausted"] == {
        "used": engine.stopped_calls,
        "limit": engine.stopped_calls,
    }
    assert [s.model_dump() for s in store.states(job_id)] == engine.saved
    issues = job["snapshot"]["blocking_issues"]
    assert engine.prior_issue in issues
    stopped = next(item for item in issues if item["key"] == "review-incomplete:call-budget")
    assert stopped["kind"] == "pending" and "部分检查尚未完成" in stopped["description"]
    assert set(stopped["section_ids"]) == {s["section_id"] for s in engine.saved}
    target = Path(job["artifact"])
    assert target.name == "draft.docx" and target.is_file()
    text = "\n".join(p.text for p in Document(target).paragraphs)
    assert "草稿" in text
    for state in engine.saved:
        assert all(b["text"] in text for b in state["content"]["blocks"] if b["text"])
    formatting = json.loads((target.parent / "formatting.json").read_text())
    assert formatting["status"] == "skipped" and formatting["changed"] is False
    if with_visual:
        visual = job["snapshot"]["visual_issues"]
        assert len(visual) == 1 and visual[0]["figure_id"] == "unfinished_approval"
        assert "ModelCallBudgetExceeded" in visual[0]["reason"]
        assert any(item["key"] == "visual:unfinished_approval" for item in issues)
        assert "配图待确认" in text and "未完成的审批流程" in text
    else:
        assert not job["snapshot"].get("visual_issues")
    with store.connect() as conn:
        assert conn.execute("SELECT published_version FROM documents").fetchone()[0] is None
    # Opening a terminal draft again cannot silently resume the unfinished review.
    assert (await engine.run(job_id))["calls"] == engine.stopped_calls


@pytest.mark.parametrize("after_saved_section", [False, True])
async def test_incomplete_body_exhaustion_remains_failed(
    store, inputs, monkeypatch, after_saved_section
):
    runtime = OfflineRuntime()
    request = JobInput(
        **inputs,
        runtime="demo",
        limits=Limits(max_calls=2000 if after_saved_section else 2, max_parallel_tasks=1),
    )
    job_id = store.create(request)
    engine = Engine(store, runtime)
    if after_saved_section:
        save_section = store.save_section

        def stop_after_first_saved(current_job, state):
            save_section(current_job, state)
            engine.limits = engine.limits.model_copy(
                update={"max_calls": store.get(current_job)["calls"]}
            )
            runtime.stopped = True

        monkeypatch.setattr(store, "save_section", stop_after_first_saved)
    with pytest.raises(ModelCallBudgetExceeded) as caught:
        await engine.run(job_id)
    job = store.get(job_id)
    assert job["status"] == "failed" and job["artifact"] is None
    assert job["calls"] == caught.value.used == caught.value.limit == len(runtime.calls)
    assert "ModelCallBudgetExceeded" in job["error"]
    assert not (job["snapshot"] or {}).get("review_passed")
    if after_saved_section:
        assert len(store.states(job_id)) == 1 < len(engine.plan.sections)
    assert not list((store.root / "jobs" / job_id).glob("*.docx"))


def test_exhausted_store_allocation_never_records_an_extra_attempt(store, demo_job):
    assert store.begin_task(demo_job, "first", 1) == 1
    with pytest.raises(ModelCallBudgetExceeded) as caught:
        store.begin_task(demo_job, "second", 1)
    assert caught.value.used == caught.value.limit == 1
    assert store.get(demo_job)["calls"] == 1 and store.task(demo_job, "second") is None
    starts = [event for event in store.events(demo_job) if event["kind"] == "task_started"]
    assert len(starts) == 1


@pytest.mark.parametrize("allowance", [2, 401, 1401, 2000])
def test_public_budget_error_identifies_application_allowance(allowance):
    result = public_model_error(ModelCallBudgetExceeded(allowance, allowance))
    assert result["code"] == "call_budget"
    assert "调用次数已达到上限" in result["text"]
    assert "API Key" not in result["text"] and "认证" not in result["text"]
