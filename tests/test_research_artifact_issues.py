"""A rejected calculation is a durable issue, not a transient tool message."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_research_artifacts import artifact_id, register, state_for, verify
from test_research_artifacts import research as research

from folio.models import Evidence, JobInput, Section
from folio.orchestration.work import WorkState, WorkTask
from folio.research.artifact_issues import (
    ISSUES_KEY,
    UnusedArtifactReview,
    findings,
    reconcile_artifact_issues,
    record_artifact_issue,
    resolve_unused_artifact,
    resolve_verified_artifact,
    unresolved_artifact_issues,
    unused_artifact_review_context,
)
from folio.research.artifacts import ArtifactError
from folio.research.ledger import ResearchLedger
from folio.runtime.errors import ModelCallFailed


@pytest.fixture
def pending():
    source = Evidence(
        evidence_id="source",
        source_id="fields.txt",
        source_version="v1",
        locator="line 1",
        text="字段说明来自原始资料。",
    )
    state = state_for(source)
    task = WorkTask(
        task_id="fields",
        title="字段说明",
        objective="说明字段要求",
        section_ids=["fields"],
        questions=["字段要求是什么？"],
        completion_criteria=["按原始资料描述"],
        rationale="同一业务",
    )
    section = Section(
        section_id="fields",
        node_id="fields",
        title="字段说明",
        level=1,
        display_number="1",
        instruction="按原始资料说明字段要求",
    )
    session = {}
    issue = record_artifact_issue(
        session, "a" * 64, "必填字段共有58个", "统计口径尚未核实", [source.evidence_id]
    )
    return SimpleNamespace(
        session=session, issue=issue, source=source, state=state, task=task, section=section
    )


def unused_review(pending, **updates):
    context = unused_artifact_review_context(pending.issue, pending.task, [pending.state], sections=[pending.section])
    return UnusedArtifactReview.model_validate({
        "artifact_id": pending.issue["artifact_id"], "body_version": context["body_version"],
        "resolved_as_unused": True, "claim_present": False, "task_requirements_met": True,
        "alternative_evidence_ids": [pending.source.evidence_id],
        "block_refs": [{"section_id": "fields", "block_id": "count"}],
        "reason": "本次任务只要求说明字段原文；正文未使用该统计，原有要求已有原始依据。",
    } | updates)


def test_finish_and_empty_missing_do_not_discard_rejected_calculations(pending):
    pending.session.update(finished=True, missing=[], notes="已完成")
    assert len(unresolved_artifact_issues(pending.session)) == 1
    assert findings(pending.session, ["fields"])[0].key == "research-artifact:" + "a" * 64
    saved = deepcopy(pending.session)
    record_artifact_issue(saved, "a" * 64, pending.issue["claim"], "重试仍失败")
    assert len(unresolved_artifact_issues(saved)) == 1
    with pytest.raises(ArtifactError, match='different business claim'):
        record_artifact_issue(saved, "a" * 64, "另一个结论", "未核实")


def test_only_successfully_verified_same_artifact_closes_and_hash_change_reopens(research):
    evidence = register(research)
    session = {}
    record_artifact_issue(session, artifact_id(evidence), "共有一个必填字段", "等待核验")
    with pytest.raises(ArtifactError, match='unverified'):
        resolve_verified_artifact(session, evidence)
    assert unresolved_artifact_issues(session)
    evidence = verify(research, evidence)
    assert resolve_verified_artifact(session, evidence)
    assert not unresolved_artifact_issues(session)
    assert session[ISSUES_KEY][artifact_id(evidence)]["status"] == "verified"
    research.output.write_text("changed")
    assert "no longer valid" in unresolved_artifact_issues(session)[0]["reason"]


@pytest.mark.parametrize("updates", [
    {"claim_present": True}, {"task_requirements_met": False},
    {"alternative_evidence_ids": []}, {"block_refs": []},
])
def test_unused_review_requires_both_absence_and_satisfied_original_requirements(pending, updates):
    with pytest.raises(ValidationError):
        unused_review(pending, **updates)


def test_independent_unused_review_closes_only_current_body_version(pending):
    review = unused_review(pending)
    assert resolve_unused_artifact(pending.session, review, [pending.state], "independent-review-key")
    assert not findings(pending.session, ["fields"], states=[pending.state])
    record = pending.session[ISSUES_KEY][pending.issue["artifact_id"]]
    assert record["resolution"]["review_key"] == "independent-review-key"
    assert record["claim"] == pending.issue["claim"]
    pending.state.revision += 1
    assert "version changed" in findings(pending.session, ["fields"], states=[pending.state])[0].description
    with pytest.raises(ArtifactError, match='current body version'):
        resolve_unused_artifact(pending.session, review, [pending.state], "old-review")


@pytest.mark.parametrize("change", ["invented_block", "uncited_source", "proposal", "ocr"])
def test_unused_review_cannot_use_invented_or_unverified_alternative_sources(pending, change):
    if change == "uncited_source":
        pending.state.content.blocks[0].evidence_ids = []
    elif change == "proposal":
        pending.state.content.blocks[0].origin = "model_proposal"
    elif change == "ocr":
        pending.source.metadata["requires_extraction_review"] = True
        pending.state.evidence = [pending.source]
    review = unused_review(pending, **({"block_refs": [{"section_id": "fields", "block_id": "absent"}]} if change == "invented_block" else {}))
    with pytest.raises(ArtifactError):
        resolve_unused_artifact(pending.session, review, [pending.state], "review")
    assert unresolved_artifact_issues(pending.session)


def fake_engine(store, inputs, pending, *, fail=False, fits=True):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    work = WorkState(task_id="fields", research={"round_id": "research-round", **deepcopy(pending.session)})
    store.save_work_state(job_id, work)
    ledger = ResearchLedger(store.root / "jobs" / job_id / "research" / "ledger.sqlite")
    ledger.save_session("fields", "research-round", deepcopy(pending.session))
    return SimpleNamespace(
        store=store, job_id=job_id,
        task_sections=lambda task: [pending.section],
        request_capacity=AsyncMock(return_value={"fits": fits}),
        call=AsyncMock(side_effect=ModelCallFailed("review unavailable") if fail else None,
                       return_value=unused_review(pending)),
        request_context=lambda name, context: context,
        request_schema=lambda *args: {},
        request_cache_key=lambda *args: "review-unused-artifact-key",
    ), work, ledger


async def test_reconcile_persists_independent_resolution_in_work_and_resume_session(store, inputs, pending):
    engine, work, ledger = fake_engine(store, inputs, pending)
    result = await reconcile_artifact_issues(engine, pending.task, work, [pending.state])
    assert result == []
    saved = store.work_states(engine.job_id)["fields"].research[ISSUES_KEY]
    restored = ledger.load_session("fields", "research-round")[ISSUES_KEY]
    assert saved == restored
    assert saved[pending.issue["artifact_id"]]["status"] == "unused"
    assert engine.call.call_args.args[0] == "review_unused_artifact"


@pytest.mark.parametrize("failure", ["unavailable", "capacity"])
async def test_reconcile_review_failure_or_oversize_keeps_pending(store, inputs, pending, failure):
    engine, work, _ = fake_engine(store, inputs, pending, fail=failure == "unavailable", fits=failure != "capacity")
    result = await reconcile_artifact_issues(engine, pending.task, work, [pending.state])
    assert len(result) == 1
    assert unresolved_artifact_issues(work.research)
    if failure == "capacity":
        engine.call.assert_not_called()
