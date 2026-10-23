import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import SectionContent
from folio.runtime.llm import LLMRuntime
from folio.runtime.writing_transport import (
    restore_primary_table,
    validate_writing_provenance,
    writing_schema,
)

CONTEXT = {"section": {"expected_table_headers": ["名称", "说明"]}}


def test_table_contract_and_restoration_preserve_values_and_provenance():
    schema = writing_schema("generate", CONTEXT, SectionContent.model_json_schema())
    assert "primary_table" in schema["required"]
    assert schema["properties"]["primary_table"]["properties"]["rows"]["items"]["minItems"] == 2
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "model_proposal",
        "evidence_ids": [],
        "rows": [["交易", "建议规则"]],
    }
    value = {
        "primary_table": table,
        "blocks": [
            table | {"block_id": "duplicate"},
            table | {"block_id": "different", "rows": [["结算", "另一规则"]]},
        ],
    }
    restored = SectionContent.model_validate(restore_primary_table("generate", CONTEXT, value))
    assert len(restored.blocks) == 2
    assert restored.blocks[0].rows == [["名称", "说明"], ["交易", "建议规则"]]
    assert restored.blocks[0].origin == "model_proposal"
    assert table["rows"] == [["交易", "建议规则"]]


def test_invalid_missing_or_wrong_width_table_is_not_silently_fixed():
    for value in ({}, {"primary_table": {"kind": "table", "rows": [["单列"]]}}):
        with pytest.raises(ValueError):
            restore_primary_table("repair", CONTEXT, value)


def test_plain_sections_are_unaffected():
    schema = SectionContent.model_json_schema()
    assert writing_schema("generate", {"section": {}}, schema) is schema
    value = {"blocks": []}
    assert restore_primary_table("generate", {"section": {}}, value) is value


@pytest.mark.parametrize("task", ["generate", "repair"])
async def test_main_table_reserves_one_block_in_real_wire_and_restores_total_limit(tmp_path, task):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Synthetic writing responses use local capacity measurement.
    context = CONTEXT | {"evidence": [], "model_completion_allowed": True}
    original = SectionContent.model_json_schema()
    try:
        _, schema, *_ = runtime.request_parts(task, context, original)
        assert schema["properties"]["blocks"]["maxItems"] == 99
        assert original["properties"]["blocks"]["maxItems"] == 100
        table = {
            "block_id": "primary_table",
            "kind": "table",
            "origin": "model_proposal",
            "evidence_ids": [],
            "rows": [["对象", "建议规则"]],
        }
        if "evidence_ids" not in schema["properties"]["primary_table"]["properties"]:
            table.pop("evidence_ids")
        blocks = [
            {"block_id": f"b{i}", "origin": "model_proposal", "text": f"第{i}项独立业务要求"}
            for i in range(100)
        ]
        valid = {"primary_table": table, "blocks": blocks[:99]}
        Draft202012Validator(schema).validate(valid)
        with pytest.raises(ValidationError, match="too long"):
            Draft202012Validator(schema).validate({"primary_table": table, "blocks": blocks})

        async def stream(*args, **kwargs):
            yield "completed", {"text": json.dumps(valid), "usage": {}}

        runtime.stream = stream
        result = await runtime.complete(task, context, original, "test")
        restored = SectionContent.model_validate_json(result.content)
        assert len(restored.blocks) == 100
        assert restored.blocks[0].block_id == "primary_table"
        assert [b.block_id for b in restored.blocks[1:]] == [f"b{i}" for i in range(99)]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("with_table", [False, True])
