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
from folio.orchestration.local_checks import hard_findings
from folio.orchestration.public_evidence_review import public_evidence_candidates
from folio.orchestration.work import WorkPlan
from folio.runtime.demo import DemoRuntime


def evidence(eid="web", *, metadata=None, text="标准原文：仅定义标准；未说明本项目采用。"):
    return Evidence(
        evidence_id=eid,
        source_id=eid,
        source_version="1",
        locator="完整原始资料",
        text=text,
        metadata=({"role": "public_web", "project_fact": False} if metadata is None else metadata),
    )


def state(blocks, sources=None, sid="s1"):
    return SectionState(
        section_id=sid,
        content=SectionContent(blocks=blocks),
        extraction=Extraction(summary="独立提取结果"),
        evidence=sources if sources is not None else [evidence()],
    )


def block(bid="b1", *, refs=None, **kwargs):
    return Block(
        block_id=bid,
        text="该标准定义业务流程记号；是否在本项目采用仍需单独核对。",
        evidence_ids=["web"] if refs is None else refs,
        **kwargs,
    )


def test_candidates_merge_per_section_and_do_not_decide_or_mutate_content():
    first = state(
        [
            block(),
            Block(block_id="table", kind="table", rows=[["标准", "含义"]], evidence_ids=["web"]),
            block("heading", kind="heading"),
            block("proposal", origin="model_proposal"),
        ]
    )
    second = state([block()], sid="s2")
    before = deepcopy([first, second])
    findings = public_evidence_candidates([first, second])
    assert len(findings) == 2
    assert [ref.block_id for ref in findings[0].block_refs] == ["b1", "table"]
    assert findings[0].evidence_ids == ["web"]
    assert all(f.kind == "unsupported" and f.rule == "other" for f in findings)
    assert all("a suspicion, not a confirmed body error" in f.description for f in findings)
    assert all("dismiss the suspicion" in f.description for f in findings)
    assert [first, second] == before
    assert findings == public_evidence_candidates([second, first])


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"role": "public_web"},
        {"project_fact": False},
        {"role": "public_web", "project_fact": True},
        {"role": "public_web", "project_fact": "false"},
        {"role": "project", "project_fact": False},
    ],
)
def test_unknown_or_inconsistent_metadata_is_not_inferred_to_be_public(metadata):
    assert public_evidence_candidates([state([block()], [evidence(metadata=metadata)])]) == []


def test_mixed_project_citations_and_unknown_refs_do_not_become_public_only():
    value = state(
        [
            block("mixed", refs=["web", "project"]),
            block("missing", refs=["web", "missing"]),
            block("uncited", refs=[]),
        ],
        [evidence(), evidence("project", metadata={"project_fact": True})],
    )
    assert public_evidence_candidates([value]) == []
    section = Section(
        section_id="s1",
        node_id="s1",
        title="内容",
        level=1,
        display_number="1",
        instruction="原有来源校验仍须运行",
    )
    hard = hard_findings(section, value.content, value.evidence, value.extraction)
    assert {f.key for f in hard} >= {"missing", "uncited"}


def test_duplicate_source_id_cannot_hide_a_project_source_version():
    value = state([block()], [evidence(), evidence(metadata={"project_fact": True})])
    assert public_evidence_candidates([value]) == []


@pytest.mark.parametrize("shared_source", [False, True])
def test_batches_bound_both_anchor_lists_without_losing_blocks_or_source_ids(shared_source):
    sources = [evidence(f"e{i}") for i in range(25)]
    blocks = [block(f"b{i}", refs=["e0" if shared_source else f"e{i}"]) for i in range(25)]
    value = state(blocks, sources)
    findings = public_evidence_candidates([value])
    assert len(findings) == 3
    assert all(len(f.block_refs) <= 12 and len(f.evidence_ids) <= 12 for f in findings)
    assert {ref.block_id for f in findings for ref in f.block_refs} == {b.block_id for b in blocks}
    assert {eid for f in findings for eid in f.evidence_ids} == {
        eid for b in blocks for eid in b.evidence_ids
    }


