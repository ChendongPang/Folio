import asyncio
from types import SimpleNamespace

import pytest

from folio.models import (
    Block,
    DocumentPlan,
    Evidence,
    Extraction,
    Finding,
    JobInput,
    Section,
    SectionContent,
    SectionState,
)
from folio.orchestration.engine import Engine
from folio.orchestration.provenance import (
    normalize_fact_references,
    revalidate_saved_references,
)
from folio.orchestration.work import BusinessFact, BusinessRecord, WorkPlan, WorkState, WorkTask
from folio.runtime.demo import DemoRuntime


def source(eid):
    return Evidence(
        evidence_id=eid, source_id="doc", source_version="1", locator=eid, text="必须审批"
    )


def content(refs, origin="evidence"):
    return SectionContent(
        blocks=[Block(block_id="b", text="必须审批", evidence_ids=refs, origin=origin)]
    )


def fact(fid, refs):
    return {"fact_id": fid, "statement": "必须审批", "evidence_ids": refs}


def test_unique_alias_expands_all_sources_with_stable_dedup_and_preserves_body():
    original = content(["e2", "f", "e1", "unknown"])
    original.pending_items = ["保留待确认记录"]
    before = original.model_dump()
    result = normalize_fact_references(
        original, [source("e1"), source("e2")], {"facts": [fact("f", ["e1", "e2", "e1"])]}
    )
    assert result.blocks[0].evidence_ids == ["e2", "e1", "unknown"]
    expected = original.model_dump()
    expected["blocks"][0]["evidence_ids"] = ["e2", "e1", "unknown"]
    assert result.model_dump() == expected
    assert original.model_dump() == before
    assert (
        normalize_fact_references(
            result, [source("e1"), source("e2")], {"facts": [fact("f", ["e1", "e2"])]}
        )
        is result
    )


@pytest.mark.parametrize(
    "facts,evidence",
    [
        ([fact("f", ["e1"])], ["f", "e1"]),  # Real source IDs win over aliases.
        ([fact("f", ["e1"]), fact("f", ["e1"])], ["e1"]),
        ([fact("f", [])], ["e1"]),
        ([fact("f", ["e1", "missing"])], ["e1"]),
        ([fact("other", ["e1"])], ["e1"]),
    ],
)
def test_ambiguous_unknown_missing_or_source_collisions_are_not_rewritten(facts, evidence):
    original = content(["f"])
    assert (
        normalize_fact_references(original, [source(e) for e in evidence], {"facts": facts})
        is original
    )


def test_model_proposals_remain_unchanged():
    original = content(["f"], origin="model_proposal")
    assert (
        normalize_fact_references(original, [source("e1")], {"facts": [fact("f", ["e1"])]})
        is original
    )


@pytest.fixture
def engine(store, inputs):
    engine = Engine(store, DemoRuntime())
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    section = Section(
        section_id="s",
        node_id="s",
        title="审批",
        level=1,
        display_number="1",
        instruction="规则",
    )
    engine.plan = DocumentPlan(sections=[section])
    task = WorkTask(
        task_id="owner",
        title="审批",
        objective="说明规则",
        section_ids=["s"],
        questions=["如何审批"],
        completion_criteria=["说明规则"],
        rationale="同一业务",
    )
    engine.work_plan = WorkPlan(tasks=[task])
    store.save_work_state(
        engine.job_id,
        WorkState(
            task_id="owner",
            record=BusinessRecord(facts=[BusinessFact(**fact("f", ["e1", "e2"]))]),
            evidence=[source("e1"), source("e2")],
        ),
    )
    engine.references = SimpleNamespace(evidence=lambda *_: [])
    return engine


async def test_validate_section_normalizes_before_extraction_and_checks_original_sources(engine):
    original = content(["f"])
    state = await engine.validate_section(
        engine.plan.sections[0], original, [source("e1"), source("e2")], 1
    )
    assert state.content.blocks[0].evidence_ids == ["e1", "e2"]
    assert state.extraction.claims[0].evidence_ids == ["e1", "e2"]
    assert state.extraction.claims[0].status == "supported"
    assert not state.local_findings
    assert original.blocks[0].evidence_ids == ["f"]


def save_legacy(engine):
    state = SectionState(
        section_id="s",
        revision=3,
        content=content(["f"]),
        extraction=Extraction(summary="旧摘要"),
        evidence=[source("e1"), source("e2")],
        local_findings=[
            Finding(kind="unsupported", key="b", description="旧引用无效", section_ids=["s"])
        ],
    )
    engine.store.save_section(engine.job_id, state)
    return state


async def test_resume_revalidates_then_saves_one_revision_and_is_idempotent(engine):
    legacy = save_legacy(engine)
    await revalidate_saved_references(engine)
    revised = engine.store.states(engine.job_id)[0]
    assert revised.revision == legacy.revision + 1
    assert revised.content.blocks[0].evidence_ids == ["e1", "e2"]
    assert revised.extraction.claims[0].status == "supported"
    assert revised.local_findings == []
    calls = engine.store.get(engine.job_id)["calls"]
    await revalidate_saved_references(engine)
    assert engine.store.states(engine.job_id)[0] == revised
    assert engine.store.get(engine.job_id)["calls"] == calls == 2


@pytest.mark.parametrize("failure", [ValueError("invalid validation"), asyncio.CancelledError()])
async def test_failed_or_cancelled_revalidation_does_not_replace_saved_state(engine, failure):
    legacy = save_legacy(engine)

    async def fail(*args, **kwargs):
        raise failure

    engine.validate_section = fail
    with pytest.raises(type(failure)):
        await revalidate_saved_references(engine)
    assert engine.store.states(engine.job_id)[0] == legacy


async def test_expanded_source_ids_do_not_waive_semantic_support_checks(engine):
    legacy = save_legacy(engine)
    for evidence in legacy.evidence:
        evidence.text = "这份材料只描述交易录入，不提供审批规则。"
    engine.store.save_section(engine.job_id, legacy)

    await revalidate_saved_references(engine)

    revised = engine.store.states(engine.job_id)[0]
    assert revised.content.blocks[0].text == legacy.content.blocks[0].text
    assert revised.content.blocks[0].evidence_ids == ["e1", "e2"]
    assert revised.extraction.claims[0].status == "candidate"
    assert any(finding.kind == "unsupported" for finding in revised.local_findings)
