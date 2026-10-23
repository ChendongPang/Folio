"""The isolated metadata path preserves review and leaves legacy research unchanged."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from folio.models import Block, JobInput, Limits, LocalReview, SectionContent
from folio.orchestration.engine import Engine
from folio.orchestration.supplement_integration import SupplementIntegration, apply_integration
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


def metadata(**changes):
    return {
        "kind": "person_name",
        "field_name": "接口人",
        "block_id": "contacts",
        "row_index": 1,
        "column_index": 1,
        "description": "原始材料未提供业务部门接口人姓名，待项目人员填写。",
    } | changes


def content(**changes):
    return SectionContent.model_validate(
        {
            "blocks": [
                {
                    "block_id": "contacts",
                    "kind": "table",
                    "rows": [["协同对象", "接口人"], ["业务部门", ""]],
                }
            ],
            "record_only_pending": [metadata()],
        }
        | changes
    )


def test_old_content_defaults_and_typed_records_round_trip():
    old = SectionContent.model_validate({"blocks": [], "pending_items": ["缺少审批人权限规则"]})
    assert old.record_only_pending == [] and old.pending_items == ["缺少审批人权限规则"]
    value = content()
    assert SectionContent.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "business_rule"},
        {"block_id": "missing"},
        {"row_index": 0},
        {"row_index": 99},
        {"row_index": True},
        {"column_index": 0},
        {"column_index": 99},
        {"field_name": "审批权限"},
    ],
)
def test_unbound_or_business_typed_values_cannot_enter_record_only_path(change):
    with pytest.raises(ValidationError):
        content(record_only_pending=[metadata(**change)])


@pytest.mark.parametrize("description", [metadata()["description"], "重复填报不同描述"])
def test_record_only_cannot_claim_a_filled_cell_or_duplicate_kind(description):
    value = content().model_dump()
    value["blocks"][0]["rows"][1][1] = "已有姓名"
    with pytest.raises(ValidationError):
        SectionContent.model_validate(value)
    with pytest.raises(ValidationError, match="Duplicate pending metadata cell and kind"):
        content(record_only_pending=[metadata(), metadata(description=description)])


def test_unrelated_supplement_preserves_pending_metadata():
    original = content()
    patch = SupplementIntegration(
        updates=[Block(block_id="new", text="补充业务方案。", origin="model_proposal")]
    )
    result = apply_integration(original, patch, [])
    assert result.record_only_pending == original.record_only_pending
    assert result.blocks[0].rows[1][1] == ""


class MetadataRuntime(DemoRuntime):
    def __init__(self, *, legacy=False, business_gap=False, misclassified=False):
        self.calls = []
        self.legacy, self.business_gap, self.misclassified = legacy, business_gap, misclassified

    async def complete(self, task, context, schema, session):
        self.calls.append((task, context))
        result = await super().complete(task, context, schema, session)
        if task == "generate":
            value = json.loads(result.content)
            value["blocks"].append(
                {
                    "block_id": "contacts",
                    "kind": "table",
                    "text": "外汇交易项目协同。",
                    "rows": [["协同对象", "接口人"], ["业务部门", ""]],
                    "evidence_ids": [context["evidence"][0]["evidence_id"]],
                }
            )
            if self.legacy:
                value["pending_items"] = ["缺少业务部门接口人姓名"]
            else:
                value["record_only_pending"] = [metadata()]
            if self.business_gap:
                value.setdefault("pending_items", []).append("审批角色授权范围需要确认")
            result = RuntimeResult(json.dumps(value, ensure_ascii=False))
        if task == "validate" and self.misclassified:
            value = json.loads(result.content)
            value["findings"].append(
                {
                    "kind": "coverage",
                    "key": "metadata-misclassified",
                    "description": "该待填项实际包含审批职责要求，应继续核对业务原文。",
                    "section_ids": [context["section"]["section_id"]],
                    "rule": "coverage",
                }
            )
            result = RuntimeResult(json.dumps(value, ensure_ascii=False))
        return result


async def run_metadata(store, inputs, runtime):
    inputs["user_request"] = "# 外汇交易"
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=0)))
    job = await Engine(store, runtime).run(job_id)
    return job, store.states(job_id)


async def test_typed_metadata_skips_research_but_keeps_blank_review_and_delivery_blocker(
    store, inputs
):
    runtime = MetadataRuntime()
    job, states = await run_metadata(store, inputs, runtime)
    assert not any(task in {"complete_gaps", "integrate_supplement"} for task, _ in runtime.calls)
    assert not any(context.get("writing_gaps") for _, context in runtime.calls)
    validation = [ctx for task, ctx in runtime.calls if task == "validate"]
    assert validation and validation[0]["content"]["record_only_pending"] == [metadata()]
    assert any(task == "review_work" for task, _ in runtime.calls)
    assert states[0].content.record_only_pending
    assert states[0].content.blocks[-1].rows[1][1] == ""
    assert job["status"] == "needs_confirmation" and not job["snapshot"]["review_passed"]
    assert any(f["key"].startswith("record-only:") for f in job["snapshot"]["blocking_issues"])
    assert Path(job["artifact"]).is_file()


@pytest.mark.parametrize("legacy,business_gap", [(True, False), (False, True)])
async def test_legacy_and_business_pending_still_use_original_research_path(
    store, inputs, legacy, business_gap
):
    runtime = MetadataRuntime(legacy=legacy, business_gap=business_gap)
    await run_metadata(store, inputs, runtime)
    writing_gaps = [gap for _, ctx in runtime.calls for gap in ctx.get("writing_gaps", [])]
    expected = "缺少业务部门接口人姓名" if legacy else "审批角色授权范围需要确认"
    assert expected in writing_gaps
    assert metadata()["description"] not in writing_gaps
    assert any(task == "complete_gaps" for task, _ in runtime.calls)


async def test_validator_can_reject_semantic_misclassification_and_preserves_body(store, inputs):
    job, states = await run_metadata(store, inputs, MetadataRuntime(misclassified=True))
    assert any(f.key == "metadata-misclassified" for f in states[0].local_findings)
    assert any(f["key"] == "metadata-misclassified" for f in job["snapshot"]["blocking_issues"])
    assert job["status"] == "needs_confirmation"
    assert states[0].content.blocks[-1].rows[1][1] == ""


def test_primary_table_data_rows_restore_header_and_pending_alias_together():
    from folio.runtime.writing_transport import restore_primary_table

    table = {"block_id": "primary_table", "kind": "table", "rows": [["业务部门", ""]]}
    value = {
        "primary_table": table,
        "blocks": [table | {"block_id": "duplicate"}],
        "record_only_pending": [metadata(block_id="duplicate")],
    }
    restored = restore_primary_table(
        "generate", {"section": {"expected_table_headers": ["协同对象", "接口人"]}}, value
    )
    result = SectionContent.model_validate(restored)
    assert len(result.blocks) == 1
    assert result.record_only_pending[0].block_id == "primary_table"
    assert result.record_only_pending[0].row_index == 1
    assert result.blocks[0].rows[1] == ["业务部门", ""]
    assert value["record_only_pending"][0]["block_id"] == "duplicate"
    field = SectionContent.model_json_schema()["$defs"]["RecordOnlyPending"]["properties"][
        "row_index"
    ]
    assert "header=0" in field["description"]


@pytest.mark.parametrize("scope", ["claims", "body"])
async def test_recursive_validation_preserves_only_current_table_pending(store, inputs, scope):
    from test_validation_evidence_scope import BoundedValidator, engine_for, validation_context

    context = validation_context(2)
    context["validation_scope"] = scope
    context["required_project_evidence_ids"] = []
    context["content"]["visuals"] = []
    for block in context["content"]["blocks"]:
        block.update(kind="table", rows=[["协同对象", "接口人"], [block["block_id"], ""]])
    context["content"]["record_only_pending"] = [metadata(block_id=f"b{i}") for i in range(2)]
    if scope == "body":
        context["extraction"]["claims"] = []
    runtime = BoundedValidator(limit=4800)
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert len(runtime.sent) == 2
    for page in runtime.sent:
        block = page["content"]["blocks"][0]
        assert page["content"]["record_only_pending"] == [metadata(block_id=block["block_id"])]
        assert block["rows"] == [["协同对象", "接口人"], [block["block_id"], ""]]


async def test_long_table_validation_keeps_original_row_coordinates_and_header(store, inputs):
    from copy import deepcopy

    from test_validation_evidence_scope import BoundedValidator, engine_for

    rows = [["协同对象", "接口人"], *[[f"部门{i}：" + "甲" * 180, ""] for i in range(30)]]
    pending = [metadata(row_index=i) for i in (1, 8, 15, 22, 30)]
    context = {
        "section": {"section_id": "s", "title": "项目协同"},
        "content": {
            "blocks": [{"block_id": "contacts", "kind": "table", "rows": rows}],
            "record_only_pending": pending,
        },
        "extraction": {"claims": []},
        "evidence": [],
        "required_project_evidence_ids": [],
    }
    original = deepcopy(context)
    runtime = BoundedValidator(limit=2600)
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert len(runtime.sent) > 2
    received = []
    for page in runtime.sent:
        section = page["body_slice"]
        assert section["total_rows"] == len(rows)
        assert (
            page["content"]["blocks"][0]["rows"] == rows[section["row_start"] : section["row_end"]]
        )
        for item in page["content"]["record_only_pending"]:
            received.append(item)
            assert section["headers"] == rows[0]
            index = item["row_index"] - section["row_start"]
            assert page["content"]["blocks"][0]["rows"][index][item["column_index"]] == ""
    assert sorted(received, key=lambda p: p["row_index"]) == pending
    assert context == original


@pytest.mark.parametrize("row_count", [3, 4])
def test_run06_contact_cell_keeps_name_and_contact_attributes_after_table_restore(row_count):
    """Both rejected run06 attempts used two distinct kinds per blank contact cell."""
    from folio.runtime.writing_transport import restore_primary_table

    headers = ["协同对象", "接口人", "协同配合事项", "备注"]
    pending = [
        metadata(
            block_id="primary_table",
            row_index=row,
            kind=kind,
            description=f"协同系统{row}的{label}尚待人工确认。",
        )
        for row in range(1, row_count + 1)
        for kind, label in [("person_name", "接口人姓名"), ("contact_information", "联系方式")]
    ]
    wire = {
        "blocks": [],
        "primary_table": {
            "block_id": "primary_table",
            "kind": "table",
            "rows": [
                [f"协同系统{row}", "", "完成既定接口协同", ""] for row in range(1, row_count + 1)
            ],
        },
        "record_only_pending": pending,
    }
    restored = restore_primary_table(
        "generate", {"section": {"expected_table_headers": headers}}, wire
    )
    parsed = SectionContent.model_validate(restored)
    assert [item.model_dump() for item in parsed.record_only_pending] == pending
    assert len(parsed.record_only_pending) == 2 * row_count
    assert SectionContent.model_validate_json(parsed.model_dump_json()) == parsed
    assert parsed.blocks[0].rows == [headers, *wire["primary_table"]["rows"]]
    for item in parsed.record_only_pending:
        assert parsed.blocks[0].rows[item.row_index][item.column_index] == ""
    # Different kinds do not waive the existing blank-cell constraint.
    filled = parsed.model_dump()
    filled["blocks"][0]["rows"][1][1] = "已有明确填写值"
    with pytest.raises(ValidationError, match="existing empty table cell"):
        SectionContent.model_validate(filled)


@pytest.mark.parametrize("misclassified", [False, True])
async def test_multiple_attributes_same_cell_reach_validation_and_remain_in_final_pending(
    store, inputs, misclassified
):
    contact = metadata(
        kind="contact_information", description="业务部门接口人的联系方式尚待人工填写。"
    )

    class ContactRuntime(MetadataRuntime):
        async def complete(self, task, context, schema, session):
            result = await super().complete(task, context, schema, session)
            if task == "generate":
                value = json.loads(result.content)
                value["record_only_pending"].append(contact)
                return RuntimeResult(json.dumps(value, ensure_ascii=False))
            return result

    runtime = ContactRuntime(misclassified=misclassified)
    job, states = await run_metadata(store, inputs, runtime)
    expected = [metadata(), contact]
    assert [item.model_dump() for item in states[0].content.record_only_pending] == expected
    validation = [ctx for task, ctx in runtime.calls if task == "validate"]
    assert validation and validation[0]["content"]["record_only_pending"] == expected
    assert any(task == "review_work" for task, _ in runtime.calls)
    pending = [
        item
        for item in job["snapshot"]["blocking_issues"]
        if item["key"].startswith("record-only:")
    ]
    assert len(pending) == 2 and len({item["key"] for item in pending}) == 2
    assert all(
        any(record["description"] in item["description"] for item in pending) for record in expected
    )
    assert job["status"] == "needs_confirmation" and not job["snapshot"]["review_passed"]
    assert states[0].content.blocks[-1].rows[1][1] == ""
    if misclassified:
        assert any(f["key"] == "metadata-misclassified" for f in job["snapshot"]["blocking_issues"])