def test_single_block_with_many_sources_keeps_complete_body_and_citations_for_drill_down():
    sources = [evidence(f"e{i}") for i in range(25)]
    value = state([block(refs=[e.evidence_id for e in sources])], sources)
    before = value.model_dump()
    findings = public_evidence_candidates([value])
    assert len(findings) == 3
    assert {eid for f in findings for eid in f.evidence_ids} == {e.evidence_id for e in sources}
    for finding in findings:
        sections, cited, targets = issue_sections(
            {"finding": finding.model_dump(), "sections": [value.model_dump()]}
        )
        assert sections[0]["content"]["blocks"] == before["content"]["blocks"]
        assert cited == {e.evidence_id for e in sources}
        assert targets == [{"section_id": "s1", "block_id": "b1"}]
    assert value.model_dump() == before


@pytest.mark.parametrize("page_size", [600000, 6000])
async def test_original_public_sources_are_mandatory_even_if_selector_rejects_everything(monkeypatch, page_size):
    monkeypatch.setattr("folio.orchestration.focused_review.material_parts",
                        partial(material_parts, size=page_size))

    class Reader(FocusedReviewWorkflow):
        async def select_reads(self, purpose, catalog):
            assert all(item["value"].get("evidence_id") != "web" for item in catalog)
            return []

    # Whole-source and explicitly small pages both retain the negative clause.
    full_text = "完整标准原文。" * 1200 + "该公开标准不证明本项目已经采用。"
    value = state([block()], [evidence(text=full_text), evidence("unrelated")])
    finding = public_evidence_candidates([value])[0]
    context = {"finding": finding.model_dump(), "sections": [value.model_dump()]}
    before = deepcopy(context)
    prepared = await Reader().prepare_issue_context(context)
    assert (
        prepared["sections"][0]["content"]["blocks"] == before["sections"][0]["content"]["blocks"]
    )
    fragments = [item["value"] for item in prepared["original_materials"]]
    if page_size == 6000:
        assert len(fragments) >= 2
        restored = json.loads(
            "".join(v["serialized_fragment"] for v in sorted(fragments, key=lambda v: v["start"]))
        )
    else:
        assert len(fragments) == 1
        restored = fragments[0]
    assert restored == value.evidence[0].model_dump()
    assert context == before


async def test_public_definition_can_be_adjudicated_and_dismissed_without_turning_red(
    store, inputs
):
    value = state([block()])
    original = value.model_dump()
    finding = public_evidence_candidates([value])[0]
    section = Section(
        section_id="s1",
        node_id="s1",
        title="术语",
        level=1,
        display_number="1",
        instruction="解释公开标准术语",
    )
    engine = Engine(store, DemoRuntime())
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan = DocumentPlan(sections=[section])
    engine.work_plan = WorkPlan()
    engine.call = AsyncMock(
        return_value=DrillDown(
            confirmed=False,
            reason="公开来源支持此标准定义，正文未声称本项目已经采用。",
        )
    )
    hard = hard_findings(section, value.content, value.evidence, value.extraction)
    assert finding.fingerprint not in {f.fingerprint for f in hard}
    results = await engine.adjudicate_findings(
        [finding],
        [value],
        hard_keys={f.fingerprint for f in hard},
    )
    engine.call.assert_awaited_once()
    request = engine.call.call_args.args[1]
    assert request["sections"] == [original]
    assert results[0][2] is None and not results[0][1].confirmed
    assert not results[0][1].needs_confirmation
    engine._dismiss(finding, [value], results[0][1].reason)
    assert engine._finding_version(finding, [value]) in engine._dismissed()
    assert value.model_dump() == original
    assert value.content.blocks[0].origin == "evidence"
