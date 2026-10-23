import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.business_transport import (
    business_payload,
    pack_rows,
    translate_ids,
    unpack_rows,
)
from folio.runtime.llm import LLMRuntime


def material():
    return {
        "section": {"section_id": "sec_" + "x" * 30, "title": "sfref1"},
        "content": {
            "blocks": [
                {
                    "block_id": "b1",
                    "kind": "paragraph",
                    "text": '原文\n第二行 "引号" \\路径',
                    "evidence_ids": ["evidence_" + "a" * 60],
                },
                {
                    "block_id": "b2",
                    "kind": "table",
                    "rows": [["字段", "说明"], ["", "sfref1\n换行"]],
                    "evidence_ids": [],
                },
            ]
        },
        "evidence": [
            {
                "evidence_id": "evidence_" + c * 60,
                "text": "完整材料" + c * 200,
                "source_id": "原始文档",
                "locator": "第" + c + "段",
            }
            for c in "abc"
        ],
        "claims": [
            {
                "claim_id": "claim_" + c * 30,
                "statement": "业务结论" + c,
                "evidence_ids": ["evidence_" + c * 60],
                "block_ids": ["b1"],
            }
            for c in "abc"
        ],
        "existing": {"block_id": "sfref1"},
    }


def test_business_payload_is_lossless_and_does_not_rewrite_prose_or_cells():
    original = material()
    original["format_feedback"] = "请检查引用：" + original["evidence"][0]["evidence_id"]
    original["repair_instruction"] = (
        "用原始证据 " + original["evidence"][0]["evidence_id"] + " 修复当前段落。"
    )
    snapshot = json.dumps(original)
    schema = {
        "properties": {
            "evidence_ids": {"items": {"enum": [e["evidence_id"] for e in original["evidence"]]}}
        }
    }
    schema_snapshot = json.dumps(schema)
    wire, output_schema, refs = business_payload(original, schema)
    assert translate_ids(unpack_rows(wire), refs) == original
    assert json.dumps(original) == snapshot and json.dumps(schema) == schema_snapshot
    assert "sfref1" not in refs
    assert wire["section"]["title"] == "sfref1"
    assert wire["content"]["blocks"][1]["rows"] == original["content"]["blocks"][1]["rows"]
    assert all(
        e.startswith("sfref") for e in output_schema["properties"]["evidence_ids"]["items"]["enum"]
    )
    assert len(json.dumps(wire, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))
    result = {
        "section_ids": [wire["section"]["section_id"]],
        "description": wire["section"]["section_id"],
    }
    restored = translate_ids(result, refs)
    assert restored["section_ids"] == [original["section"]["section_id"]]
    # Internal findings must retain canonical references across request namespaces.
    assert restored["description"] == original["section"]["section_id"]


def test_writer_requires_explicit_origin_for_prose_and_tables():
    from folio.models import SectionContent

    schema = SectionContent.model_json_schema()
    for branch in schema["$defs"]["Block"]["anyOf"]:
        assert {"origin", "evidence_ids"}.issubset(branch["required"])


@pytest.mark.parametrize("note_type", ["gap", "review"])
def test_material_kind_is_recovered_from_references_and_cannot_be_promoted(note_type):
    import jsonschema

    from folio.orchestration.context import GapMaterialRead
    from folio.orchestration.focused_review import ReviewMaterialRead
    from folio.runtime.business_transport import material_read_schema, restore_material_kinds

    model = GapMaterialRead if note_type == "gap" else ReviewMaterialRead
    field = "constraints" if note_type == "gap" else "notes"
    context = {
        "catalog": [
            {"context_ref": "r1", "kind": "risk"},
            {"context_ref": "r2", "kind": "risk"},
            {"context_ref": "e1", "kind": "evidence"},
        ]
    }
    schema = material_read_schema(model.model_json_schema(), context)
    valid = {field: [{"source_refs": ["r1", "r2"], "text": "待确认的业务限制"}]}
    jsonschema.validate(valid, schema)
    restored = restore_material_kinds(valid, context)
    assert restored[field][0]["source_kind"] == "risk"
    model.model_validate(restored)
    assert "source_kind" not in valid[field][0]
    for refs in (["r1", "e1"], ["invented"], []):
        wrong = {field: [{"source_refs": refs, "text": "约束"}]}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(wrong, schema)
        with pytest.raises(ValueError):
            restore_material_kinds(wrong, context)
    with pytest.raises(ValueError):
        restore_material_kinds({field: [valid[field][0] | {"source_kind": "fact"}]}, context)


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{}, {}],
        [{"a": None}, {"a": False, "b": []}],
        [{"a": "", "b": 0, "c": []}, {"c": {}, "a": "\n", "b": True}],
    ],
)
def test_row_encoding_preserves_empty_values_heterogeneous_objects_and_order(value):
    assert unpack_rows(pack_rows(value)) == value