def test_parent_scope_preserves_child_obligations_visuals_and_existing_capacity(with_table):
    from folio.runtime.base import INSTRUCTIONS

    schema = SectionContent.model_json_schema()
    context = {
        "section": {"expected_table_headers": ["类别", "规则"] if with_table else []},
        "child_sections": [
            {
                "section_id": "child",
                "title": "产品办理",
                "instruction": "逐项描述操作步骤，绘制流程图并保留例外条件。",
            }
        ],
    }
    original_context = deepcopy(context)
    result = writing_schema("generate", context, schema)
    # The scope contract guides ownership, without turning parents into empty
    # containers, imposing an arbitrary short limit, or removing required figures.
    assert result["properties"]["blocks"]["maxItems"] == (99 if with_table else 100)
    assert "Parent-section body" in result["properties"]["blocks"]["description"]
    assert "explicitly required figures/tables" in result["properties"]["blocks"]["description"]
    assert result["properties"]["visuals"] == schema["properties"]["visuals"]
    assert result["$defs"]["Block"] == schema["$defs"]["Block"] or with_table
    assert context == original_context
    assert schema == SectionContent.model_json_schema()
    for task in ("generate", "repair"):
        assert "parent-specific applicability conditions" in INSTRUCTIONS[task]
        assert "Selecting an evidence item means some passage is relevant" in INSTRUCTIONS[task]
    assert (
        "Source support and section applicability are independent checks"
        in INSTRUCTIONS["validate"]
    )


def test_table_fill_instructions_do_not_allow_source_reclassification_or_fuzzy_deletion():
    from folio.runtime.base import INSTRUCTIONS

    prompt = INSTRUCTIONS["integrate_supplement"]
    assert "update the same block_id" in prompt
    assert "supplementation must not rewrite old facts" in prompt
    assert "Do not duplicate the full table or relabel old facts as proposals" in prompt
    for task in ("validate", "review_work", "drill_down"):
        assert "New values versus blanks are not a conflict" in INSTRUCTIONS[task]
        assert (
            "independent tables with different objects, scopes, values, or sources"
            in INSTRUCTIONS[task]
        )


@pytest.mark.parametrize("header_in_duplicate", [False, True])
@pytest.mark.parametrize("main_caption", ["", "业务量估算指标表"])
def test_identical_primary_table_rows_merge_single_caption_and_retarget_visuals(
    header_in_duplicate, main_caption
):
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "model_proposal",
        "evidence_ids": [],
        "text": main_caption,
        "rows": [["用户量", "待确认"], ["交易量", "待确认"]],
    }
    duplicate = table | {
        "block_id": "b2",
        "text": "" if main_caption else "业务量估算指标表",
        "rows": ([CONTEXT["section"]["expected_table_headers"]] if header_in_duplicate else [])
        + table["rows"],
    }
    value = {
        "primary_table": table,
        "blocks": [duplicate],
        "visuals": [{"source_block_ids": ["b2"], "after_block_id": "b2"}],
    }
    original = deepcopy(value)
    restored = restore_primary_table("generate", CONTEXT, value)
    assert value == original
    assert len(restored["blocks"]) == 1
    assert restored["blocks"][0]["text"] == "业务量估算指标表"
    assert restored["blocks"][0]["rows"] == [
        CONTEXT["section"]["expected_table_headers"],
        *table["rows"],
    ]
    assert restored["visuals"] == [
        {"source_block_ids": ["primary_table"], "after_block_id": "primary_table"}
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "model_proposal"},
        {"evidence_ids": ["e2"]},
        {"rows": [["另一指标", "另一规则"]]},
        {"text": "另一适用范围的估算"},
    ],
)
def test_table_deduplication_preserves_distinct_scope_sources_and_origin(change):
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "evidence",
        "evidence_ids": ["e1"],
        "text": "本期估算",
        "rows": [["用户量", "规则"]],
    }
    duplicate = table | {"block_id": "b2", "text": ""} | change
    value = {"primary_table": table, "blocks": [duplicate]}
    restored = restore_primary_table("repair", CONTEXT, value)
    assert len(restored["blocks"]) == 2
    assert restored["blocks"][1] == duplicate


