import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Claim, Extraction, LocalReview
from folio.orchestration.engine import Engine
from folio.runtime.business_transport import business_payload, translate_ids, unpack_rows
from folio.runtime.extraction_transport import (
    CLAIM_KEYS,
    EXTRACTION_KEYS,
    extraction_context,
    extraction_schema,
    extraction_scope_schema,
    restore_extraction,
)
from folio.runtime.llm import LLMRuntime


def context_and_result():
    evidence = "ev_" + "a" * 32
    context = {
        "section": {"section_id": "sec_" + "x" * 32, "title": "静态数据维护"},
        "content": {
            "blocks": [{"block_id": "b1", "text": "只有已授权角色可修改，已引用的数据不可删除。"}]
        },
        "evidence": [{"evidence_id": evidence, "text": "已引用的数据不可删除。"}],
    }
    result = {
        "summary": '静态数据维护，保留 "限制"\n第二行',
        "claims": [
            {
                "claim_id": "candidate_1",
                "block_ids": ["b1"],
                "statement": "在已被引用的情况下，不允许删除静态数据；sfref1 是原文字面值。",
                "subject": "静态数据",
                "predicate": "允许删除",
                "value": "否",
                "scope": "已被引用；业务角色已授权",
                "cardinality": "single",
                "evidence_ids": [evidence],
                "status": "candidate",
            }
        ],
        "entities": ["静态数据", "已授权角色", "sfref1"],
    }
    return context, result


def output_schema(context):
    return Engine.request_schema(Engine.__new__(Engine), "extract", context, Extraction)


def wire_value(result):
    value = {EXTRACTION_KEYS[k]: deepcopy(v) for k, v in result.items()}
    value["c"] = [
        {CLAIM_KEYS[k]: v for k, v in claim.items() if k != "status"}
        for claim in value.get("c", [])
    ]
    return value


def test_extraction_round_trip_keeps_all_qualifiers_entities_and_reference_values():
    context, original = context_and_result()
    canonical = output_schema(context)
    before = deepcopy(canonical)
    _, schema, references = business_payload(context, canonical)
    schema_before = deepcopy(schema)
    compact = extraction_schema("extract", schema)
    value = wire_value(translate_ids(original, {v: k for k, v in references.items()}))
    snapshot = deepcopy(value)
    restored = translate_ids(restore_extraction(value, compact), references)
    assert restored == original
    assert Extraction.model_validate(restored) == Extraction.model_validate(original)
    assert value == snapshot and canonical == before and schema == schema_before
    assert references and restored["entities"][-1] == "sfref1"
    assert "sfref1 是原文字面值" in restored["claims"][0]["statement"]
    assert len(json.dumps(value)) < len(json.dumps(original))


def test_extraction_schema_keeps_exact_dynamic_limits_and_has_no_tuple_schema():
    context, _ = context_and_result()
    _, schema, _ = business_payload(context, output_schema(context))
    compact = extraction_schema("extract", schema)
    for full, short in EXTRACTION_KEYS.items():
        assert compact["properties"][short] == schema["properties"][full]
    for full, short in CLAIM_KEYS.items():
        assert (
            compact["$defs"]["Claim"]["properties"][short]
            == schema["$defs"]["Claim"]["properties"][full]
        )
    assert compact["$defs"]["Claim"]["required"] == [
        CLAIM_KEYS[k] for k in schema["$defs"]["Claim"]["required"]
    ]
    assert "status" not in compact["$defs"]["Claim"]["properties"]
    assert "prefixItems" not in json.dumps(compact)
    Draft202012Validator.check_schema(compact)


