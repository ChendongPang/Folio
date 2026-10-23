"""Agent contracts and shared facts remain correct across waivers and revisions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_business_work import GroupRuntime, document, task

from folio.decisions import ActionDecision
from folio.models import (
    Block,
    Claim,
    Evidence,
    Extraction,
    Finding,
    JobInput,
    Review,
    SectionContent,
    SectionState,
    Verdict,
)
from folio.orchestration.agents import BusinessAgent, RepairAssignment
from folio.orchestration.context import ContextSelection, apply_selection, dependency_catalog
from folio.orchestration.engine import Engine
from folio.orchestration.local_checks import finding_version
from folio.orchestration.state import hierarchical_state, registry
from folio.orchestration.work import BusinessRecord, WorkPlan, WorkState


def section_state(*, supported=True):
    return SectionState(
        section_id="a",
        content=SectionContent(
            blocks=[
                Block(
                    block_id="rule",
                    kind="paragraph",
                    text="交易撤销由主管审批",
                    evidence_ids=["source"],
                )
            ]
        ),
        extraction=Extraction(
            summary="交易审批",
            claims=[
                Claim(
                    claim_id="approval",
                    block_ids=["rule"],
                    statement="交易撤销由主管审批",
                    subject="交易",
                    predicate="审批角色",
                    value="主管",
                    evidence_ids=["source"],
                )
            ],
        ),
        evidence=[
            Evidence(
                evidence_id="source",
                source_id="policy",
                source_version="v1",
                locator="第一段",
                text="交易撤销由主管审批",
            )
        ],
        claim_verdicts=[Verdict(claim_id="approval", supported=supported, reason="核对原文")],
        validation_completed=True,
    )


def finding(description="审批规则似乎缺失"):
    return Finding(kind="coverage", key="approval", description=description, section_ids=["a"])


def test_waiver_restores_positive_verdict_facts_without_mutating_audit():
    state = section_state()
    issue = finding()
    state.local_findings = [issue]
    before = state.model_dump()
    assert not hierarchical_state(document(["a"]), [state])["records"]
    dismissed = {finding_version(issue, [state]): "原文已经说明"}
    records = hierarchical_state(document(["a"]), [state], dismissed=dismissed)["records"]
    assert records["a:approval"]["status"] == "supported"
    assert state.model_dump() == before
    state.revision += 1
    assert not hierarchical_state(document(["a"]), [state], dismissed=dismissed)["records"]


def test_shared_views_exclude_stored_nodes_outside_confirmed_directory():
    state = section_state()
    obsolete = state.model_copy(deep=True, update={"section_id": "obsolete"})
    obsolete.extraction.claims[0].value = "其他规则"
    obsolete.extraction.claims[0].cardinality = "single"
    state.extraction.claims[0].cardinality = "single"
    plan = document(["a"])
    records, _ = registry([state, obsolete], plan=plan)
    assert set(records) == {"a:approval"}
    view = hierarchical_state(plan, [state, obsolete])
    assert set(view["records"]) == {"a:approval"}
    assert set(view["chapters"]) == {"a"}
    assert view["disputed_keys"] == []


@pytest.mark.parametrize("change", ["negative", "unknown", "proposal", "missing_source"])
def test_waiver_cannot_supply_missing_claim_support(change):
    state = section_state(supported=change != "negative")
    state.local_findings = [finding()]
    if change == "unknown":
        state.claim_verdicts = []
    elif change == "proposal":
        state.content.blocks[0].origin = "model_proposal"
    elif change == "missing_source":
        state.evidence = []
    dismissed = {finding_version(f, [state]): "排除" for f in state.local_findings}
    assert not hierarchical_state(document(["a"]), [state], dismissed=dismissed)["records"]


def test_task_review_waiver_is_shared_by_dependency_and_fact_views(store, inputs):
    engine = Engine(store, GroupRuntime())
    engine.request = JobInput(**inputs, runtime="demo")
    engine.job_id = store.create(engine.request)
    engine.plan = document()
    engine.work_plan = WorkPlan(tasks=[task("source", ["a"]), task("consumer", ["b"], ["source"])])
    store.save_work_plan(engine.job_id, engine.work_plan)
    state = section_state()
    store.save_section(engine.job_id, state)
    issue = finding()
    work = WorkState(
        task_id="source", review_key="review-a-v1", review_revisions={"a": 1}, findings=[issue]
    )
    store.save_work_state(engine.job_id, work)
    assert not engine.shared_state()["records"]
    version = finding_version(issue, [state], engine.work_plan, {"source": work})
    engine.issue_ledger().dismiss(version, "原文已覆盖")
    dependency = engine.dependency_context(engine.work_plan.tasks[1])[0]
    assert dependency["findings"] == []
    assert dependency["sections"][0]["supported_facts"][0]["claim_id"] == "approval"
    assert engine.shared_state()["records"]["a:approval"]
    assert store.work_states(engine.job_id)["source"].findings == [issue]
    state.revision += 1
    store.save_section(engine.job_id, state)
    assert not engine.shared_state()["records"]


def test_multi_owner_risk_selection_does_not_reuse_mixed_summary():
    risks = [finding("审批角色缺失").model_dump(), finding("退款通知缺失").model_dump()]
    catalog, objects, views = dependency_catalog(
        [
            {"task_id": owner, "title": owner, "sections": [], "findings": [risk]}
            for owner, risk in zip(["approval", "refund"], risks, strict=True)
        ]
    )
    apply_selection(
        views,
        objects,
        ContextSelection(
            risk_refs=[item["context_ref"] for item in catalog],
            risk_summary="审批角色和退款通知缺失",
        ),
    )
    for view, risk in zip(views, risks, strict=True):
        brief = view["risk_briefs"][0]
        assert brief["summary"] != "审批角色和退款通知缺失"
        assert [item["value"] for item in brief["originals"]] == [risk]


async def test_shared_fact_includes_original_evidence_without_a_dependency_edge(store, inputs):
    engine = Engine(store, GroupRuntime())
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan = document()
    engine.plan.sections[1].title = "交易审批"
    engine.work_plan = WorkPlan(tasks=[task("source", ["a"]), task("consumer", ["b"])])
    store.save_work_plan(engine.job_id, engine.work_plan)
    state = section_state()
    store.save_section(engine.job_id, state)
    store.save_work_state(
        engine.job_id,
        WorkState(
            task_id="source",
            review_key="review-a",
            review_revisions={"a": 1},
        ),
    )
    engine.references = SimpleNamespace(evidence=lambda *_: [])
    engine.call = AsyncMock(
        side_effect=[
            BusinessRecord(),
            ActionDecision(
                action="write",
                summary="共享资料已经足够",
            ),
        ]
    )
    work = await engine.research_work(engine.work_plan.tasks[1])
    assert work.global_facts["b"][0]["record_id"] == "a:approval"
    assert work.evidence == state.evidence
    assert engine.call.call_args_list[0].args[1]["evidence"] == [state.evidence[0].model_dump()]


async def test_business_review_keeps_distinct_issues_with_same_model_key(store, inputs):
    engine = Engine(store, GroupRuntime())
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await engine.run(job_id)
    owner = engine.work_plan.tasks[0]
    work = store.work_states(job_id)[owner.task_id]
    work.review_key = ""
    store.save_work_state(job_id, work)
    first = finding("审批角色缺失").model_copy(update={"section_ids": owner.section_ids})
    second = first.model_copy(update={"description": "审批超时处理缺失"})
    engine.call = AsyncMock(return_value=Review(findings=[first, second]))
    result = await engine.review_business(owner)
    assert {f.description for f in result} == {first.description, second.description}
    assert len({f.fingerprint for f in result}) == 2
    assert len(store.work_states(job_id)[owner.task_id].findings) == 2


async def test_agent_receipt_and_stale_assignment_guard(store, inputs):
    engine = Engine(store, GroupRuntime())
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await engine.run(job_id)
    receipts = list((store.root / "jobs" / job_id / "contexts" / "agent_results").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    # archive_context writes the value directly.
    assert receipt["task_id"] == engine.work_plan.tasks[0].task_id
    assert receipt["section_revisions"]
    agent = BusinessAgent(engine)
    assignment = agent.assignment(engine.work_plan.tasks[0])
    assignment.plan_version = "obsolete"
    with pytest.raises(ValueError, match="confirmed plan"):
        await agent.run(assignment)


def test_repair_contract_rejects_other_owner_and_stale_revision():
    state = section_state()
    host = SimpleNamespace(
        work_plan=WorkPlan(tasks=[task("a_owner", ["a"]), task("b_owner", ["b"])]),
        store=SimpleNamespace(states=lambda _: [state]),
        job_id="job",
        archive_context=lambda *_: None,
    )
    agent = BusinessAgent(host)
    assignment = RepairAssignment(
        task_id="a_owner",
        expected_revisions={"a": 1},
        finding=finding(),
        instruction="补充审批规则",
    )
    assert agent.prepare_repair(assignment).task_id == "a_owner"
    with pytest.raises(ValueError, match="stale"):
        agent.prepare_repair(assignment.model_copy(update={"expected_revisions": {"a": 2}}))
    with pytest.raises(ValueError, match="owned"):
        agent.prepare_repair(assignment.model_copy(update={"task_id": "b_owner"}))
