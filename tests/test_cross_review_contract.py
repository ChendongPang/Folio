from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from folio.models import Finding, FindingBlockRef, LocalReview, Review
from folio.orchestration.cross_review import cross_review_schema, validate_cross_review


def context():
    return {
        "review_scope": "cross_section_consistency",
        "sections": [
            {
                "section_id": "s1",
                "claims": [{"claim_id": "c1", "block_ids": ["b1"], "evidence_ids": ["ev1"]}],
                "comparison_blocks": [{"block_id": "b1", "text": "虚拟交易豁免反洗钱检查"}],
            },
            {
                "section_id": "s2",
                "claims": [],
                "model_proposals": [
                    {
                        "block_id": "p1",
                        "origin": "model_proposal",
                        "text": "所有交易必须通过反洗钱检查",
                    }
                ],
            },
        ],
    }


def conflict():
    return Finding(
        kind="conflict",
        key="反洗钱适用范围",
        description="通用检查未保留虚拟交易例外",
        section_ids=["s1", "s2"],
        block_refs=[
            FindingBlockRef(section_id="s1", block_id="b1"),
            FindingBlockRef(section_id="s2", block_id="p1"),
        ],
        comparison_scope="虚拟交易",
        incompatibility="一处豁免，另一处要求所有交易检查",
        rule="evidence_conflict",
        evidence_ids=["ev1"],
    )


def test_red_proposal_conflicting_with_explicit_exception_is_not_exempt():
    source = context()
    before = deepcopy(source)
    parsed = Review(findings=[conflict()])
    validate_cross_review(parsed, source)
    Draft202012Validator(cross_review_schema(Review, Review.model_json_schema(), source)).validate(
        parsed.model_dump()
    )
    assert source == before


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "unsupported"},
        {"kind": "coverage"},
        {"basis": "guidance"},
        {"comparison_scope": ""},
        {"incompatibility": "  "},
        {"section_ids": ["s1"]},
        {"section_ids": ["s1", "s1"]},
        {"section_ids": ["s1", "s2", "s3"]},
        {"rule": "citation_required"},
        {"evidence_ids": ["invented"]},
        {"block_refs": []},
        {
            "block_refs": [
                FindingBlockRef(section_id="s1", block_id="p1"),
                FindingBlockRef(section_id="s2", block_id="b1"),
            ]
        },
    ],
)
def test_invalid_cross_comparison_is_rejected_not_silently_dismissed(change):
    value = conflict().model_copy(update=change)
    with pytest.raises(ValueError, match="Cross-section conflict"):
        validate_cross_review(Review(findings=[value]), context())


def test_local_checks_retain_their_source_and_coverage_authority():
    value = Finding(
        kind="unsupported",
        key="missing-source",
        description="真实审批信息无依据",
        section_ids=["s1"],
    )
    validate_cross_review(Review(findings=[value]), {"review_scope": "task_consistency"})
    for model in [LocalReview, Review]:
        schema = cross_review_schema(
            model, model.model_json_schema(), {"review_scope": "task_consistency"}
        )
        assert "comparison_scope" not in schema["$defs"]["Finding"]["properties"]
        assert "unsupported" in schema["$defs"]["Finding"]["properties"]["kind"]["enum"]


def test_schema_requires_actual_sides_and_explanations():
    schema = cross_review_schema(Review, Review.model_json_schema(), context())
    value = Review(findings=[conflict()]).model_dump()
    del value["findings"][0]["incompatibility"]
    assert list(Draft202012Validator(schema).iter_errors(value))
    value = Review(findings=[conflict()]).model_dump()
    value["findings"][0]["block_refs"][1]["block_id"] = "b1"
    assert list(Draft202012Validator(schema).iter_errors(value))


def test_single_sided_page_cannot_invent_a_cross_section_conflict():
    source = context()
    source["sections"] = source["sections"][:1]
    schema = cross_review_schema(Review, Review.model_json_schema(), source)
    assert schema["properties"]["findings"]["maxItems"] == 0
    Draft202012Validator(schema).validate({"findings": []})


def test_capacity_page_keeps_whole_qualified_body_and_does_not_retain_other_pages():
    from folio.orchestration.cross_review import scope_cross_review

    source = context()
    source["sections"][0]["comparison_blocks"].append(
        {"block_id": "b2", "text": "其他节点负责的流程"}
    )
    before = deepcopy(source)
    scoped = scope_cross_review(source)
    assert scoped["sections"][0]["comparison_blocks"] == [
        {"block_id": "b1", "text": "虚拟交易豁免反洗钱检查"}
    ]
    assert scoped["sections"][1]["model_proposals"] == before["sections"][1]["model_proposals"]
    assert source == before


