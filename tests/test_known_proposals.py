import json
from copy import deepcopy
from functools import partial
from unittest.mock import AsyncMock

import pytest

from folio.models import (
    Block,
    DocumentPlan,
    DrillDown,
    Evidence,
    Extraction,
    JobInput,
    Section,
    SectionContent,
    SectionState,
)
from folio.orchestration.engine import Engine
from folio.orchestration.focused_review import (
    FocusedReviewWorkflow,
    issue_sections,
    material_parts,
)
from folio.orchestration.known_proposals import known_proposal_candidates
from folio.orchestration.local_checks import hard_findings
from folio.orchestration.work import GapProposal, WorkPlan
from folio.runtime.base import prompt_for
from folio.runtime.demo import DemoRuntime
from folio.runtime.writing_transport import validate_writing_provenance

SOURCE = "历史市场数据应按新系统需要确定迁移范围。"
DESIGN = "市场数据接入可采用接口、文件或数据库方式，分别配置主数据源与备用数据源。"
CHECK = "更新时支持范围检查、异常值检测和多源比对，保留校验记录以支持血缘追溯。"
DEADLINE = "历史数据补录或修正应在T+1日内完成。"


def source(eid="e1", text=SOURCE):
    return Evidence(evidence_id=eid, source_id=eid, source_version="1", locator="原文", text=text)


def paragraph(text=DESIGN, bid="b1", refs=None, **kwargs):
    return Block(block_id=bid, text=text, evidence_ids=["e1"] if refs is None else refs, **kwargs)


def state(blocks=None, sources=None, sid="s1"):
    return SectionState(
        section_id=sid,
        content=SectionContent(blocks=[paragraph()] if blocks is None else blocks),
        extraction=Extraction(summary="源文规则"),
        evidence=[source()] if sources is None else sources,
    )


def proposal(content=DESIGN + CHECK, **kwargs):
    return GapProposal(gap_id="gap_design", section_ids=["s1"], content=content, **kwargs)


def test_real_pattern_copied_design_with_valid_but_unrelated_citations_is_a_candidate():
    value = state([paragraph(), paragraph(CHECK, "b2")])
    before = deepcopy(value)
    findings = known_proposal_candidates([value], [proposal()])
    assert len(findings) == 1
    finding = findings[0]
    assert [r.block_id for r in finding.block_refs] == ["b1", "b2"]
    assert finding.evidence_ids == ["e1"]
    assert "gap_id=gap_design" in finding.description
    assert "not a confirmed body error" in finding.description
    assert "including semantic equivalents, dismiss the suspicion" in finding.description
    assert finding.rule == "other" and finding.kind == "unsupported"
    assert value == before  # No automatic redying, deletion, or citation replacement.


def test_real_pattern_mixed_t_plus_one_sentence_has_precise_range_without_redying_facts():
    value = state([paragraph(SOURCE + DEADLINE)])
    advice = proposal("针对数据延迟与可用性，建议设定以下指标。" + DEADLINE)
    finding = known_proposal_candidates([value], [advice])[0]
    assert f"block_span=[{len(SOURCE)}, {len(SOURCE + DEADLINE)}]" in finding.description
    assert [r.block_id for r in finding.block_refs] == ["b1"]
    assert "separate the proposed content from evidence-backed facts" in finding.description
    assert value.content.blocks[0].text == SOURCE + DEADLINE
    assert value.content.blocks[0].origin == "evidence"


@pytest.mark.parametrize(
    "change",
    [
        "other_section",
        "record_only",
        "red",
        "table",
        "heading",
        "short",
        "paraphrase",
        "unknown",
        "uncited",
    ],
)
def test_candidate_boundary_does_not_guess_unrelated_or_unmatched_content(change):
    value, advice = state(), proposal()
    if change == "other_section":
        advice.section_ids = ["s2"]
    elif change == "record_only":
        advice.disposition = "record_only"
    elif change == "red":
        value.content.blocks[0].origin = "model_proposal"
    elif change in {"table", "heading"}:
        value.content.blocks[0].kind = change
    elif change == "short":
        value.content.blocks[0].text = "市场数据接入"
    elif change == "paraphrase":
        value.content.blocks[0].text = "接入市场行情时，可以通过文件或接口获取并设置备用数据来源。"
    elif change == "unknown":
        value.content.blocks[0].evidence_ids = ["unknown"]
    else:
        value.content.blocks[0].evidence_ids = []
    assert known_proposal_candidates([value], [advice]) == []