@pytest.mark.parametrize(
    "fault", ["source", "block", "statement", "missing_source", "count", "status", "mixed", "type"]
)
def test_short_output_cannot_bypass_existing_contract(fault):
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    schema = extraction_schema("extract", schema)
    value = wire_value(translate_ids(result, {v: k for k, v in refs.items()}))
    claim = value["c"][0]
    if fault == "source":
        claim["e"] = ["invented_source"]
    elif fault == "block":
        claim["b"] = ["invented_block"]
    elif fault == "statement":
        claim["t"] = "长" * 3501
    elif fault == "missing_source":
        del claim["e"]
    elif fault == "count":
        value["c"] *= 13
    elif fault == "status":
        claim["status"] = "supported"
    elif fault == "mixed":
        claim["statement"] = claim["t"]
    else:
        claim["e"] = "not an array"
    with pytest.raises(ValueError, match="Invalid compact"):
        restore_extraction(value, schema)


@pytest.mark.parametrize("field", ["statement", "subject", "predicate", "value", "scope"])
def test_claim_field_lengths_follow_domain_contract_without_hidden_smaller_limits(field):
    context, result = context_and_result()
    domain = Claim.model_json_schema()["properties"][field]["maxLength"]
    canonical = output_schema(context)
    assert canonical["$defs"]["Claim"]["properties"][field]["maxLength"] == domain
    _, wire, refs = business_payload(context, canonical)
    wire = extraction_schema("extract", wire)
    result["claims"][0][field] = "长" * domain
    value = wire_value(translate_ids(result, {v: k for k, v in refs.items()}))
    restored = translate_ids(restore_extraction(value, wire), refs)
    assert restored["claims"][0][field] == result["claims"][0][field]
    value["c"][0][CLAIM_KEYS[field]] += "长"
    with pytest.raises(ValueError) as error:
        restore_extraction(value, wire)
    assert f"c[0].{CLAIM_KEYS[field]}: maxLength" in str(error.value)
    assert f"expected <= {domain} characters; actual {domain + 1}" in str(error.value)


@pytest.mark.parametrize("legacy", [False, True])
def test_249_character_statement_is_preserved_in_both_response_formats(legacy):
    context, result = context_and_result()
    result["claims"][0]["statement"] = "含适用范围和限定条件的完整事实" + "详" * 234
    assert len(result["claims"][0]["statement"]) == 249
    _, schema, refs = business_payload(context, output_schema(context))
    encoded = translate_ids(result, {v: k for k, v in refs.items()})
    restored = restore_extraction(
        encoded if legacy else wire_value(encoded), extraction_schema("extract", schema)
    )
    assert restored["claims"][0]["statement"] == result["claims"][0]["statement"]


def test_compact_errors_are_bounded_actionable_and_do_not_quote_private_output():
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    schema = extraction_schema("extract", schema)
    value = wire_value(translate_ids(result, {v: k for k, v in refs.items()}))
    value["c"][0]["t"] = "PRIVATE_FACT_" + "私" * 3500
    value["c"][0]["e"] = ["PRIVATE_INVALID_SOURCE"]
    value["c"][0]["s"] = {"PRIVATE_FIELD_NAME": "PRIVATE_FIELD_VALUE"}
    del value["c"][0]["k"]
    with pytest.raises(ValueError) as error:
        restore_extraction(value, schema)
    detail = str(error.value)
    assert "c[0].t: maxLength; expected <= 3500 characters; actual 3513" in detail
    assert "c[0].e[0]: enum; expected one of 1 allowed evidence references" in detail
    assert "c[0].s: type; expected string; actual dict" in detail
    assert "c[0]: required; missing required fields: k" in detail
    assert "PRIVATE" not in detail
    value["c"] *= 12
    with pytest.raises(ValueError) as many:
        restore_extraction(value, schema)
    assert "additional schema errors omitted" in str(many.value)
    assert len(str(many.value)) < 2100 and "PRIVATE" not in str(many.value)


