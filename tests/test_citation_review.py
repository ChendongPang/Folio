import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import JobInput, LocalReview, Review
from folio.orchestration.citation_review import (
    citation_review_schema,
    validate_citation_review,
)
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.business_transport import translate_ids
from folio.runtime.demo import DemoRuntime
from folio.runtime.llm import LLMRuntime

S1, S2 = "section_original_0001", "section_original_0002"
B1, B2, RED = "block_original_00001", "block_original_00002", "block_proposal_00001"


def source(task):
    first = {
        "section_id": S1,
        "content": {
            "blocks": [
                {"block_id": B1, "text": "事实待核对", "origin": "evidence", "evidence_ids": []},
                {
                    "block_id": RED,
                    "text": "拟支持审批查询；不得冒称已经批准。",
                    "origin": "model_proposal",
                    "evidence_ids": [],
                },
            ]
        },
    }
    if task == "validate":
        return {
            "section": {"section_id": S1},
            "content": first["content"],
            "evidence": [],
            "extraction": {"claims": []},
        }
    return {
        "review_scope": "task_consistency",
        "sections": [
            first,
            {
                "section_id": S2,
                "content": {"blocks": [{"block_id": B2, "text": "历史事实，沿用默认来源类型"}]},
            },
        ],
    }


def diagnosis(task, *, block_id=B1, section_id=S1, rule="citation_required", kind="coverage"):
    value = {
        "findings": [
            {
                "kind": kind,
                "key": "业务内容核对",
                "description": "当前正文需核对来源。",
                "section_ids": [section_id],
                "block_refs": [{"section_id": section_id, "block_id": block_id}],
                "evidence_ids": [],
                "rule": rule,
            }
        ]
    }
    return value | ({"verdicts": []} if task == "validate" else {})


@pytest.mark.parametrize("task,model", [("validate", LocalReview), ("review_work", Review)])
@pytest.mark.parametrize("kind", ["coverage", "unsupported"])
def test_missing_citation_requires_actual_non_red_anchor_regardless_of_kind(task, model, kind):
    engine = object.__new__(Engine)
    context = source(task)
    before = deepcopy(context)
    schema = engine.request_schema(task, context, model)
    validator = Draft202012Validator(schema)
    valid = diagnosis(task, kind=kind)
    validator.validate(valid)
    engine.check_result(model.model_validate(valid), context)
    for block_id, section_id in [(RED, S1), (B2, S1), (B1, S2), ("unknown", S1)]:
        invalid = diagnosis(task, block_id=block_id, section_id=section_id, kind=kind)
        assert list(validator.iter_errors(invalid))
        with pytest.raises(ValueError, match="citation_required"):
            engine.check_result(model.model_validate(invalid), context)
    empty = diagnosis(task, kind=kind)
    empty["findings"][0]["block_refs"] = []
    assert list(validator.iter_errors(empty))
    with pytest.raises(ValueError, match="citation_required"):
        engine.check_result(model.model_validate(empty), context)
    assert context == before


@pytest.mark.parametrize("rule", ["evidence_conflict", "fabricated_fact", "coverage", "other"])
def test_all_red_body_still_accepts_real_conflict_fabrication_and_coverage(rule):
    engine = object.__new__(Engine)
    context = source("validate")
    context["content"]["blocks"] = context["content"]["blocks"][1:]
    schema = engine.request_schema("validate", context, LocalReview)
    value = diagnosis("validate", block_id=RED, rule=rule)
    Draft202012Validator(schema).validate(value)
    engine.check_result(LocalReview.model_validate(value), context)
    value["findings"][0]["rule"] = "citation_required"
    assert list(Draft202012Validator(schema).iter_errors(value))
    with pytest.raises(ValueError, match="citation_required"):
        engine.check_result(LocalReview.model_validate(value), context)