def test_reworded_estimate_with_additional_business_detail_is_not_silently_deleted():
    table = {
        "block_id": "primary_table",
        "kind": "table",
        "origin": "model_proposal",
        "evidence_ids": [],
        "rows": [
            ["日均交易笔数", "根据预计接入的机构数量测算"],
            ["峰值并发", "按集中交易时段估算"],
        ],
    }
    other = table | {
        "block_id": "b2",
        "text": "业务量估算指标建议表",
        "rows": [
            ["日均交易笔数", "根据预计开户的机构数量测算"],
            ["峰值并发", "按集中交易时段（如开盘、收盘）估算"],
        ],
    }
    restored = restore_primary_table(
        "generate", CONTEXT, {"primary_table": table, "blocks": [other]}
    )
    # The reviewer can propose consolidation, but transport cannot infer that
    # onboarding and account opening mean the same business scope.
    assert len(restored["blocks"]) == 2
    assert restored["blocks"][1] == other


@pytest.mark.parametrize("task", ["generate", "repair"])
async def test_main_and_independent_tables_keep_separate_contracts_in_real_wire(tmp_path, task):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Preserve token checks without external tokenizer probes.
    context = CONTEXT | {"evidence": [], "model_completion_allowed": True}
    original_schema = SectionContent.model_json_schema()
    try:
        messages, schema, *_ = runtime.request_parts(task, context, original_schema)
        main = schema["properties"]["primary_table"]
        extra = next(
            branch
            for branch in schema["$defs"]["Block"]["anyOf"]
            if branch["properties"]["kind"].get("const") == "table"
        )
        assert "without headers" in main["properties"]["rows"]["description"]
        assert "its first row must be its own header" in extra["properties"]["rows"]["description"]
        assert "text" not in extra["required"]
        assert "paraphrased version of the same metric table" in messages[0]["content"]
        assert original_schema == SectionContent.model_json_schema()
        table = {
            "block_id": "primary_table",
            "kind": "table",
            "origin": "model_proposal",
            "evidence_ids": [],
            "rows": [["用户量", "总行日常并发量估算"]],
        }
        if "evidence_ids" not in main["properties"]:
            table.pop("evidence_ids")
        independent = {
            "block_id": "branch_table",
            "kind": "table",
            "origin": "model_proposal",
            "rows": [["名称", "说明"], ["用户量", "分行应急窗口并发量估算"]],
        }
        # Same column labels and no caption are valid for a genuinely different
        # business scope. The dedicated main table may be the sole body content.
        Draft202012Validator(schema).validate({"primary_table": table, "blocks": []})
        response = {"primary_table": table, "blocks": [independent]}
        Draft202012Validator(schema).validate(response)

        async def stream(*args, **kwargs):
            yield "completed", {"text": json.dumps(response), "usage": {}}

        runtime.stream = stream
        result = await runtime.complete(task, context, original_schema, "test")
        content = SectionContent.model_validate_json(result.content)
        assert len(content.blocks) == 2
        assert content.blocks[0].rows == [["名称", "说明"], *table["rows"]]
        assert content.blocks[1].rows == independent["rows"]
        assert content.blocks[1].text == ""
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("with_primary_table", [False, True])
async def test_fresh_writer_without_sources_rejects_unbacked_facts_in_actual_wire(
    tmp_path, with_primary_table
):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Only generation is replaced by the fixture stream below.
    context = {
        "section": {"expected_table_headers": ["姓名", "日期"] if with_primary_table else []},
        "evidence": [],
        "model_completion_allowed": True,
        "model_proposals": [{"content": "系统支持按用户角色配置操作权限。"}],
    }
    original = SectionContent.model_json_schema()
    try:
        _, schema, *_ = runtime.request_parts("generate", context, original)
        proposal = {
            "block_id": "b1",
            "origin": "model_proposal",
            "text": "系统支持按用户角色配置操作权限。",
        }
        response = {"blocks": [proposal]}
        if with_primary_table:
            response["primary_table"] = {
                "block_id": "primary_table",
                "origin": "model_proposal",
                "kind": "table",
                "rows": [["", ""]],
            }
        validator = Draft202012Validator(schema)
        validator.validate(response)
        invalid = deepcopy(response)
        invalid["blocks"][0]["origin"] = "evidence"
        with pytest.raises(ValidationError):
            validator.validate(invalid)
        with pytest.raises(ValueError, match="must use origin=model_proposal"):
            validate_writing_provenance("generate", context, invalid)
        assert invalid["blocks"][0]["origin"] == "evidence"  # No automatic red reclassification.
        if with_primary_table:
            invalid = deepcopy(response)
            invalid["primary_table"]["origin"] = "evidence"
            with pytest.raises(ValidationError):
                validator.validate(invalid)
            with pytest.raises(ValueError, match="must use origin=model_proposal"):
                validate_writing_provenance("generate", context, invalid)
        invalid = deepcopy(response)
        invalid["blocks"][0]["evidence_ids"] = ["unavailable-source"]
        with pytest.raises(ValidationError):
            validator.validate(invalid)
        with pytest.raises(ValueError, match="cannot cite unavailable"):
            validate_writing_provenance("generate", context, invalid)

        async def stream(*args, **kwargs):
            yield "completed", {"text": json.dumps(response), "usage": {}}

        runtime.stream = stream
        result = await runtime.complete("generate", context, original, "test")
        content = SectionContent.model_validate_json(result.content)
        validate_writing_provenance("generate", context, content.model_dump())
        assert all(b.origin == "model_proposal" and not b.evidence_ids for b in content.blocks)
        assert original == SectionContent.model_json_schema()
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "task,override",
    [
        ("repair", {}),
        (None, {}),
        ("generate", {"current": {"blocks": [{"block_id": "old", "text": "已确认规则"}]}}),
        ("generate", {"evidence": [{"evidence_id": "e1", "text": "已有项目规则"}]}),
        ("generate", {"model_completion_allowed": False}),
    ],
)
def test_new_draft_provenance_does_not_rewrite_existing_or_unrelated_context(task, override):
    context = {"evidence": [], "model_completion_allowed": True} | override
    value = {
        "blocks": [
            {"block_id": "old", "origin": "evidence", "evidence_ids": ["e1"], "text": "原文"}
        ]
    }
    before = deepcopy(value)
    validate_writing_provenance(task, context, value)
    assert value == before
    schema = SectionContent.model_json_schema()
    assert writing_schema(task, context, schema) is schema


