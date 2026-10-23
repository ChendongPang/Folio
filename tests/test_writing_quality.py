from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Block, SectionContent
from folio.runtime.llm import LLMRuntime
from folio.runtime.writing_quality import (
    repeated_figure_paragraphs,
    validate_writing_repetition,
)
from folio.runtime.writing_transport import validate_writing_provenance, writing_schema


def paragraph(block_id, text, origin="model_proposal", evidence_ids=()):
    return Block(
        block_id=block_id, text=text, origin=origin, evidence_ids=list(evidence_ids)
    ).model_dump()


async def test_repair_all_proposals_cannot_lose_red_provenance_in_actual_wire(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    current = {
        "blocks": [
            paragraph("b1", "本章描述交易发起与后续处理。"),
            paragraph("b2", "所有操作均应记录审计日志。"),
            paragraph("si4", "结算方式由业务规则决定。"),
        ]
    }
    context = {
        "evidence": [],
        "current": current,
        "repair_instruction": "只修改si4的结算方式，其他内容保持原样。",
    }
    before = deepcopy(context)
    try:
        _, schema, *_ = runtime.request_parts("repair", context, SectionContent.model_json_schema())
        valid = {
            "blocks": [
                {k: v for k, v in b.items() if k in ("block_id", "origin", "text")}
                for b in current["blocks"]
            ]
        }
        Draft202012Validator(schema).validate(valid)
        invalid = deepcopy(valid)
        invalid["blocks"][0]["origin"] = "evidence"
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(invalid)
        with pytest.raises(ValueError, match="Repair block 'b1'"):
            validate_writing_provenance("repair", context, invalid)
        validate_writing_provenance("repair", context, current)
        assert context == before
        assert invalid["blocks"][0]["origin"] == "evidence"  # Rejected, never auto-recolored.
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "old,returned,available",
    [
        (paragraph("p", "拟议规则"), paragraph("p", "拟议规则", "evidence"), []),
        (paragraph("p", "拟议规则"), paragraph("p", "拟议规则", "evidence", ["unknown"]), ["e1"]),
        (paragraph("p", "已有建议"), paragraph("new", "新事实", "evidence"), ["e1"]),
        (
            paragraph("p", "已核实规则", "evidence", ["e1"]),
            paragraph("p", "已核实规则", "evidence"),
            ["e1"],
        ),
        (paragraph("p", "历史待确认", "evidence"), paragraph("p", "新增业务事实", "evidence"), []),
    ],
)
def test_repair_cannot_create_untraced_facts_or_strip_existing_citations(old, returned, available):
    context = {"current": {"blocks": [old]}, "evidence": [{"evidence_id": e} for e in available]}
    value = {"blocks": [returned]}
    original = deepcopy((context, value))
    with pytest.raises(ValueError, match="without valid evidence_ids"):
        validate_writing_provenance("repair", context, value)
    assert (context, value) == original


@pytest.mark.parametrize(
    "old,returned,available",
    [
        (
            paragraph("p", "拟议规则"),
            paragraph("p", "有原文支持的修订规则", "evidence", ["e1"]),
            ["e1"],
        ),
        (
            paragraph("p", "已核实规则", "evidence", ["e1"]),
            paragraph("p", "已核实规则", "evidence", ["e1"]),
            ["e1"],
        ),
        (paragraph("p", "历史待确认", "evidence"), paragraph("p", "历史待确认", "evidence"), []),
        (paragraph("p", "拟议规则"), paragraph("p", "修订后仍是拟议规则"), []),
    ],
)
def test_repair_preserves_existing_facts_and_allows_actual_new_source(old, returned, available):
    context = {"current": {"blocks": [old]}, "evidence": [{"evidence_id": e} for e in available]}
    value = {"blocks": [returned]}
    original = deepcopy((context, value))
    validate_writing_provenance("repair", context, value)
    assert (context, value) == original


def test_mixed_repair_does_not_force_existing_facts_to_model_proposal():
    fact = paragraph("fact", "已有事实", "evidence", ["e1"])
    proposal = paragraph("proposal", "尚未确认的建议")
    context = {"current": {"blocks": [fact, proposal]}, "evidence": [{"evidence_id": "e1"}]}
    schema = SectionContent.model_json_schema()
    assert writing_schema("repair", context, schema) == schema
    validate_writing_provenance("repair", context, context["current"])
    assert fact["origin"] == "evidence"


