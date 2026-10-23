import json

import pytest
from jsonschema import Draft202012Validator

from folio.document.presentation import normalize_content
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Block, Section, SectionContent, VisualRequest
from folio.runtime.business_transport import translate_ids, unpack_rows
from folio.runtime.llm import LLMRuntime
from folio.runtime.writing_transport import restore_primary_table


@pytest.mark.parametrize("task", ["generate", "repair"])
@pytest.mark.parametrize("primary_table", [False, True])
async def test_visuals_survive_real_writer_transport_and_reference_aliases(
    tmp_path, task, primary_table
):
    evidence_id = "ev_0123456789abcdef0123456789abcdef"
    block_id = "block_0123456789abcdef0123456789abcdef"
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "evidence",
        "evidence_ids": [evidence_id],
        "rows": [["金额", "交易金额"]],
    }
    blocks = [
        {
            "block_id": block_id,
            "text": "输入交易金额后提交审批。",
            "origin": "evidence",
            "evidence_ids": [evidence_id],
        }
    ]
    sources = [block_id] + (["primary_table"] if primary_table else [])
    visual = {
        "figure_id": "approval_figure",
        "kind": "ui",
        "caption": "交易录入",
        "description": "展示交易金额和提交按钮。",
        "source_block_ids": sources,
        "after_block_id": block_id,
    }
    context = {
        "section": {
            "section_id": "sec_0123456789abcdef0123456789abcdef",
            "expected_table_headers": ["字段", "说明"] if primary_table else [],
        },
        "evidence": [{"evidence_id": evidence_id, "text": "输入金额后提交审批。"}],
    }
    if task == "repair":
        context["current"] = {"blocks": blocks, "visuals": [visual]}
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # This test replaces generation; keep capacity measurement local.
    messages, wire_schema, _, _, refs = runtime.request_parts(
        task, context, SectionContent.model_json_schema()
    )
    aliases = {canonical: alias for alias, canonical in refs.items()}
    assert "visuals" in wire_schema["properties"]
    assert {"source_block_ids", "after_block_id"} <= set(
        wire_schema["$defs"]["VisualRequest"]["properties"]
    )
    sent = unpack_rows(json.loads(messages[1]["content"]))
    if task == "repair":
        sent_visual = sent["current"]["visuals"][0]
        assert sent_visual["source_block_ids"][0].startswith("sfref")
        assert sent_visual["after_block_id"].startswith("sfref")
        assert translate_ids(sent["current"], refs) == context["current"]
    response = {"blocks": blocks, "visuals": [visual]}
    if primary_table:
        response["primary_table"] = table
    response = translate_ids(response, aliases)
    Draft202012Validator(wire_schema).validate(response)

    async def stream(*args, **kwargs):
        yield "completed", {"text": json.dumps(response), "usage": {}}

    runtime.stream = stream
    try:
        result = await runtime.complete(task, context, SectionContent.model_json_schema(), "test")
    finally:
        await runtime.aclose()
    content = SectionContent.model_validate_json(result.content)
    assert content.visuals[0].model_dump() == visual
    assert all(b.evidence_ids == [evidence_id] for b in content.blocks)
    if primary_table:
        assert content.blocks[0].block_id == "primary_table"
        assert content.blocks[0].rows == [["字段", "说明"], ["金额", "交易金额"]]


def test_deduplicating_primary_table_keeps_visual_source_and_insertion_references():
    context = {"section": {"expected_table_headers": ["字段", "说明"]}}
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "evidence",
        "evidence_ids": ["ev1"],
        "rows": [["金额", "交易金额"]],
    }
    value = {
        "primary_table": table,
        "blocks": [table | {"block_id": "duplicate_table"}],
        "visuals": [
            {
                "figure_id": "ui1",
                "kind": "ui",
                "caption": "金额录入",
                "description": "展示金额字段",
                "source_block_ids": ["duplicate_table", "primary_table"],
                "after_block_id": "duplicate_table",
            }
        ],
    }
    restored = SectionContent.model_validate(restore_primary_table("generate", context, value))
    assert restored.visuals[0].source_block_ids == ["primary_table"]
    assert restored.visuals[0].after_block_id == "primary_table"
    assert len(restored.blocks) == 1
    assert value["visuals"][0]["after_block_id"] == "duplicate_table"


def test_normalization_keeps_figure_when_redundant_heading_is_removed():
    section = Section(
        section_id="s1",
        node_id="o1",
        title="审批",
        display_number="1",
        level=1,
        instruction="说明审批步骤",
    )
    content = SectionContent(
        blocks=[
            Block(block_id="h1", kind="heading", text="审批"),
            Block(block_id="b1", text="1. 录入\n2. 审批"),
            Block(block_id="b1_p1", text="拒绝后允许修改"),
        ],
        visuals=[
            VisualRequest(
                figure_id="flow1",
                kind="flowchart",
                caption="审批流程",
                description="表示审批步骤",
                source_block_ids=["h1", "b1", "b1_p1"],
                after_block_id="h1",
            )
        ],
    )
    normalized = normalize_content(content, section)
    SectionContent.model_validate(normalized.model_dump())
    assert [b.block_id for b in normalized.blocks] == ["b1", "b1_p2", "b1_p1"]
    assert normalized.visuals[0].source_block_ids == ["b1", "b1_p2", "b1_p1"]
    assert normalized.visuals[0].after_block_id == "b1_p1"
    assert content.visuals[0].after_block_id == "h1"
    assert content.visuals[0].source_block_ids == ["h1", "b1", "b1_p1"]