def test_new_draft_provenance_does_not_allow_missing_origin_to_use_fact_default():
    context = {"evidence": [], "model_completion_allowed": True}
    value = {"blocks": [{"block_id": "b1", "text": "未经核实的业务方案"}]}
    with pytest.raises(ValueError, match="must use origin=model_proposal"):
        validate_writing_provenance("generate", context, value)
    restored = SectionContent.model_validate(value)
    with pytest.raises(ValueError, match="must use origin=model_proposal"):
        validate_writing_provenance("generate", context, restored.model_dump())


def test_research_prompt_scopes_actual_nodes_before_derived_task_questions():
    from folio.runtime.base import INSTRUCTIONS

    for task in ("understand_work", "next_action", "query", "review_work"):
        prompt = INSTRUCTIONS[task]
        assert "actual sections" in prompt
        assert "instruction" in prompt
        assert "derived" in prompt or "navigation only" in prompt
    assert (
        "Task goals, section numbering or ownership, material catalogs, and retrieval instructions "
        "are not business facts" in INSTRUCTIONS["understand_work"]
    )


def test_only_duplicate_backed_inline_citations_are_removed():
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    e1, e2 = "ev_12345678", "ev_abcdef12"
    original = {
        "blocks": [
            {
                "block_id": "p",
                "text": "规则A[sfref1]；规则B[ev_abcdef12]。[业务编号1]",
                "evidence_ids": [e1, e2],
                "origin": "evidence",
            },
            {
                "block_id": "t",
                "kind": "table",
                "evidence_ids": [e1],
                "rows": [
                    ["规则", "说明"],
                    ["A", "明确业务要求[sfref1]"],
                    ["B", "未知[sfref2][sfref99]"],
                ],
            },
        ]
    }
    restored = remove_duplicate_inline_citations(original, {"sfref1": e1, "sfref2": e2}, [e1, e2])
    assert restored["blocks"][0]["text"] == "规则A；规则B。[业务编号1]"
    assert restored["blocks"][0]["evidence_ids"] == [e1, e2]
    assert restored["blocks"][0]["origin"] == "evidence"
    assert restored["blocks"][1]["rows"][1][1] == "明确业务要求"
    assert restored["blocks"][1]["rows"][2][1] == "未知[sfref2][sfref99]"
    assert "[sfref1]" in original["blocks"][0]["text"]