@pytest.mark.parametrize("length", [2011, 2122, 3500])
def test_extraction_summary_accepts_realistic_section_length_and_keeps_bound(length):
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    wire = extraction_schema("extract", schema)
    value = wire_value(translate_ids(result, {v: k for k, v in refs.items()}))
    value["s"] = ("Section summary. " * 220)[:length]
    assert len(value["s"]) == length

    restored = translate_ids(restore_extraction(value, wire), refs)
    assert restored["summary"] == value["s"]

    value["s"] += "x" * (3501 - len(value["s"]))
    with pytest.raises(ValueError) as error:
        restore_extraction(value, wire)
    assert "s: maxLength; expected <= 3500 characters; actual 3501" in str(error.value)


@pytest.mark.parametrize("status", ["candidate", "supported", None])
def test_canonical_responses_remain_compatible_but_never_self_approve(status):
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    schema = extraction_schema("extract", schema)
    original = deepcopy(result)
    if status is None:
        del result["claims"][0]["status"]
    else:
        result["claims"][0]["status"] = status
    canonical_wire = translate_ids(result, {v: k for k, v in refs.items()})
    restored = translate_ids(restore_extraction(canonical_wire, schema), refs)
    assert restored == original
    # Previously persisted canonical records still use the same public model.
    assert Extraction.model_validate_json(json.dumps(original)).model_dump() == original


def test_canonical_response_rejects_mixed_keys_and_unsupported_status():
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    schema = extraction_schema("extract", schema)
    result = translate_ids(result, {v: k for k, v in refs.items()})
    with pytest.raises(ValueError, match="Mixed"):
        restore_extraction(result | {"s": "ambiguous"}, schema)
    result["claims"][0]["status"] = {"supported": True}
    with pytest.raises(ValueError, match="status"):
        restore_extraction(result, schema)


def test_legacy_default_cardinality_is_restored_without_guessing_required_sources():
    context, result = context_and_result()
    _, schema, refs = business_payload(context, output_schema(context))
    compact = extraction_schema("extract", schema)
    del result["claims"][0]["cardinality"]
    del result["claims"][0]["scope"]
    canonical_wire = translate_ids(result, {v: k for k, v in refs.items()})
    restored = translate_ids(restore_extraction(canonical_wire, compact), refs)
    assert Extraction.model_validate(restored) == Extraction.model_validate(result)
    assert restored["claims"][0]["cardinality"] == "unspecified"
    # New wire output must still satisfy its explicit required fields.
    with pytest.raises(ValueError, match="Invalid compact"):
        restore_extraction(wire_value(canonical_wire), compact)
    del canonical_wire["claims"][0]["evidence_ids"]
    with pytest.raises(ValueError, match="Invalid compact"):
        restore_extraction(canonical_wire, compact)


def test_empty_sources_and_no_claims_stay_compatible_with_omitted_empty_fields():
    context, result = context_and_result()
    context["evidence"] = []
    result["claims"][0]["evidence_ids"] = []
    _, schema, refs = business_payload(context, output_schema(context))
    compact = extraction_schema("extract", schema)
    assert "e" not in compact["$defs"]["Claim"]["properties"]
    canonical_wire = translate_ids(result, {v: k for k, v in refs.items()})
    restored = translate_ids(restore_extraction(canonical_wire, compact), refs)
    assert Extraction.model_validate(restored) == Extraction.model_validate(result)
    assert restore_extraction({"s": "纯模型建议，无已核实事实", "c": [], "e": []}, compact) == {
        "summary": "纯模型建议，无已核实事实",
        "claims": [],
        "entities": [],
    }


def test_other_tasks_generic_schemas_and_persisted_models_are_unchanged():
    canonical = Extraction.model_json_schema()
    assert extraction_schema("validate", canonical) is canonical
    generic = {"type": "object", "properties": {"text": {"type": "string"}}}
    assert extraction_schema("extract", generic) is generic
    assert restore_extraction({"text": "unchanged"}, generic) == {"text": "unchanged"}
    assert "summary" in Extraction.model_json_schema()["properties"]


