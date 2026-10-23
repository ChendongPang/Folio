import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from folio.models import Block, Claim, Evidence, Finding, JobInput, Review, Verdict, digest
from folio.orchestration.engine import Engine
from folio.orchestration.work import GapProposal, WebSearchAttempt
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


@pytest.fixture
async def saved_job(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    engine = Engine(store, DemoRuntime())
    await engine.run(job_id)
    section = engine.plan.sections[0]
    previous = next(
        state for state in store.states(job_id) if state.section_id == section.section_id
    )
    task = engine.owner(section.section_id)
    work = store.work_states(job_id)[task.task_id]
    work.searches = [["round1"], ["round2"], ["round3"]]
    work.research_instruction = "已有研究目标"
    store.save_work_state(job_id, work)
    finding = Finding(
        kind="coverage",
        key="editorial",
        description="删除编写说明",
        section_ids=[section.section_id],
    )
    engine.validate_section = AsyncMock(return_value=previous)
    return engine, section, previous, task, work, finding


def forbid_research(engine):
    engine.research_work = AsyncMock(side_effect=AssertionError("Unnecessary research"))
    engine.complete_gaps = AsyncMock(side_effect=AssertionError("Repeated gap completion"))


def complete_and_save_proposals(engine):
    """Model gap completion's durable side effect, then exercise real integration."""

    async def complete(task, work, gaps, section_ids=None):
        proposals = [
            GapProposal(
                gap_id=digest(gap),
                section_ids=section_ids or task.section_ids,
                content=f"针对{gap}补充适用范围和审批约束，未经主管审批不得继续业务处理。",
            )
            for gap in gaps
        ]
        work.missing = list(dict.fromkeys(work.missing + gaps))
        work.proposals.extend(proposals)
        engine.store.save_work_state(engine.job_id, work)
        return proposals

    engine.complete_gaps = AsyncMock(side_effect=complete)
    engine.runtime.complete = AsyncMock(wraps=engine.runtime.complete)


def integration_contexts(engine):
    return [
        call.args[1]
        for call in engine.runtime.complete.call_args_list
        if call.args[0] == "integrate_supplement"
    ]


@pytest.mark.parametrize("still_missing", [False, True])
async def test_node_research_never_assigns_other_nodes_gaps_to_current_writer(
    saved_job, monkeypatch, still_missing
):
    engine, section, previous, task, work, _ = saved_job
    local_gap, other_gap = "本节审批条件", "其他章节的渠道范围"
    work.searches = [["first search"]]
    work.missing = [other_gap]
    other = GapProposal(
        gap_id=digest(other_gap),
        section_ids=["other-section"],
        content="渠道范围由其他章节统一描述；本节只说明审批约束，不重复渠道清单。",
    )
    work.proposals = [other]
    engine.store.save_work_state(engine.job_id, work)
    candidate = previous.content.model_copy(deep=True)
    candidate.pending_items = [local_gap]
    engine.call = AsyncMock(return_value=candidate)

    async def research(*args, **kwargs):
        refreshed = engine.store.work_states(engine.job_id)[task.task_id]
        refreshed.searches = [["first"], ["second"], ["third"]]
        refreshed.missing = [other_gap] + ([local_gap] if still_missing else [])
        engine.store.save_work_state(engine.job_id, refreshed)
        return refreshed

    engine.research_work = AsyncMock(side_effect=research)
    complete_and_save_proposals(engine)
    integrations = []

    async def integrate(_engine, source, proposals, *, supported_gaps=()):
        integrations.append((proposals, list(supported_gaps)))
        return candidate

    monkeypatch.setattr(
        "folio.orchestration.supplement_integration.integrate_supplements", integrate
    )
    await engine.generate(section)
    assert engine.research_work.call_args.kwargs["writing_gaps"] == [local_gap]
    assert engine.complete_gaps.await_count == int(still_missing)
    if still_missing:
        assert engine.complete_gaps.call_args.args[2:] == ([local_gap], [section.section_id])
    saved = engine.store.work_states(engine.job_id)[task.task_id]
    assert next(p for p in saved.proposals if p.gap_id == other.gap_id) == other
    assert all(p["gap_id"] != other.gap_id for proposals, _ in integrations for p in proposals)
    assert integrations[0][1] == ([] if still_missing else [local_gap])


async def test_local_deletion_reuses_existing_research_and_retains_previous_sources(saved_job):
    engine, section, previous, task, work, finding = saved_job
    extra = Evidence(
        evidence_id="historical-source",
        source_id="doc",
        source_version="1",
        locator="1",
        text="既有业务规则",
    )
    previous.evidence.append(extra)
    previous.content.blocks.append(
        Block(block_id="note", text="编写说明", evidence_ids=[extra.evidence_id])
    )
    candidate = previous.content.model_copy(deep=True)
    candidate.blocks.pop()
    before = work.model_dump()
    engine.call = AsyncMock(return_value=candidate)
    forbid_research(engine)

    await engine.generate(section, previous, finding, "只删除 note 编写说明")

    engine.research_work.assert_not_awaited()
    engine.complete_gaps.assert_not_awaited()
    assert engine.call.await_count == 1
    assert engine.call.call_args.args[0] == "repair"
    assert extra.evidence_id in {
        e["evidence_id"] for e in engine.call.call_args.args[1]["evidence"]
    }
    assert extra.evidence_id in {e.evidence_id for e in engine.validate_section.call_args.args[2]}
    assert engine.store.work_states(engine.job_id)[task.task_id].model_dump() == before


@pytest.mark.parametrize("new_gap", [False, True])
async def test_repair_brings_only_current_supported_dependency_sources(saved_job, new_gap):
    engine, section, previous, task, _, finding = saved_job
    upstream_task = next(other for other in engine.work_plan.tasks if other.task_id != task.task_id)
    task.depends_on = [upstream_task.task_id]
    upstream = next(
        state
        for state in engine.store.states(engine.job_id)
        if state.section_id in upstream_task.section_ids
    )
    upstream = upstream.model_copy(deep=True)
    upstream.revision += 1
    extra_ids = ["new-supported-source", "unconfirmed-source", "unused-upstream-source"]
    upstream.evidence.extend(
        Evidence(
            evidence_id=eid,
            source_id="upstream-doc",
            source_version="2",
            locator=eid,
            text="上游修订后新增的业务规则",
        )
        for eid in extra_ids
    )
    upstream.extraction.claims.extend(
        Claim(
            claim_id=eid,
            block_ids=[upstream.content.blocks[0].block_id],
            statement="新业务规则",
            subject="上游",
            predicate="规则",
            value=eid,
            evidence_ids=[eid],
            status="supported" if index == 0 else "candidate",
        )
        for index, eid in enumerate(extra_ids[:2])
    )
    # The fixture represents a completed revalidation of the new revision;
    # setting an extraction's status alone must not bypass the support verdict.
    upstream.claim_verdicts.extend(
        Verdict(claim_id=eid, supported=index == 0, reason="已核对上游修订依据")
        for index, eid in enumerate(extra_ids[:2])
    )
    upstream.validation_completed = True
    engine.store.save_section(engine.job_id, upstream)
    unrelated = upstream.model_copy(deep=True, update={"section_id": "unrelated"})
    unrelated.evidence = [
        upstream.evidence[-1].model_copy(update={"evidence_id": "unrelated-source"})
    ]
    unrelated.extraction.claims = [
        upstream.extraction.claims[-2].model_copy(update={"evidence_ids": ["unrelated-source"]})
    ]
    engine.store.save_section(engine.job_id, unrelated)
    upstream_work = engine.store.work_states(engine.job_id)[upstream_task.task_id]
    upstream_work.review_key = "revised-upstream-reviewed"
    upstream_work.review_revisions = {
        state.section_id: state.revision
        for state in engine.store.states(engine.job_id)
        if state.section_id in upstream_task.section_ids
    }
    upstream_work.findings = []
    engine.store.save_work_state(engine.job_id, upstream_work)

    candidate = previous.content.model_copy(deep=True)
    candidate.blocks.append(
        Block(block_id="upstream-rule", text="应用新上游业务规则", evidence_ids=[extra_ids[0]])
    )
    if new_gap:
        candidate.pending_items = ["新的其他缺口"]
    engine.call = AsyncMock(return_value=candidate)
    engine.research_work = AsyncMock(side_effect=AssertionError("No research needed"))
    complete_and_save_proposals(engine)

    await engine.generate(section, previous, finding, "按上游修订更新当前规则")

    assert engine.call.await_count == 1
    scoped = integration_contexts(engine)
    assert len(scoped) == int(new_gap)
    for ctx in [engine.call.call_args.args[1], *scoped]:
        available = {e["evidence_id"] for e in ctx["evidence"]}
        assert "new-supported-source" in available
        assert not {"unconfirmed-source", "unused-upstream-source", "unrelated-source"} & available
        assert any(
            fact["evidence_ids"] == ["new-supported-source"]
            for dependency in ctx["dependencies"]
            for node in dependency["sections"]
            for fact in node["supported_facts"]
        )
    validated = {e.evidence_id for e in engine.validate_section.call_args.args[2]}
    assert "new-supported-source" in validated
    assert not {"unconfirmed-source", "unused-upstream-source", "unrelated-source"} & validated
    if new_gap:
        saved = engine.store.work_states(engine.job_id)[task.task_id]
        new_proposal = next(p for p in saved.proposals if p.gap_id == digest("新的其他缺口"))
        assert scoped[0]["model_proposals"] == [new_proposal.model_dump()]
        written = engine.validate_section.call_args.args[1]
        assert written.blocks[: len(candidate.blocks)] == candidate.blocks
        assert written.blocks[-1].text == new_proposal.content
        assert written.blocks[-1].origin == "model_proposal"
        assert written.blocks[-1].evidence_ids == []
    engine.research_work.assert_not_awaited()


@pytest.mark.parametrize("known_as", ["previous_pending", "work_missing", "completed_proposal"])
async def test_existing_gap_is_not_researched_or_completed_again(saved_job, known_as):
    engine, section, previous, task, work, finding = saved_job
    gap = "已有待确认信息"
    if known_as == "previous_pending":
        previous.content.pending_items = [gap]
    elif known_as == "work_missing":
        work.missing = [gap]
    else:
        work.proposals = [
            GapProposal(
                gap_id=digest(gap),
                section_ids=[section.section_id],
                content="这是已有并已经完成的业务方案内容，不能重复生成。",
            )
        ]
    engine.store.save_work_state(engine.job_id, work)
    candidate = previous.content.model_copy(deep=True)
    candidate.pending_items = [gap]
    engine.call = AsyncMock(return_value=candidate)
    forbid_research(engine)
    await engine.generate(section, previous, finding, "只删除编写说明")
    engine.research_work.assert_not_awaited()
    engine.complete_gaps.assert_not_awaited()
    assert engine.call.await_count == 1
    assert engine.validate_section.call_args.args[1].pending_items == [gap]


@pytest.mark.parametrize("kind", ["untraced", "model_proposal"])
async def test_unchanged_old_blocks_do_not_start_new_research(saved_job, kind):
    engine, section, previous, _, work, finding = saved_job
    previous.content.blocks.append(
        Block(
            block_id="old",
            text="旧的业务说明",
            origin="model_proposal" if kind == "model_proposal" else "evidence",
        )
    )
    work.searches = [["round1"]]
    engine.store.save_work_state(engine.job_id, work)
    engine.call = AsyncMock(return_value=previous.content.model_copy(deep=True))
    forbid_research(engine)
    await engine.generate(section, previous, finding, "只修其他块")
    engine.research_work.assert_not_awaited()
    assert engine.call.await_count == 1


@pytest.mark.parametrize(
    "searches,web_attempts,expected_research", [(2, 2, True), (3, 1, True), (3, 2, False)]
)
async def test_only_new_gap_uses_remaining_internal_or_web_allowance(
    saved_job, searches, web_attempts, expected_research
):
    engine, section, previous, task, work, finding = saved_job
    previous.content.pending_items = ["旧缺口"]
    work.searches = [[f"round{i}"] for i in range(searches)]
    work.web_searches = [WebSearchAttempt() for _ in range(web_attempts)]
    engine.web_knowledge = object()
    engine.store.save_work_state(engine.job_id, work)
    candidate = previous.content.model_copy(deep=True)
    candidate.pending_items = ["旧缺口", "新缺口"]
    engine.call = AsyncMock(return_value=candidate)
    researched = work.model_copy(deep=True)
    researched.missing = ["旧缺口", "新缺口"]
    engine.research_work = AsyncMock(return_value=researched)
    complete_and_save_proposals(engine)

    await engine.generate(section, previous, finding, "更新某条规则")

    assert engine.research_work.await_count == int(expected_research)
    if expected_research:
        assert engine.research_work.call_args.args == (task, "已有研究目标")
        assert engine.research_work.call_args.kwargs == {"writing_gaps": ["新缺口"]}
    assert engine.complete_gaps.call_args.args[2] == ["新缺口"]
    assert engine.call.await_count == 1
    scoped = integration_contexts(engine)
    assert len(scoped) == 1
    assert scoped[0]["integration_item"]["gap_id"] == digest("新缺口")
    assert len(scoped[0]["model_proposals"]) == 1
    assert scoped[0]["model_proposals"][0]["gap_id"] == digest("新缺口")
    written = engine.validate_section.call_args.args[1]
    assert written.blocks[: len(candidate.blocks)] == candidate.blocks
    assert written.blocks[-1].text == scoped[0]["integration_item"]["content"]
    assert written.blocks[-1].origin == "model_proposal" and written.blocks[-1].evidence_ids == []
    assert written.pending_items == ["旧缺口"]
    assert len(work.searches) == searches and len(work.web_searches) == web_attempts


async def test_generate_integrates_proposal_and_newly_supported_gap_with_distinct_provenance(
    saved_job,
):
    engine, section, previous, task, work, finding = saved_job
    work.searches = [["initial-search"]]
    engine.store.save_work_state(engine.job_id, work)
    candidate = previous.content.model_copy(deep=True)
    candidate.pending_items = ["需要模型方案", "补查找到原文依据"]
    engine.call = AsyncMock(return_value=candidate)
    answered = Evidence(
        evidence_id="newly_retrieved_answer",
        source_id="original-project-rule",
        source_version="2",
        locator="业务约束",
        text="补查找到原文依据：审批通过后才允许交割，未经审批不得交割。",
    )

    async def research(*args, **kwargs):
        assert kwargs["writing_gaps"] == candidate.pending_items
        found = work.model_copy(deep=True)
        found.missing = ["需要模型方案"]
        found.evidence.append(answered)
        engine.store.save_work_state(engine.job_id, found)
        return found

    engine.research_work = AsyncMock(side_effect=research)
    complete_and_save_proposals(engine)
    demo_complete = engine.runtime.complete

    async def complete(operation, context, schema, session):
        if operation == "select_context" and context["purpose"]["supplement"].get(
            "evidence_refresh"
        ):
            return RuntimeResult(
                json.dumps(
                    {
                        "selected_refs": [
                            item["context_ref"]
                            for item in context["catalog"]
                            if item["kind"] == "evidence"
                            and item["value"]["evidence_id"] == answered.evidence_id
                        ],
                        "risk_refs": [],
                        "risk_summary": "",
                    }
                )
            )
        return await demo_complete(operation, context, schema, session)

    engine.runtime.complete = AsyncMock(side_effect=complete)
    await engine.generate(section, previous, finding, "核对新业务缺口")
    engine.research_work.assert_awaited_once()
    assert engine.complete_gaps.call_args.args[2] == ["需要模型方案"]
    scoped = integration_contexts(engine)
    assert len(scoped) == 2
    assert scoped[0]["model_proposals"][0]["gap_id"] == digest("需要模型方案")
    assert not scoped[0]["integration_item"].get("evidence_refresh")
    refresh = scoped[1]
    assert refresh["integration_item"]["evidence_refresh"] is True
    assert refresh["integration_item"]["gap_description"] == "补查找到原文依据"
    assert refresh["model_proposals"] == []
    assert [item["evidence_id"] for item in refresh["evidence"]] == [answered.evidence_id]
    written = engine.validate_section.call_args.args[1]
    assert written.blocks[: len(candidate.blocks)] == candidate.blocks
    assert written.blocks[-2].origin == "model_proposal" and written.blocks[-2].evidence_ids == []
    assert written.blocks[-1].origin == "evidence"
    assert written.blocks[-1].text == answered.text
    assert written.blocks[-1].evidence_ids == [answered.evidence_id]
    assert written.pending_items == []
    validated_sources = engine.validate_section.call_args.args[2]
    assert answered in validated_sources
    assert {item.evidence_id for item in previous.evidence} <= {
        item.evidence_id for item in validated_sources
    }
    saved = engine.store.work_states(engine.job_id)[task.task_id]
    assert {p.gap_id for p in saved.proposals} == {digest("需要模型方案")}


async def test_new_untraced_block_still_requests_missing_provenance(saved_job):
    engine, section, previous, _, _, finding = saved_job
    candidate = previous.content.model_copy(deep=True)
    candidate.blocks.append(Block(block_id="new", text="新增但没有来源的规则"))
    engine.call = AsyncMock(return_value=candidate)
    engine.research_work = AsyncMock(side_effect=AssertionError("All searches spent"))
    engine.complete_gaps = AsyncMock(return_value=[])
    await engine.generate(section, previous, finding, "修复当前内容")
    assert "Body blocks new are marked evidence-backed but have no citations" in engine.complete_gaps.call_args.args[2][0]
    engine.research_work.assert_not_awaited()


async def test_new_proposal_still_searches_when_only_old_pending_remains(saved_job):
    engine, section, previous, _, work, finding = saved_job
    previous.content.pending_items = ["旧缺口"]
    work.searches = [["round1"]]
    engine.store.save_work_state(engine.job_id, work)
    candidate = previous.content.model_copy(deep=True)
    candidate.blocks.append(
        Block(block_id="new", text="新增模型补写的规则", origin="model_proposal")
    )
    engine.call = AsyncMock(return_value=candidate)
    engine.research_work = AsyncMock(return_value=work)
    engine.complete_gaps = AsyncMock()

    await engine.generate(section, previous, finding, "修复当前内容")

    engine.research_work.assert_awaited_once()
    gaps = engine.research_work.call_args.kwargs["writing_gaps"]
    assert len(gaps) == 1 and "Supplemental content lacks evidence" in gaps[0]
    assert "旧缺口" not in gaps


async def test_gap_resolved_by_sibling_before_lock_does_not_repair_twice(saved_job):
    engine, section, previous, _, work, finding = saved_job
    candidate = previous.content.model_copy(deep=True)
    candidate.pending_items = ["并发新缺口"]

    async def sibling_completes(*_):
        work.proposals.append(
            GapProposal(
                gap_id=digest("并发新缺口"),
                section_ids=[section.section_id],
                content="并发任务已经生成完整业务方案，当前节点应交现有验证流程核查。",
            )
        )
        engine.store.save_work_state(engine.job_id, work)
        return candidate

    engine.call = AsyncMock(side_effect=sibling_completes)
    forbid_research(engine)
    await engine.generate(section, previous, finding, "修复当前内容")
    assert engine.call.await_count == 1
    engine.research_work.assert_not_awaited()
    engine.complete_gaps.assert_not_awaited()
    assert engine.validate_section.call_args.args[1].pending_items == ["并发新缺口"]


async def test_review_resumes_saved_body_without_restarting_interrupted_research(saved_job):
    engine, _, _, task, work, _ = saved_job
    work.researched = False
    work.review_key = ""
    work.missing = ["仍然保留的未完成缺口"]
    engine.store.save_work_state(engine.job_id, work)
    engine.call = AsyncMock(return_value=Review())
    forbid_research(engine)
    findings = await engine.review_business(task)
    engine.research_work.assert_not_awaited()
    assert engine.call.call_args.args[0] == "review_work"
    assert any(finding.description == work.missing[0] for finding in findings)
    saved = engine.store.work_states(engine.job_id)[task.task_id]
    assert saved.researched is False and saved.missing == work.missing


@pytest.mark.parametrize(
    "missing_initial_state", ["not_writable", "no_evidence", "incomplete_body"]
)
async def test_review_still_researches_without_ready_saved_body(saved_job, missing_initial_state):
    engine, _, _, task, work, _ = saved_job
    work.researched = False
    work.review_key = ""
    if missing_initial_state == "not_writable":
        work.writable = False
    elif missing_initial_state == "no_evidence":
        work.evidence = []
    else:
        task = task.model_copy(update={"section_ids": task.section_ids + ["not_saved"]})
    engine.store.save_work_state(engine.job_id, work)
    engine.research_work = AsyncMock(return_value=deepcopy(work))
    engine.call = AsyncMock(return_value=Review())
    await engine.review_business(task)
    engine.research_work.assert_awaited_once()