@pytest.mark.asyncio
async def test_long_optional_originals_defer_to_drill_without_truncating_index_or_proposals():
    from types import SimpleNamespace

    from folio.orchestration.requests import RequestWorkflow

    source = context()
    before = deepcopy(source)
    seen, notices = [], []

    class Workflow(RequestWorkflow):
        job_id = "j"
        store = SimpleNamespace(work_event=lambda job, text: notices.append(text))

        async def measure_request(self, task, value, model):
            return {"fits": not any(s.get("comparison_blocks") for s in value["sections"])}

        async def _call(self, task, value, model):
            seen.append(value)
            return Review()

    result = await Workflow().call_bounded("review", source, Review)
    assert result.findings == []
    assert seen[0]["comparison_originals_deferred_to_drill_down"] is True
    assert seen[0]["sections"][0]["claims"] == source["sections"][0]["claims"]
    assert seen[0]["sections"][1]["model_proposals"] == source["sections"][1]["model_proposals"]
    assert source == before
    assert notices


def test_different_applicability_cannot_share_dedup_or_dismissal_identity():
    from folio.orchestration.review_candidates import deduplicate_review_findings

    first = conflict()
    second = first.model_copy(update={"comparison_scope": "隔日虚拟交易"})
    third = first.model_copy(update={"incompatibility": "两处对必须人工确认的条件不一致"})
    assert len({item.fingerprint for item in [first, second, third]}) == 3
    assert len(deduplicate_review_findings([first, second, third]).findings) == 3


async def test_production_review_wire_uses_supported_grammar_and_keeps_application_uniqueness(
    tmp_path,
):
    import json

    import httpx

    from folio.model_settings import ModelSettingsInput, ModelSettingsStore
    from folio.orchestration.engine import Engine
    from folio.runtime.business_transport import translate_ids
    from folio.runtime.llm import LLMRuntime

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
    source = context()
    engine = object.__new__(Engine)
    schema = engine.request_schema("review", source, Review)
    _, actual, _, _, refs = runtime.request_parts("review", source, schema)
    value = Review(findings=[conflict()]).model_dump()
    output = translate_ids(value, {canonical: short for short, canonical in refs.items()})
    requests = []

    def schema_keywords(node):
        if not isinstance(node, dict):
            return set()
        result = set(node)
        for name, child in node.items():
            if name in {"properties", "$defs"}:
                for definition in child.values():
                    result.update(schema_keywords(definition))
            elif name in {"items", "additionalProperties"}:
                result.update(schema_keywords(child))
            elif name in {"anyOf", "allOf", "oneOf"}:
                for definition in child:
                    result.update(schema_keywords(definition))
        return result

    def handler(request):
        payload = json.loads(request.content)
        sent = payload["response_format"]["json_schema"]["schema"]
        requests.append(payload)
        assert sent == actual
        # Audit the final transport schema, not just one intermediate field.
        assert schema_keywords(sent) <= {
            "$defs",
            "$ref",
            "additionalProperties",
            "anyOf",
            "const",
            "default",
            "description",
            "enum",
            "items",
            "maxItems",
            "maxLength",
            "minItems",
            "minLength",
            "properties",
            "required",
            "title",
            "type",
        }
        assert "uniqueItems" not in json.dumps(sent)
        visible = json.JSONDecoder().raw_decode(
            payload["messages"][0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        assert visible == sent
        Draft202012Validator(sent).validate(output)
        # Without the unsupported keyword the wire grammar may accept duplicate
        # IDs. The application must still reject them after decoding.
        duplicate = deepcopy(output)
        duplicate["findings"][0]["section_ids"][1] = duplicate["findings"][0]["section_ids"][0]
        Draft202012Validator(sent).validate(duplicate)
        chunk = {"choices": [{"delta": {"content": json.dumps(output)}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("review", source, schema, "grammar-regression")
        parsed = Review.model_validate_json(result.content)
        assert parsed.model_dump() == value
        engine.check_result(parsed, source)
        duplicate = parsed.model_copy(deep=True)
        duplicate.findings[0].section_ids = ["s1", "s1"]
        with pytest.raises(ValueError, match="Cross-section conflict"):
            engine.check_result(duplicate, source)
        invalid_anchor = parsed.model_copy(deep=True)
        invalid_anchor.findings[0].block_refs[1].block_id = "b1"
        with pytest.raises(ValueError, match="Cross-section conflict"):
            engine.check_result(invalid_anchor, source)
        assert len(requests) == 1
    finally:
        await runtime.aclose()