@pytest.mark.parametrize("mode", ["json_schema", "prompt"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_runtime_measures_and_sends_short_schema_then_restores_canonical(
    tmp_path, mode, legacy
):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1", model="test", no_auth=True, structured_output=mode
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    context, expected = context_and_result()
    schema = output_schema(context)

    def handler(request):
        payload = json.loads(request.content)
        messages = payload["messages"]
        wire_schema = json.JSONDecoder().raw_decode(
            messages[0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        assert runtime.prompt_chars("extract", context, schema) == sum(
            len(m["content"]) for m in messages
        )
        assert set(wire_schema["properties"]) == {"s", "c", "e"}
        assert "status" not in wire_schema["$defs"]["Claim"]["properties"]
        assert "Subsequent independent validation" in messages[0]["content"]
        if mode == "json_schema":
            assert payload["response_format"]["json_schema"]["schema"] == wire_schema
        else:
            assert "response_format" not in payload
        _, _, refs = business_payload(context, schema)
        output = translate_ids(expected, {v: k for k, v in refs.items()})
        if legacy:
            output["claims"][0]["status"] = "supported"
        else:
            output = wire_value(output)
        chunk = {"choices": [{"delta": {"content": json.dumps(output)}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        actual = await runtime.complete("extract", context, schema, "test")
        assert json.loads(actual.content) == expected
        assert Extraction.model_validate_json(actual.content).model_dump() == expected
    finally:
        await runtime.aclose()


def mixed_provenance_context():
    context, result = context_and_result()
    first = context["content"]["blocks"][0]
    first.update(block_id="block_" + "a" * 32, kind="paragraph", origin="evidence")
    table = {
        "block_id": "block_" + "t" * 32,
        "kind": "table",
        "origin": "evidence",
        "rows": [["场景", "限制"], ["sfref1 已引用", "禁止删除"]],
        "evidence_ids": [context["evidence"][0]["evidence_id"]],
    }
    proposal = {
        "block_id": "block_" + "p" * 32,
        "kind": "paragraph",
        "origin": "model_proposal",
        "text": "建议增加夜间批量校验；失败时禁止自动删除。sfref1 是原文字面值。",
        "evidence_ids": [],
    }
    context["content"]["blocks"] += [table, proposal]
    result["claims"][0]["block_ids"] = [first["block_id"], table["block_id"]]
    result["summary"] += "；建议夜间批量校验，失败时禁止自动删除。"
    result["entities"] += ["夜间批量校验"]
    return context, result


def test_fact_scope_excludes_only_proposal_blocks_and_preserves_narrower_contracts():
    context, _ = mixed_provenance_context()
    schema = output_schema(context)
    before = deepcopy((context, schema))
    actual = extraction_scope_schema(schema, context)
    ids = [b["block_id"] for b in context["content"]["blocks"]]
    assert actual["$defs"]["Claim"]["properties"]["block_ids"]["items"]["enum"] == ids[:2]
    assert actual["properties"]["claims"]["maxItems"] == 12
    assert (context, schema) == before
    schema["$defs"]["Claim"]["properties"]["block_ids"]["items"]["enum"] = ids[1:]
    narrowed = extraction_scope_schema(schema, context)
    assert narrowed["$defs"]["Claim"]["properties"]["block_ids"]["items"]["enum"] == ids[1:2]
    # The legacy persisted Block default is evidence. Missing provenance in an
    # old context must not silently remove those actual fact blocks.
    del context["content"]["blocks"][1]["origin"]
    assert extraction_scope_schema(schema, context) == narrowed


@pytest.mark.parametrize("legacy", [False, True])
def test_compact_and_canonical_claims_cannot_cite_red_or_unknown_blocks(legacy):
    context, result = mixed_provenance_context()
    _, schema, refs = business_payload(context, output_schema(context))
    compact = extraction_schema("extract", schema)
    aliases = {v: k for k, v in refs.items()}
    good = translate_ids(result, aliases)
    assert (
        translate_ids(restore_extraction(good if legacy else wire_value(good), compact), refs)
        == result
    )
    for bad_id in [context["content"]["blocks"][-1]["block_id"], "unknown_block"]:
        bad = deepcopy(result)
        # Mixing a real fact block with a proposal must not launder the proposal.
        bad["claims"][0]["block_ids"].append(bad_id)
        bad = translate_ids(bad, aliases)
        with pytest.raises(ValueError, match="Invalid compact"):
            restore_extraction(bad if legacy else wire_value(bad), compact)


@pytest.mark.parametrize("empty", [False, True])
def test_no_fact_blocks_disables_claims_without_an_invalid_empty_enum(empty):
    context, result = mixed_provenance_context()
    context["content"]["blocks"] = [] if empty else context["content"]["blocks"][-1:]
    _, schema, refs = business_payload(context, output_schema(context))
    compact = extraction_schema("extract", schema)
    assert compact["properties"]["c"]["maxItems"] == 0
    assert "enum" not in compact["$defs"]["Claim"]["properties"]["b"]["items"]
    Draft202012Validator.check_schema(compact)
    result["claims"] = []
    for value in (result, wire_value(result)):
        assert translate_ids(restore_extraction(value, compact), refs) == result
    invented = wire_value(context_and_result()[1])
    with pytest.raises(ValueError, match="Invalid compact"):
        restore_extraction(invented, compact)


@pytest.mark.parametrize("mode", ["json_schema", "prompt"])
@pytest.mark.parametrize("all_red", [False, True])
async def test_http_fact_scope_preserves_full_body_for_summary_entities_and_validation(
    tmp_path, mode, all_red
):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1", model="test", no_auth=True, structured_output=mode
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    context, expected = mixed_provenance_context()
    if all_red:
        for block in context["content"]["blocks"]:
            block.update(origin="model_proposal", evidence_ids=[])
        expected["claims"] = []
    before = deepcopy(context)
    schema = output_schema(context)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        messages = payload["messages"]
        actual = json.JSONDecoder().raw_decode(
            messages[0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        _, _, refs = business_payload(context, schema)
        sent = translate_ids(unpack_rows(json.loads(messages[1]["content"])), refs)
        assert sent == extraction_context(context)
        assert sent["content"] == context["content"]  # Every paragraph, table cell and literal.
        scope = sent["fact_extraction_scope"]
        assert scope["fact_block_ids"] == [
            b["block_id"] for b in context["content"]["blocks"]
            if b.get("origin") != "model_proposal"
        ]
        assert scope["summary_only_block_ids"] == [
            b["block_id"] for b in context["content"]["blocks"]
            if b.get("origin") == "model_proposal"
        ]
        assert runtime.prompt_chars("extract", context, schema) == sum(
            len(m["content"]) for m in messages
        )
        if mode == "json_schema":
            assert payload["response_format"]["json_schema"]["schema"] == actual
        if all_red:
            assert actual["properties"]["c"]["maxItems"] == 0
        else:
            allowed = actual["$defs"]["Claim"]["properties"]["b"]["items"]["enum"]
            assert all(ref.startswith("sfref") for ref in allowed)
            assert [refs[ref] for ref in allowed] == [
                b["block_id"] for b in context["content"]["blocks"][:2]
            ]
        output = wire_value(translate_ids(expected, {v: k for k, v in refs.items()}))
        chunk = {"choices": [{"delta": {"content": json.dumps(output)}, "finish_reason": "stop"}]}
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("extract", context, schema, "test")
        assert json.loads(result.content) == expected
        assert len(calls) == 1 and context == before
        # Independent validation retains the complete body, including proposals.
        messages, _, _, _, refs = runtime.request_parts(
            "validate", context, LocalReview.model_json_schema()
        )
        assert translate_ids(unpack_rows(json.loads(messages[1]["content"])), refs) == before
    finally:
        await runtime.aclose()
