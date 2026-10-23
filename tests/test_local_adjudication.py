import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from folio.i18n import translate_text
from folio.models import (
    Block,
    Claim,
    DrillDown,
    Evidence,
    Extraction,
    Finding,
    JobInput,
    Section,
    SectionContent,
    digest,
)
from folio.orchestration.engine import Engine
from folio.orchestration.local_checks import hard_findings
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


@pytest.mark.parametrize(
    "failure,kind,key,description",
    [
        ("empty", "coverage", "empty-section", "内容章节不能为空"),
        ("table", "coverage", "required-table", "缺少本节要求的表格及列名"),
        ("table_order", "coverage", "required-table", "缺少本节要求的表格及列名"),
        ("required_source", "coverage", "e2", "未覆盖项目材料：附件二"),
        ("heading_jump", "coverage", "heading", "正文子标题不能跳级"),
        ("heading_escape", "coverage", "heading", "正文子标题不能越过当前章节层级"),
        ("block_ref", "unsupported", "body", "正文引用了未检索到的证据"),
        (
            "block_empty_ref",
            "unsupported",
            "body",
            "Body text is marked evidence-backed but lacks citations; verify the evidence or treat it as model-proposed content.",
        ),
        ("claim_target", "extraction", "claim", "事实指向不存在的正文块"),
        ("claim_ref", "unsupported", "claim", "事实缺少有效证据引用"),
        ("claim_empty_ref", "unsupported", "claim", "事实缺少有效证据引用"),
        ("pending", "pending", "pending_0", "尚待项目确认"),
    ],
)
def test_extracted_hard_checks_keep_all_rules_and_fingerprints(failure, kind, key, description):
    section = Section(
        section_id="s",
        node_id="s",
        title="审批",
        level=1,
        display_number="1",
        instruction="规则",
    )
    content = SectionContent(blocks=[Block(block_id="body", text="必须审批", evidence_ids=["e1"])])
    evidence = [
        Evidence(
            evidence_id="e1", source_id="doc", source_version="1", locator="正文", text="必须审批"
        )
    ]
    extraction = Extraction(
        summary="审批",
        claims=[
            Claim(
                claim_id="claim",
                block_ids=["body"],
                statement="必须审批",
                subject="交易",
                predicate="条件",
                value="审批",
                evidence_ids=["e1"],
            )
        ],
    )
    required = []
    if failure == "empty":
        content.blocks = []
        extraction.claims = []
    elif failure in {"table", "table_order"}:
        section.expected_table_headers = ["名称", "说明"]
        if failure == "table_order":
            content.blocks = [
                Block(
                    block_id="body",
                    kind="table",
                    rows=[["说明", "名称"], ["审批", "交易"]],
                    evidence_ids=["e1"],
                )
            ]
    elif failure == "required_source":
        evidence.append(evidence[0].model_copy(update={"evidence_id": "e2", "locator": "附件二"}))
        required = ["e2"]
    elif failure in {"heading_jump", "heading_escape"}:
        content.blocks.append(
            Block(
                block_id="heading",
                kind="heading",
                text="子标题",
                heading_level=4 if failure == "heading_jump" else 1,
                evidence_ids=["e1"],
            )
        )
    elif failure in {"block_ref", "block_empty_ref"}:
        content.blocks[0].evidence_ids = ["unknown"] if failure == "block_ref" else []
    elif failure == "claim_target":
        extraction.claims[0].block_ids = ["unknown"]
    elif failure in {"claim_ref", "claim_empty_ref"}:
        extraction.claims[0].evidence_ids = ["unknown"] if failure == "claim_ref" else []
    else:
        content.pending_items = ["尚待项目确认"]
    before = [section.model_dump(), content.model_dump(), extraction.model_dump()]
    findings = hard_findings(section, content, evidence, extraction, required)
    expected = Finding(
        kind=kind,
        key=key,
        description=translate_text(description, "en"),
        section_ids=["s"],
        # Missing citations now identify the exact factual block, so a waiver for
        # model-proposed text elsewhere cannot hide this unsupported statement.
        block_refs=[{"section_id": "s", "block_id": "body"}]
        if failure == "block_empty_ref"
        else [],
    )
    assert expected in findings
    assert next(f for f in findings if f == expected).fingerprint == expected.fingerprint
    legacy = expected.model_copy(update={"description": description})
    assert legacy.fingerprint == expected.fingerprint
    if failure == "block_empty_ref":
        unlocated = Finding(kind=kind, key=key, description=description, section_ids=["s"])
        assert expected.fingerprint != unlocated.fingerprint
    assert [section.model_dump(), content.model_dump(), extraction.model_dump()] == before


