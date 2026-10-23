from copy import deepcopy

import pytest

from folio.models import Block, RecordOnlyPending, SectionContent, VisualRequest, digest
from folio.orchestration.completed_proposals import reuse_exact_written_proposals
from folio.orchestration.work import GapProposal, WorkState

QUESTION = "业务流程涉及哪些角色和权限？"
TEXT = "交易员负责发起交易，审批员负责审批，清算员负责结算，按岗位分别授予对应操作权限。"


def fixture():
    proposal = GapProposal(gap_id=digest(QUESTION), section_ids=["s1"], content=TEXT)
    content = SectionContent(
        blocks=[Block(block_id="b1", text=TEXT, origin="model_proposal")],
        pending_items=[QUESTION, "实际参与人员名单待确认。"],
        visuals=[
            VisualRequest(
                figure_id="roles",
                kind="flowchart",
                caption="角色流程",
                description="根据正文说明各角色的操作顺序。",
                source_block_ids=["b1"],
                after_block_id="b1",
            )
        ],
    )
    return content, proposal


def test_exact_written_supplement_clears_only_duplicate_writing_gap_and_keeps_audit():
    content, proposal = fixture()
    work = WorkState(task_id="t1", missing=[QUESTION], proposals=[proposal])
    before = deepcopy((content, work))
    result, matches = reuse_exact_written_proposals(
        content, work.proposals, "s1", completion_allowed=True
    )
    assert result.pending_items == ["实际参与人员名单待确认。"]
    assert matches == [
        {"gap_id": digest(QUESTION), "section_id": "s1", "block_id": "b1", "pending_item": QUESTION}
    ]
    assert result.blocks == content.blocks
    assert result.visuals == content.visuals
    assert (content, work) == before
    assert work.missing == [QUESTION]
    # This recognition is idempotent; independent extraction/review still sees
    # the same red proposal and complete original body.
    assert reuse_exact_written_proposals(result, work.proposals, "s1", completion_allowed=True) == (
        result,
        [],
    )


@pytest.mark.parametrize(
    "change",
    [
        {"text": TEXT.replace("负责发起", "负责录入")},
        {"text": TEXT + " "},
        {"text": TEXT[:20]},
        {"origin": "evidence"},
        {"evidence_ids": ["e1"]},
        {"kind": "heading"},
    ],
)
def test_similar_partial_or_wrong_provenance_block_does_not_resolve_gap(change):
    content, proposal = fixture()
    content.blocks[0] = content.blocks[0].model_copy(update=change)
    result, matches = reuse_exact_written_proposals(
        content, [proposal], "s1", completion_allowed=True
    )
    assert result is content
    assert matches == []


def test_proposal_spread_over_multiple_paragraphs_is_not_guessed_complete():
    content, proposal = fixture()
    content.blocks = [
        Block(block_id="b1", text=TEXT[:20], origin="model_proposal"),
        Block(block_id="b2", text=TEXT[20:], origin="model_proposal"),
    ]
    assert reuse_exact_written_proposals(content, [proposal], "s1", completion_allowed=True) == (
        content,
        [],
    )


@pytest.mark.parametrize(
    "change",
    [
        {"section_ids": ["s2"]},
        {"disposition": "record_only"},
        {"gap_id": digest("相似但不是同一问题")},
    ],
)
def test_unowned_record_only_or_different_gap_proposal_stays_pending(change):
    content, proposal = fixture()
    proposal = proposal.model_copy(update=change)
    assert reuse_exact_written_proposals(content, [proposal], "s1", completion_allowed=True) == (
        content,
        [],
    )


def test_remaining_research_allowance_prevents_reuse():
    content, proposal = fixture()
    result, matches = reuse_exact_written_proposals(
        content, [proposal], "s1", completion_allowed=False
    )
    assert result is content
    assert matches == []


def test_metadata_pending_and_other_pending_strings_are_preserved():
    content, proposal = fixture()
    content.blocks.append(
        Block(block_id="people", kind="table", rows=[["编写人"], [""]], origin="model_proposal")
    )
    content.record_only_pending.append(
        RecordOnlyPending(
            kind="person_name",
            field_name="编写人",
            block_id="people",
            row_index=1,
            column_index=0,
            description="待用户填写编写人。",
        )
    )
    content.pending_items += [QUESTION + "请确认。"]
    result, matches = reuse_exact_written_proposals(
        content, [proposal], "s1", completion_allowed=True
    )
    assert len(matches) == 1
    assert result.record_only_pending == content.record_only_pending
    assert result.pending_items == ["实际参与人员名单待确认。", QUESTION + "请确认。"]