def test_direct_new_source_support_is_preserved_and_semantic_support_is_not_hard_rejected():
    value = state(sources=[source(text=DESIGN)])
    assert known_proposal_candidates([value], [proposal()]) == []
    whole = state([paragraph(DESIGN + CHECK)], [source(text=DESIGN + CHECK)])
    assert known_proposal_candidates([whole], [proposal()]) == []
    semantic = source(text="行情接入采用文件或接口，并为每类行情指定主要来源以及备用来源。")
    value = state(sources=[semantic])
    assert known_proposal_candidates([value], [proposal()])
    context = {"evidence": [semantic.model_dump()], "model_proposals": [proposal().model_dump()]}
    validate_writing_provenance("generate", context, value.content.model_dump())
    validate_writing_provenance(
        "repair", context | {"current": value.content.model_dump()}, value.content.model_dump()
    )
    assert value.content.blocks[0].origin == "evidence"


def test_many_candidates_keep_complete_anchors_within_finding_limits():
    blocks = [paragraph(bid=f"b{i}", refs=[f"e{i}"]) for i in range(30)]
    value = state(blocks, [source(f"e{i}") for i in range(30)])
    findings = known_proposal_candidates([value], [proposal(), proposal()])
    assert len(findings) >= 3
    assert all(len(f.description) <= 3500 for f in findings)
    assert all(len(f.block_refs) <= 12 and len(f.evidence_ids) <= 12 for f in findings)
    assert {r.block_id for f in findings for r in f.block_refs} == {b.block_id for b in blocks}
    assert {eid for f in findings for eid in f.evidence_ids} == {f"e{i}" for i in range(30)}
    assert findings == known_proposal_candidates([value], [proposal()])


@pytest.mark.parametrize("page_size", [600000, 6000])
async def test_named_proposal_and_all_source_parts_survive_selector_rejection(
    monkeypatch, page_size
):
    # Both the default whole-source path and an explicitly small paged budget
    # must preserve mandatory proposals and all supporting originals.
    monkeypatch.setattr(
        "folio.orchestration.focused_review.material_parts", partial(material_parts, size=page_size)
    )

    class Reader(FocusedReviewWorkflow):
        async def select_reads(self, purpose, catalog):
            assert all(item["kind"] != "proposal" for item in catalog)
            return []

    advice = proposal(DESIGN + "\\\n" * 2200)
    value = state(sources=[source(text=SOURCE * 700)])
    finding = known_proposal_candidates([value], [advice])[0]
    context = {
        "finding": finding.model_dump(),
        "sections": [value.model_dump()],
        "business_contexts": [{"model_proposals": [advice.model_dump()]}],
    }
    before = deepcopy(context)
    result = await Reader().prepare_issue_context(context)
    for kind, original in (
        ("proposal", advice.model_dump()),
        ("evidence", value.evidence[0].model_dump()),
    ):
        parts = [m["value"] for m in result["original_materials"] if m["kind"] == kind]
        if page_size == 6000:
            assert len(parts) >= 2
            actual = json.loads(
                "".join(p["serialized_fragment"] for p in sorted(parts, key=lambda p: p["start"]))
            )
        else:
            assert len(parts) == 1
            actual = parts[0]
        assert actual == original
    assert context == before
    sections, cited, _ = issue_sections(context)
    assert sections[0]["content"]["blocks"] == value.content.model_dump()["blocks"]
    assert cited == {"e1"}


async def test_new_semantically_verified_source_can_dismiss_candidate_without_recoloring(
    store, inputs
):
    value = state(sources=[source(text="行情接入采用文件或接口，并指定主要和备用来源。")])
    finding = known_proposal_candidates([value], [proposal()])[0]
    section = Section(
        section_id="s1",
        node_id="s1",
        title="行情",
        level=1,
        display_number="1",
        instruction="描述行情接入规则。",
    )
    engine = Engine(store, DemoRuntime())
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan = DocumentPlan(sections=[section])
    engine.work_plan = WorkPlan()
    engine.call = AsyncMock(
        return_value=DrillDown(
            confirmed=False, reason="后续取得的原文语义支持此设计，不因相同方案文字而判错。"
        )
    )
    hard = hard_findings(section, value.content, value.evidence, value.extraction)
    assert finding.fingerprint not in {f.fingerprint for f in hard}
    results = await engine.adjudicate_findings(
        [finding], [value], hard_keys={f.fingerprint for f in hard}
    )
    assert not results[0][1].confirmed and results[0][2] is None
    assert value.content.blocks[0].origin == "evidence"


def test_prompt_contracts_limit_repairs_to_real_obligations_and_planned_figures():
    prompt = prompt_for("drill_down", {}, DrillDown.model_json_schema())
    assert "a finding's description is not a confirmed requirement" in prompt
    assert "rather than demanding that the parent repeat every child detail" in prompt
    assert "does not need renewed confirmation or sources" in prompt
    for task in ("generate", "repair", "integrate_supplement"):
        prompt = prompt_for(task, {}, SectionContent.model_json_schema())
        assert "allowed_visual_kinds" in prompt and "delegated_to_children" in prompt
        assert "do not turn optional figures into mandatory ones" in prompt
    prompt = prompt_for("repair", {}, SectionContent.model_json_schema())
    assert "do not clear old citations merely because this call adds no new materials" in prompt