def test_mixed_refs_cannot_attach_red_or_other_pages_but_legacy_facts_remain_checkable():
    engine = object.__new__(Engine)
    context = source("review_work")
    value = diagnosis("review_work", block_id=B2, section_id=S2)
    schema = engine.request_schema("review_work", context, Review)
    Draft202012Validator(schema).validate(value)
    engine.check_result(Review.model_validate(value), context)
    value["findings"][0]["block_refs"].append({"section_id": S1, "block_id": RED})
    value["findings"][0]["section_ids"].append(S1)
    assert list(Draft202012Validator(schema).iter_errors(value))
    with pytest.raises(ValueError, match="citation_required"):
        engine.check_result(Review.model_validate(value), context)
    page = context | {"sections": context["sections"][:1]}
    with pytest.raises(ValueError, match="citation_required"):
        engine.check_result(
            Review.model_validate(diagnosis("review_work", block_id=B2, section_id=S2)), page
        )


def test_note_only_review_keeps_existing_contract_and_never_infers_origin():
    context = {"sections": [{"section_id": S1, "summary": "没有原文"}]}
    schema = Review.model_json_schema()
    assert citation_review_schema("review_work", schema, context) is schema
    validate_citation_review(Review.model_validate(diagnosis("review_work")), context)


def test_anonymous_prose_does_not_create_an_invented_citation_anchor():
    engine = object.__new__(Engine)
    context = {"sections": [{"section_id": S1, "content": {"blocks": [{"text": "完整原文"}]}}]}
    before = deepcopy(context)
    schema = engine.request_schema("review_work", context, Review)
    Draft202012Validator(schema).validate({"findings": []})
    assert "citation_required" not in schema["$defs"]["Finding"]["properties"]["rule"]["enum"]
    assert context == before


async def test_invalid_model_result_retries_and_archives_instead_of_dropping_finding(store, inputs):
    class Reviewer(DemoRuntime):
        calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            if self.calls == 2:
                assert "citation_required" in context["format_feedback"]
            # Preserve a real factual-conflict diagnosis on retry; never silently remove it.
            return RuntimeResult(
                json.dumps(
                    diagnosis(
                        task,
                        block_id=RED,
                        rule="citation_required" if self.calls == 1 else "evidence_conflict",
                    )
                )
            )

    runtime = Reviewer()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    context = source("validate")
    original = deepcopy(context)
    result = await engine._call("validate", context, LocalReview)
    assert runtime.calls == 2 and result.findings[0].rule == "evidence_conflict"
    assert context == original
    rejected = list((store.root / "jobs" / engine.job_id / "contexts/rejected").glob("*.json"))
    assert len(rejected) == 1
    assert (
        json.loads(rejected[0].read_text())["candidate"]["findings"][0]["rule"]
        == "citation_required"
    )
    assert await engine._call("validate", context, LocalReview) == result
    assert runtime.calls == 2


async def test_real_transport_short_ids_preserve_paired_origin_constraints(tmp_path):
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
    engine = object.__new__(Engine)
    context = source("review_work")
    schema = engine.request_schema("review_work", context, Review)
    _, actual, _, _, refs = runtime.request_parts("review_work", context, schema)
    aliases = {canonical: short for short, canonical in refs.items()}
    value = diagnosis("review_work", block_id=B2, section_id=S2)
    for finding in value["findings"]:
        finding.setdefault("basis", "content")
    wire = translate_ids(value, aliases)
    assert wire["findings"][0]["block_refs"][0] == {
        "section_id": aliases[S2],
        "block_id": aliases[B2],
    }
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        sent = payload["response_format"]["json_schema"]["schema"]
        assert sent == actual
        assert "uniqueItems" not in json.dumps(sent)
        Draft202012Validator(sent).validate(wire)
        wrong = translate_ids(diagnosis("review_work", block_id=B2, section_id=S1), aliases)
        assert list(Draft202012Validator(sent).iter_errors(wrong))
        red = translate_ids(diagnosis("review_work", block_id=RED), aliases)
        assert list(Draft202012Validator(sent).iter_errors(red))
        calls.append(request)
        chunk = {"choices": [{"delta": {"content": json.dumps(wire)}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("review_work", context, schema, "citation-wire")
        parsed = Review.model_validate_json(result.content)
        assert parsed.findings[0].block_refs[0].section_id == S2
        assert parsed.findings[0].block_refs[0].block_id == B2
        engine.check_result(parsed, context)
        assert len(calls) == 1
    finally:
        await runtime.aclose()