def figure_copy():
    source = [
        paragraph(
            f"b{i}",
            f"第{i}类业务管理规则："
            + "系统需保留每项业务操作的原始依据和处理条件，确保操作结果可追溯。",
            "evidence",
            ["e1"],
        )
        for i in range(1, 6)
    ]
    return {
        "blocks": [
            *source,
            Block(block_id="b6", kind="heading", text="业务流程图", heading_level=3).model_dump(),
            paragraph(
                "b7", "这里的图示概括上述业务；这句话与原文不同，程序必须保留。", "evidence", ["e1"]
            ),
            *(b | {"block_id": f"b{i + 6}"} for i, b in enumerate(source[1:], 2)),
        ],
        "visuals": [
            {
                "figure_id": "flow",
                "kind": "flowchart",
                "caption": "业务流程图",
                "description": "说明各业务环节的联系。",
                "source_block_ids": [f"b{i}" for i in range(1, 6)],
                "after_block_id": "b5",
            }
        ],
    }


def test_exact_figure_source_copies_get_actionable_feedback_without_deleting_content():
    value = figure_copy()
    before = deepcopy(value)
    assert [
        (x["duplicate_block_id"], x["source_block_id"]) for x in repeated_figure_paragraphs(value)
    ] == [("b8", "b2"), ("b9", "b3"), ("b10", "b4"), ("b11", "b5")]
    with pytest.raises(ValueError, match="b8 repeats b2"):
        validate_writing_repetition("generate", {}, value)
    with pytest.raises(ValueError, match="Preserve any distinct conditions"):
        validate_writing_repetition("repair", {}, value)
    assert value == before


@pytest.mark.parametrize(
    "change", ["heading", "nested", "sources", "origin", "wording", "table", "ambiguous"]
)
def test_repetition_guard_preserves_distinct_business_scope_and_provenance(change):
    value = figure_copy()
    if change == "heading":
        value["blocks"][5]["text"] = "灾备场景的特殊要求"
    elif change == "nested":
        value["blocks"].insert(
            6, Block(block_id="scope", kind="heading", text="另一个适用范围").model_dump()
        )
    elif change == "sources":
        for block in value["blocks"][7:]:
            block["evidence_ids"] = ["other-source"]
    elif change == "origin":
        for block in value["blocks"][7:]:
            block["origin"] = "model_proposal"
    elif change == "wording":
        for block in value["blocks"][7:]:
            block["text"] += "本要求只适用于境外分行。"
    elif change == "table":
        for block in value["blocks"][7:]:
            block["kind"] = "table"
            block["rows"] = [["条件", "结果"], ["条件一", block["text"]]]
    else:
        value["visuals"].append(value["visuals"][0] | {"figure_id": "other"})
    before = deepcopy(value)
    assert repeated_figure_paragraphs(value) == []
    validate_writing_repetition("generate", {}, value)
    assert value == before


def test_single_duplicate_and_short_labels_do_not_trigger_repeated_passage_guard():
    value = figure_copy()
    value["blocks"] = value["blocks"][:8]
    assert repeated_figure_paragraphs(value) == []
    value = figure_copy()
    for block in value["blocks"]:
        if block["kind"] == "paragraph":
            block["text"] = "待业务确认"
    assert repeated_figure_paragraphs(value) == []


def test_repetition_does_not_compare_ordinary_sections_or_other_tasks():
    value = figure_copy()
    value["visuals"] = []
    assert repeated_figure_paragraphs(value) == []
    validate_writing_repetition("extract", {}, figure_copy())


def test_figure_contract_guides_writer_without_changing_sources_or_block_limit():
    schema = SectionContent.model_json_schema()
    result = writing_schema("generate", {"required_visual_kinds": ["flowchart"]}, schema)
    prose = result["$defs"]["Block"]["anyOf"][0]
    assert (
        "Do not copy the already referenced preceding text wholesale under a matching figure heading"
        in prose["properties"]["text"]["description"]
    )
    assert result["properties"]["blocks"]["maxItems"] == 100
    assert (
        prose["properties"]["origin"]
        == schema["$defs"]["Block"]["anyOf"][0]["properties"]["origin"]
    )
    assert schema == SectionContent.model_json_schema()


def test_engine_result_validation_enforces_both_guards_before_acceptance():
    from folio.orchestration.engine import Engine

    engine = object.__new__(Engine)
    current = {"blocks": [paragraph("p", "现有模型建议")]}
    invalid = SectionContent.model_validate(
        {"blocks": [paragraph("p", "现有模型建议", "evidence")]}
    )
    with pytest.raises(ValueError, match="Repair block 'p'"):
        engine.check_result(invalid, {"current": current, "evidence": []}, task="repair")
    copied = SectionContent.model_validate(figure_copy())
    with pytest.raises(ValueError, match="b8 repeats b2"):
        engine.check_result(copied, {"evidence": [{"evidence_id": "e1"}]}, task="generate")
    # The whole original source passage and the unique figure explanation stay;
    # removing only the writer's duplicated copies satisfies the same validator.
    fixed = figure_copy()
    fixed["blocks"] = fixed["blocks"][:7]
    engine.check_result(
        SectionContent.model_validate(fixed),
        {"evidence": [{"evidence_id": "e1"}]},
        task="generate",
    )