def test_repeated_material_keeps_distinct_evidence_sources_and_full_original_text():
    original_text = '完整原文\n保留 "引号"、\\路径和不同来源。' * 50
    context = {
        "evidence": [
            {
                "evidence_id": "ev_" + str(index) * 32,
                "text": original_text,
                "source_id": "source-" + str(index),
                "metadata": {
                    "url": "https://source.test/" + str(index),
                    "project_fact": index == 0,
                },
            }
            for index in range(3)
        ],
        "content": {
            "blocks": [
                {"text": '原文中的字面量 {"$same":0}', "rows": [[original_text, "保留正文表格"]]}
            ]
        },
    }
    before = json.dumps(context, ensure_ascii=False)
    wire, _, refs = business_payload(context, {})
    serialized = json.dumps(wire, ensure_ascii=False)
    assert len(serialized) < len(before) - len(original_text)
    assert wire["evidence"]["$row_refs"] == 1
    text_column = wire["evidence"]["$columns"].index("text")
    assert wire["evidence"]["$rows"][0][text_column] == original_text
    assert wire["evidence"]["$rows"][1][text_column] == {"$same": 0}
    restored = translate_ids(unpack_rows(json.loads(serialized)), refs)
    assert restored == context
    assert len({e["evidence_id"] for e in restored["evidence"]}) == 3
    assert {e["source_id"] for e in restored["evidence"]} == {"source-0", "source-1", "source-2"}
    assert wire["content"]["blocks"][0]["rows"] == context["content"]["blocks"][0]["rows"]
    assert json.dumps(context, ensure_ascii=False) == before


def test_nested_shared_rows_restore_recursively_without_aliasing_mutable_values():
    paragraphs = [
        {"id": n, "text": "保留所有原文" * 50, "details": {"note": "完整说明" * 20}}
        for n in range(3)
    ]
    values = [{"id": n, "catalog": paragraphs, "role": "来源身份"} for n in range(2)]
    packed = pack_rows(values)
    assert packed["$row_refs"] == 1
    decoded = unpack_rows(json.loads(json.dumps(packed)))
    assert decoded == values
    decoded[0]["catalog"][0]["details"]["note"] = "edited"
    assert decoded[1]["catalog"] == paragraphs
    assert decoded[0]["catalog"][1]["details"] == paragraphs[1]["details"]


def test_literal_same_objects_are_not_interpreted_as_row_references():
    values = [{"a": {"$same": 0}, "b": "long" * 100, "c": n} for n in range(2)]
    packed = pack_rows(values)
    assert "$row_refs" not in packed
    assert unpack_rows(packed) == values
    assert "$row_refs" not in pack_rows([{"a": "x", "b": "y", "c": "z"}] * 2)


@pytest.mark.parametrize("reference", [-1, 0, True, "0"])
def test_invalid_or_forward_row_references_are_rejected(reference):
    with pytest.raises(ValueError, match="earlier row"):
        unpack_rows({"$columns": ["a"], "$rows": [[{"$same": reference}]], "$row_refs": 1})