def test_forged_or_unattached_citations_remain_rejected():
    from folio.document.presentation import validate_document_content
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    for ids, available in (([], ["ev_12345678"]), (["ev_12345678"], [])):
        value = {"blocks": [{"block_id": "p", "text": "说明[sfref1]", "evidence_ids": ids}]}
        result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, available)
        with pytest.raises(ValueError, match="internal references"):
            validate_document_content(SectionContent.model_validate(result))


@pytest.mark.parametrize("citation", ["sfref1", "[sfref1]", "ev_12345678"])
def test_redundant_trailing_citation_cells_preserve_table_header_and_data(citation):
    from folio.document.presentation import validate_document_content
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    rows = [["名称", "说明"], ["账户", "管理逻辑账户", citation], ["路径", "清算路径", citation]]
    value = {
        "blocks": [
            {"block_id": "t", "kind": "table", "evidence_ids": ["ev_12345678"], "rows": rows}
        ]
    }
    result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, ["ev_12345678"])
    assert result["blocks"][0]["rows"] == [rows[0], rows[1][:2], rows[2][:2]]
    assert value["blocks"][0]["rows"] == rows
    assert len(rows[1]) == 3
    validate_document_content(SectionContent.model_validate(result))


@pytest.mark.parametrize("extra", ["sfref99", "交易规则不得删除", "业务说明sfref1"])
def test_extra_business_cells_or_unknown_references_are_never_dropped(extra):
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    rows = [["名称", "说明"], ["账户", "业务内容", extra]]
    value = {
        "blocks": [
            {"block_id": "t", "kind": "table", "evidence_ids": ["ev_12345678"], "rows": rows}
        ]
    }
    result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, ["ev_12345678"])
    assert result["blocks"][0]["rows"] == rows
    with pytest.raises(ValueError, match="same width"):
        SectionContent.model_validate(result)


def test_bare_citation_in_existing_cell_retains_business_columns():
    from folio.document.presentation import validate_document_content
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    value = {
        "blocks": [
            {
                "block_id": "t",
                "kind": "table",
                "evidence_ids": ["ev_12345678"],
                "rows": [["名称", "备注"], ["账户", "sfref1"]],
            }
        ]
    }
    result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, ["ev_12345678"])
    assert result["blocks"][0]["rows"] == [["名称", "备注"], ["账户", ""]]
    validate_document_content(SectionContent.model_validate(result))
    value["blocks"][0]["evidence_ids"] = []
    result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, ["ev_12345678"])
    with pytest.raises(ValueError, match="internal references"):
        validate_document_content(SectionContent.model_validate(result))


def test_column_containing_only_backed_citations_is_removed():
    from folio.runtime.writing_transport import remove_duplicate_inline_citations

    value = {
        "blocks": [
            {
                "block_id": "t",
                "kind": "table",
                "evidence_ids": ["ev_12345678"],
                "rows": [["账户", "业务数据", "sfref1"], ["路径", "清算数据", "sfref1"]],
            }
        ]
    }
    result = remove_duplicate_inline_citations(value, {"sfref1": "ev_12345678"}, ["ev_12345678"])
    assert result["blocks"][0]["rows"] == [["账户", "业务数据"], ["路径", "清算数据"]]