class RejectFinding(DemoRuntime):
    def __init__(self, needs_confirmation=False):
        self.needs_confirmation = needs_confirmation
        self.adjudicated = []

    async def complete(self, task, context, schema, session_key):
        if task == "drill_down":
            self.adjudicated.append(context["finding"])
            return RuntimeResult(
                json.dumps(
                    {
                        "confirmed": False,
                        "reason": "对照原文后，此语义问题不成立。",
                        "needs_confirmation": self.needs_confirmation,
                        "affected_section_ids": [],
                        "repair_instruction": "",
                    },
                    ensure_ascii=False,
                )
            )
        if task == "repair":
            raise AssertionError("Rejected findings cannot rewrite the document")
        return await super().complete(task, context, schema, session_key)


@pytest.fixture
async def saved_job(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    engine = Engine(store, DemoRuntime())
    await engine.run(job_id)
    state = store.states(job_id)[0]
    return engine, state


@pytest.mark.parametrize("needs_confirmation", [False, True])
async def test_rejected_local_semantic_findings_continue_unless_confirmation_needed(
    saved_job, needs_confirmation
):
    engine, state = saved_job
    findings = [
        Finding(
            kind="unsupported",
            key=f"semantic-{i}",
            description=f"模型语义误报{i}",
            section_ids=[state.section_id],
        )
        for i in range(2)
    ]
    state.local_findings = findings
    engine.store.save_section(engine.job_id, state)
    engine.store.update(engine.job_id, status="failed", stage="review")
    engine.store.resume_job(engine.job_id)
    runtime = RejectFinding(needs_confirmation)
    restored = Engine(engine.store, runtime)
    job = await restored.run(engine.job_id)
    if needs_confirmation:
        assert job["status"] == "needs_confirmation"
        assert not restored._dismissed()
        assert len(runtime.adjudicated) == 2  # Independent reads complete despite a pending item.
    else:
        assert job["status"] == "draft" and job["snapshot"]["review_passed"]
        assert job["snapshot"]["blocking_issues"] == []
        assert len(runtime.adjudicated) == len(restored._dismissed()) == 2
    saved = next(s for s in engine.store.states(engine.job_id) if s.section_id == state.section_id)
    assert saved == state  # Keep original findings as audit; no rewriting or false promotion.
    assert Path(job["artifact"]).is_file()


@pytest.mark.parametrize("failure", ["block_ref", "table"])
async def test_real_hard_failure_cannot_be_dismissed_by_semantic_adjudication(saved_job, failure):
    engine, state = saved_job
    section = next(s for s in engine.plan.sections if s.section_id == state.section_id)
    if failure == "block_ref":
        state.content.blocks[0].evidence_ids = ["unknown"]
        finding = Finding(
            kind="unsupported",
            key=state.content.blocks[0].block_id,
            description="正文引用了未检索到的证据",
            section_ids=[state.section_id],
        )
    else:
        section.expected_table_headers = ["名称", "说明"]
        engine.store.save_plan(engine.job_id, engine.plan)
        finding = Finding(
            kind="coverage",
            key="required-table",
            description="缺少本节要求的表格及列名",
            section_ids=[state.section_id],
        )
    state.local_findings = [finding]
    engine.store.save_section(engine.job_id, state)
    engine.store.update(engine.job_id, status="failed", stage="review")
    engine.store.resume_job(engine.job_id)
    runtime = RejectFinding()
    restored = Engine(engine.store, runtime)
    job = await restored.run(engine.job_id)
    assert job["status"] == "needs_confirmation"
    assert job["snapshot"]["review_passed"] is False
    assert len(runtime.adjudicated) == 1
    assert restored._dismissed() == {}
    assert any(f["key"] == finding.key for f in job["snapshot"]["blocking_issues"])
    assert (
        next(s for s in engine.store.states(engine.job_id) if s.section_id == state.section_id)
        == state
    )


async def test_review_recomputes_missing_hard_finding_despite_current_dismissal(saved_job):
    engine, state = saved_job
    state.content.blocks[0].evidence_ids = ["unknown"]
    state.local_findings = []
    engine.store.save_section(engine.job_id, state)
    finding = Finding(
        kind="unsupported",
        key=state.content.blocks[0].block_id,
        description="正文引用了未检索到的证据",
        section_ids=[state.section_id],
    )
    engine._dismiss(finding, engine.store.states(engine.job_id), "历史误判记录不能豁免硬规则")
    engine.store.update(engine.job_id, status="failed", stage="review")
    engine.store.resume_job(engine.job_id)
    restored = Engine(engine.store, RejectFinding())
    restored.business_findings = AsyncMock(return_value=[])
    restored.global_review = AsyncMock(return_value=[])
    job = await restored.run(engine.job_id)
    assert job["status"] == "needs_confirmation"
    assert any(
        Finding.model_validate(value).fingerprint == finding.fingerprint
        for value in job["snapshot"]["blocking_issues"]
    )


async def test_pure_evidence_document_publishes_after_current_semantic_adjudication(store, inputs):
    class SemanticFalsePositive(RejectFinding):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "validate":
                data = json.loads(result.content)
                data["findings"] = [
                    {
                        "kind": "unsupported",
                        "key": "semantic-only",
                        "description": "模型语义误报",
                        "section_ids": [context["section"]["section_id"]],
                    }
                ]
                return RuntimeResult(json.dumps(data, ensure_ascii=False))
            return result

    job_id = store.create(JobInput(**inputs, runtime="llm"))
    engine = Engine(store, SemanticFalsePositive())
    job = await engine.run(job_id)
    assert job["status"] == "published" and job["snapshot"]["review_passed"]
    assert all(state.local_findings for state in store.states(job_id))
    assert len(engine._dismissed()) == len(store.states(job_id))
    assert all(
        block.origin == "evidence"
        for state in store.states(job_id)
        for block in state.content.blocks
    )


@pytest.mark.parametrize(
    "invalidity,error",
    [
        ("revision", "current adjudication"),
        ("work_version", "current adjudication"),
        ("blank_reason", "current adjudication"),
        ("hard_failure", "deterministic"),
        ("stale_digest", "stale"),
    ],
)
async def test_publish_dismissal_requires_current_versions_and_never_waives_hard_errors(
    saved_job, invalidity, error
):
    engine, state = saved_job
    finding = Finding(
        kind="unsupported",
        key="semantic-only",
        description="模型语义疑问",
        section_ids=[state.section_id],
    )
    state.local_findings = [finding]
    engine.store.save_section(engine.job_id, state)
    works = engine.store.work_states(engine.job_id)
    for work in works.values():
        work.review_key = "reviewed"
        engine.store.save_work_state(engine.job_id, work)
    engine._dismiss(finding, engine.store.states(engine.job_id), "精查已明确否定")
    if invalidity == "revision":
        state.revision += 1
        engine.store.save_section(engine.job_id, state)
        for work in works.values():
            engine.store.save_work_state(engine.job_id, work)
    elif invalidity == "work_version":
        work = works[engine.owner(state.section_id).task_id]
        work.review_key = "new-review"
        engine.store.save_work_state(engine.job_id, work)
    elif invalidity == "blank_reason":
        path = engine.store.root / "jobs" / engine.job_id / "adjudications.json"
        path.write_text(json.dumps({key: "" for key in engine._dismissed()}))
    elif invalidity == "hard_failure":
        state.content.blocks[0].evidence_ids = ["unknown"]
        engine.store.save_section(engine.job_id, state)
        for work in works.values():
            engine.store.save_work_state(engine.job_id, work)
    states = engine.store.states(engine.job_id)
    snapshot = {
        "review_passed": True,
        "blocking_issues": [],
        "state_digest": "stale"
        if invalidity == "stale_digest"
        else digest("".join(s.model_dump_json() for s in states)),
        "work_states": {
            key: value.model_dump()
            for key, value in engine.store.work_states(engine.job_id).items()
        },
    }
    engine.store.update(engine.job_id, snapshot=snapshot)
    artifact = engine.store.root / "jobs" / engine.job_id / "draft.docx"
    with pytest.raises(ValueError, match=error):
        engine.store.publish(engine.job_id, artifact)


async def test_independent_adjudications_overlap_and_failed_item_does_not_cancel_peers(saved_job):
    engine, state = saved_job
    engine.limits = engine.limits.model_copy(update={"max_parallel_reviews": 2})
    entered = asyncio.Event()
    active = peak = 0
    before = state.model_dump_json()

    async def call(task, context, model):
        nonlocal active, peak
        assert task == "drill_down"
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if context["finding"]["key"] == "failure":
                raise ValueError("unavailable page")
            return DrillDown(confirmed=False, reason="对照原文，指控不成立")
        finally:
            active -= 1

    engine.call = call
    findings = [
        Finding(kind="conflict", key=k, description=k, section_ids=[state.section_id])
        for k in ("failure", "other", "third")
    ]
    results = await engine.adjudicate_findings(findings, [state])
    assert peak == 2 and active == 0
    assert [f.key for f, _, _ in results] == ["failure", "other", "third"]
    assert results[0][2] == "ValueError"
    assert all(
        result and not result.confirmed and error is None for _, result, error in results[1:]
    )
    assert state.model_dump_json() == before


async def test_misclassified_citation_still_requires_independent_adjudication(saved_job):
    engine, state = saved_job
    block = state.content.blocks[0]
    block.origin = "model_proposal"
    block.evidence_ids = []
    block.text = "系统已于2026年10月获得监管批准"
    state.extraction.claims = []
    state.local_findings = [
        Finding(
            kind="unsupported",
            rule="citation_required",
            key="unnecessary-source",
            description="拟议业务设计缺少来源",
            section_ids=[state.section_id],
            block_refs=[{"section_id": state.section_id, "block_id": block.block_id}],
        )
    ]
    engine.store.save_section(engine.job_id, state)
    engine.store.update(engine.job_id, status="failed", stage="review")
    engine.store.resume_job(engine.job_id)
    runtime = RejectFinding()
    restored = Engine(engine.store, runtime)
    restored.business_findings = AsyncMock(return_value=[])
    restored.global_review = AsyncMock(return_value=[])
    job = await restored.run(engine.job_id)
    assert len(runtime.adjudicated) == 1
    assert len(restored._dismissed()) == 1
    assert job["snapshot"]["review_passed"]


async def test_citation_waiver_cannot_hide_a_conflict_or_other_block_with_same_key(saved_job):
    engine, state = saved_job
    block = state.content.blocks[0]
    original = Finding(
        kind="unsupported",
        key="same-subject",
        description="没有来源",
        rule="citation_required",
        section_ids=[state.section_id],
        block_refs=[{"section_id": state.section_id, "block_id": block.block_id}],
    )
    engine._dismiss(original, [state], "仅引用义务不适用")
    variants = [
        original.model_copy(update={"rule": "evidence_conflict"}),
        original.model_copy(update={"evidence_ids": ["different-source"]}),
        original.model_copy(
            update={"block_refs": [original.block_refs[0].model_copy(update={"block_id": "other"})]}
        ),
    ]
    assert all(f.fingerprint != original.fingerprint for f in variants)
    assert all(engine._finding_version(f, [state]) not in engine._dismissed() for f in variants)


async def test_saved_waiver_does_not_hide_other_same_key_issue_after_restart(saved_job):
    engine, state = saved_job
    first = Finding(
        kind="coverage",
        key="approval-rules",
        description="审批角色缺失",
        section_ids=[state.section_id],
    )
    second = first.model_copy(update={"description": "审批超时处理缺失"})
    state.local_findings = [first, second]
    engine.store.save_section(engine.job_id, state)
    engine._dismiss(first, engine.store.states(engine.job_id), "正文已经说明审批角色")
    engine.store.update(engine.job_id, status="failed", stage="review")
    engine.store.resume_job(engine.job_id)
    runtime = RejectFinding(needs_confirmation=True)
    restored = Engine(engine.store, runtime)
    restored.business_findings = AsyncMock(return_value=[])
    restored.global_review = AsyncMock(return_value=[])

    job = await restored.run(engine.job_id)

    assert runtime.adjudicated == [second.model_dump()]
    assert job["status"] == "needs_confirmation"
    assert any(
        value["description"] == second.description for value in job["snapshot"]["blocking_issues"]
    )
    assert not any(
        value["description"] == first.description for value in job["snapshot"]["blocking_issues"]
    )
    saved = next(s for s in engine.store.states(engine.job_id) if s.section_id == state.section_id)
    assert saved.local_findings == [first, second]


async def test_failed_visual_exports_explicit_pending_draft_with_original_text(
    saved_job, monkeypatch
):
    from docx import Document

    from folio.visuals.workflow import VisualBuildResult

    engine, state = saved_job
    block = state.content.blocks[0]
    figures = VisualBuildResult(
        {
            state.section_id: [
                {
                    "status": "unresolved",
                    "figure_id": "flow",
                    "kind": "flowchart",
                    "caption": "业务流程",
                    "after_block_id": block.block_id,
                    "source_block_ids": [block.block_id],
                    "origin": block.origin,
                    "evidence_ids": block.evidence_ids,
                    "reason": "禁止路径仍存在",
                }
            ]
        }
    )
    monkeypatch.setattr("folio.visuals.agent.VisualAgent.build", AsyncMock(return_value=figures))
    states = engine.store.states(engine.job_id)
    before = [s.model_dump_json() for s in states]
    await engine.finish(states, draft=False)
    result = engine.store.get(engine.job_id)
    assert result["status"] == "needs_confirmation" and result["stage"] == "complete"
    assert result["snapshot"]["review_passed"] is False
    assert any(f["key"] == "visual:flow" for f in result["snapshot"]["blocking_issues"])
    assert [s.model_dump_json() for s in engine.store.states(engine.job_id)] == before
    doc = Document(result["artifact"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "配图待确认" in text and "业务流程" in text
    assert "禁止路径仍存在" not in text
