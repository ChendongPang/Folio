from copy import deepcopy

import pytest

from folio.models import Block, SectionContent
from folio.orchestration.supplement_integration import SupplementIntegration, apply_integration


def table(**updates):
    return Block.model_validate(
        {
            "block_id": "primary_table",
            "kind": "table",
            "origin": "model_proposal",
            "text": "业务报表清单",
            "rows": [["名称", "用途", "主体"], ["余额表", "核对余额", ""]],
        }
        | updates
    )


def test_duplicate_blank_completion_requires_update_in_place_and_keeps_original():
    current = SectionContent(blocks=[table()])
    before = deepcopy(current)
    completed = table(
        block_id="si1", rows=[["名称", "用途", "主体"], ["余额表", "核对余额", "系统"]]
    )
    with pytest.raises(ValueError, match="fill the existing table block primary_table"):
        apply_integration(current, SupplementIntegration(updates=[completed]), [])
    assert current == before
    completed.block_id = "primary_table"
    result = apply_integration(current, SupplementIntegration(updates=[completed]), [])
    assert result.blocks == [completed]
    assert current == before


@pytest.mark.parametrize("change", ["caption", "nonblank_fact", "row_count", "provenance"])
def test_distinct_tables_are_not_rejected_or_silently_merged(change):
    current = SectionContent(blocks=[table()])
    added = table(block_id="si1", rows=[["名称", "用途", "主体"], ["余额表", "核对余额", "系统"]])
    if change == "caption":
        added.text = "清算报表清单"
    elif change == "nonblank_fact":
        added.rows[1][1] = "统计交易"
    elif change == "row_count":
        added.rows.append(["交易表", "核对交易", "系统"])
    else:
        added.origin = "evidence"
        added.evidence_ids = ["source"]
    result = apply_integration(
        current,
        SupplementIntegration(updates=[added]),
        ["source"],
        model_supplement=change != "provenance",
    )
    assert len(result.blocks) == 2
    assert result.blocks[0] == current.blocks[0]


def test_completion_does_not_bypass_fact_or_prior_supplement_protection():
    current = SectionContent(blocks=[table()])
    completed = table(rows=[["名称", "用途", "主体"], ["余额表", "核对余额", "系统"]])
    with pytest.raises(ValueError, match="protected block"):
        apply_integration(
            current,
            SupplementIntegration(updates=[completed]),
            [],
            protected_block_ids=["primary_table"],
        )
    current.blocks[0].origin = "evidence"
    current.blocks[0].evidence_ids = ["source"]
    completed.origin = "evidence"
    completed.evidence_ids = ["source"]
    with pytest.raises(ValueError, match="rewrite established facts"):
        apply_integration(
            current, SupplementIntegration(updates=[completed]), ["source"], model_supplement=False
        )


def test_blank_template_rows_do_not_prove_two_tables_describe_the_same_records():
    current = SectionContent(blocks=[table(rows=[["序号", "名称"], ["1", ""]])])
    added = table(block_id="si1", rows=[["序号", "名称"], ["1", "余额表"]])
    result = apply_integration(current, SupplementIntegration(updates=[added]), [])
    assert len(result.blocks) == 2