@pytest.mark.parametrize("mode", ["json_schema", "prompt"])
async def test_business_request_budget_measures_actual_wire_and_restores_output(tmp_path, mode):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1", model="test", no_auth=True, structured_output=mode
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    context = material()
    context["evidence"][1]["text"] = context["evidence"][0]["text"]
    schema = {
        "type": "object",
        "properties": {"section_ids": {"type": "array", "items": {"type": "string"}}},
    }

    def handler(request):
        data = json.loads(request.content)
        messages = data["messages"]
        wire = json.loads(messages[1]["content"])
        assert wire["evidence"]["$row_refs"] == 1
        assert runtime.prompt_chars("validate", context, schema) == sum(
            len(m["content"]) for m in messages
        )
        visible_schema = json.JSONDecoder().raw_decode(
            messages[0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        if mode == "json_schema":
            assert visible_schema == data["response_format"]["json_schema"]["schema"]
            assert data["response_format"]["json_schema"]["strict"] is True
        else:
            assert "response_format" not in data
        assert visible_schema["properties"]["section_ids"]["items"]["type"] == "string"
        assert "$columns" in messages[1]["content"]
        result = {"section_ids": [wire["section"]["section_id"]]}
        chunk = {"choices": [{"delta": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("validate", context, schema, "test")
        assert json.loads(result.content)["section_ids"] == [context["section"]["section_id"]]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "model_name", ["BusinessRecord", "SectionContent", "Extraction", "ActionDecision"]
)
def test_evidence_enum_only_contains_current_sources_and_survives_aliasing(model_name):
    from folio.decisions import ActionDecision
    from folio.models import Extraction, SectionContent
    from folio.orchestration.work import BusinessRecord
    from folio.runtime.business_transport import evidence_schema

    models = {c.__name__: c for c in (BusinessRecord, SectionContent, Extraction, ActionDecision)}
    schema = models[model_name].model_json_schema()
    context = material()
    # Both a different request's evidence and a section ref exist, but neither is a legal source.
    context["previous_record"] = {
        "facts": [{"fact_id": "old_fact", "evidence_ids": ["old_evidence_" + "z" * 40]}]
    }
    before = json.dumps(schema)
    constrained = evidence_schema(schema, context)
    wire, converted, refs = business_payload(context, constrained)

    def items(s):
        if model_name == "ActionDecision":
            return s["properties"]["evidence_refs"]
        definition = {
            "BusinessRecord": "BusinessFact",
            "SectionContent": "Block",
            "Extraction": "Claim",
        }[model_name]
        node = s["$defs"][definition]
        if "anyOf" in node:
            branches = [b["properties"]["evidence_ids"] for b in node["anyOf"]]
            assert all(b == branches[0] for b in branches)
            return branches[0]
        return node["properties"]["evidence_ids"]

    expected = {e["evidence_id"] for e in context["evidence"]}
    assert set(items(constrained)["items"]["enum"]) == expected
    assert {refs[i] for i in items(converted)["items"]["enum"]} == expected
    assert wire["section"]["section_id"] not in items(converted)["items"]["enum"]
    assert json.dumps(schema) == before
    empty = evidence_schema(schema, context | {"evidence": []})
    assert items(empty)["maxItems"] == 0
    if model_name == "BusinessRecord":
        assert empty["properties"]["facts"]["maxItems"] == 0


@pytest.mark.parametrize("mode", ["json_schema", "prompt"])
async def test_zero_verdict_review_omits_constant_on_wire_and_restores_it(tmp_path, mode):
    from folio.models import LocalReview

    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1", model="test", no_auth=True, structured_output=mode
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    schema = LocalReview.model_json_schema()
    schema["properties"]["verdicts"].update({"minItems": 0, "maxItems": 0})
    context = {
        "section": {"section_id": "s"},
        "evidence": [],
        "extraction": {"claims": []},
        "validation_scope": "body",
    }

    def handler(request):
        payload = json.loads(request.content)
        if mode == "json_schema":
            wire_schema = payload["response_format"]["json_schema"]["schema"]
        else:
            wire_schema = json.loads(
                payload["messages"][0]["content"]
                .split("JSON Schema:\n")[1]
                .split("\nTask data (JSON):")[0]
            )
        assert "verdicts" not in wire_schema["properties"]
        assert "verdicts" not in wire_schema["required"]
        result = {
            "findings": [
                {
                    "kind": "unsupported",
                    "description": "一项业务断言缺少依据",
                    "section_ids": ["s"],
                    "key": "business-rule",
                }
            ]
        }
        event = {"choices": [{"delta": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("validate", context, schema, "zero-verdict")
        review = LocalReview.model_validate_json(result.content)
        assert review.verdicts == [] and len(review.findings) == 1
        assert "verdicts" in schema["properties"]
    finally:
        await runtime.aclose()


def test_omit_empty_fields_keeps_real_lists_and_does_not_mask_invalid_returned_values():
    from folio.models import SectionContent
    from folio.runtime.business_transport import omit_empty_fields, restore_empty_fields

    schema = SectionContent.model_json_schema()
    wire = omit_empty_fields(schema)
    prose, table = wire["$defs"]["Block"]["anyOf"]
    assert "rows" not in prose["properties"]
    assert table["properties"]["rows"]["minItems"] == 1
    assert "rows" in schema["$defs"]["Block"]["anyOf"][0]["properties"]
    assert restore_empty_fields(
        {"verdicts": ["bad"]}, {"properties": {"verdicts": {"type": "array", "maxItems": 0}}}
    ) == {"verdicts": ["bad"]}


async def test_catalog_without_risks_requests_selection_only(tmp_path):
    from folio.orchestration.context import ContextSelection
    from folio.runtime.business_transport import restore_empty_fields

    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            structured_output="json_schema",
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    schema = ContextSelection.model_json_schema()
    schema["properties"]["risk_refs"]["maxItems"] = 0
    schema["properties"]["risk_summary"]["const"] = ""
    context = {"catalog": [{"context_ref": "ev_source1234567890", "kind": "evidence", "value": {}}]}

    def handler(request):
        payload = json.loads(request.content)
        wire_schema = payload["response_format"]["json_schema"]["schema"]
        assert set(wire_schema["properties"]) == {"selected_refs"}
        event = {
            "choices": [
                {"delta": {"content": '{"selected_refs":["sfref1"]}'}, "finish_reason": "stop"}
            ]
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("select_context", context, schema, "no-risk-catalog")
        parsed = ContextSelection.model_validate_json(result.content)
        assert parsed.selected_refs == ["ev_source1234567890"]
        assert parsed.risk_refs == [] and parsed.risk_summary == ""
        assert (
            restore_empty_fields({"risk_summary": "invalid"}, schema)["risk_summary"] == "invalid"
        )
    finally:
        await runtime.aclose()


def test_internal_finding_references_survive_next_request_namespace():
    from folio.runtime.business_transport import translate_ids

    first = {"description": "正文与 sfref3 的规则冲突", "key": "sfref3", "reason": "需核对 sfref3"}
    saved = translate_ids(first, {"sfref3": "ctx_source"})
    assert saved["key"] == "ctx_source" and "ctx_source" in saved["description"]
    forwarded = translate_ids(saved, {"ctx_source": "sfref8"})
    assert "sfref8" in forwarded["description"] and "sfref3" not in forwarded["reason"]
    assert translate_ids({"text": "正文 sfref3"}, {"sfref3": "ctx_source"}) == {
        "text": "正文 sfref3"
    }
