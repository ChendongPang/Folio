"""Exercise the actual requests; these tests do not predict semantic model scores."""

import json
from copy import deepcopy

import httpx
import pytest
from decision_wire import payload as decision_payload
from decision_wire import transport as decision_transport
from jsonschema import Draft202012Validator

from folio.model_settings import ModelSettingsInput, ModelSettingsStore, probe_fingerprint
from folio.models import Extraction, LocalReview
from folio.orchestration.context import ContextSelection
from folio.orchestration.work import GapCompletion
from folio.runtime.business_transport import translate_ids, unpack_rows
from folio.runtime.llm import LLMRuntime


@pytest.fixture
async def runtime(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    result = LLMRuntime(settings.path)
    yield result
    await result.aclose()


def report_context():
    return {
        "purpose": {
            "section": "业务报表",
            "instruction": "列出报表名称、说明、输出方式与适用范围。",
        },
        "catalog": [
            {
                "context_ref": "ctx_partial",
                "kind": "evidence",
                "value": {
                    "evidence_id": "ev_report",
                    "text": "自动生成账务核对报告，支持导出、打印与存档。仅用于账务核对。",
                },
            }
        ],
    }


async def test_primary_selection_gets_partial_support_contract_without_losing_conditions(runtime):
    original = report_context()
    before = deepcopy(original)
    messages, _, _, _, refs = runtime.request_parts(
        "select_context", original, ContextSelection.model_json_schema()
    )
    instructions = messages[0]["content"]
    assert "any specific item, field, rule, or constraint" in instructions
    assert "a single source need not answer every question" in instructions
    assert "business object, product, phase, and conditions" in instructions
    assert (
        "Similar titles, shared task background, or vague mentions are not substantive support"
        in instructions
    )
    assert translate_ids(unpack_rows(json.loads(messages[1]["content"])), refs) == original
    assert original == before


async def test_jev_receives_partial_support_rule_but_keeps_strict_probability_and_full_text(
    tmp_path, monkeypatch
):
    config = {
        "version": 1,
        "base_url": "http://primary.invalid/v1",
        "model": "test",
        "no_auth": True,
        "provider_mode": "custom",
        "decision_enabled": True,
        "decision_model": "jev",
        "decision_base_url": "https://judge.test/v1", "decision_api_key": "mock-secret",
    }
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(config))
    sent = []
    source = report_context()["catalog"][0]
    context = report_context()
    context["catalog"] = [
        source,
        {
            "context_ref": "ctx_uncertain",
            "kind": "evidence",
            "value": {"text": "资产信息可自动导入。适用范围：资产静态信息。"},
        },
        {
            "context_ref": "ctx_unrelated",
            "kind": "evidence",
            "value": {"text": "报表栏目仅为目录标题，无业务内容。"},
        },
    ]
    original = deepcopy(context)
    scores = {"ctx_partial": 0.91, "ctx_uncertain": 0.8, "ctx_unrelated": 0.1}

    def handler(request):
        assert request.url.host == "judge.test", "No LLM fallback or external service"
        payload = decision_payload(request)
        sent.append(payload)
        assert (
            len(json.dumps({k: payload[k] for k in ("state", "questions")}, ensure_ascii=False))
            <= 32000
        )
        answers = {}
        for index, item in enumerate(payload["state"]["catalog"]):
            instructions = payload["questions"][f"item_{index}"]["instructions"]
            assert (
                "substantively support one specific item, field, rule or constraint" in instructions
            )
            assert "need not answer every question" in instructions
            assert (
                "Evaluate the original business object, product, stage and conditions"
                in instructions
            )
            assert "strictly greater than 0.8" in instructions
            assert item == next(
                x for x in original["catalog"] if x["context_ref"] == item["context_ref"]
            )
            answers[f"item_{index}"] = {"type": "noul", "noul": scores[item["context_ref"]]}
        return httpx.Response(200, json={"answers": answers, "model": "jev-mock"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: client_type(*a, **kw, transport=decision_transport(handler)),
    )
    runtime = LLMRuntime(path)
    try:
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "partial-support"
        )
    finally:
        await runtime.aclose()
    assert sent
    assert ContextSelection.model_validate_json(result.content).selected_refs == ["ctx_partial"]
    assert context == original
    assert {row["context_ref"]: row["accepted"] for row in result.usage["decision_relevance"]} == {
        "ctx_partial": True,
        "ctx_uncertain": False,
        "ctx_unrelated": False,
    }


async def test_gap_contract_keeps_business_lists_and_real_metadata_separate_in_wire(runtime):
    context = {
        "gaps": [
            {"gap_id": "gap_reports", "description": "补充业务报表清单及输出字段"},
            {"gap_id": "gap_person", "description": "缺少真实报表审批人姓名及批准日期"},
        ],
        "sections": [{"section_id": "section_report", "title": "业务报表"}],
    }
    before = deepcopy(context)
    messages, schema, _, _, refs = runtime.request_parts(
        "complete_gaps", context, GapCompletion.model_json_schema()
    )
    prompt = messages[0]["content"]
    assert (
        'do not classify based on words such as "supplement," "list," or "improve the document."'
        in prompt
    )
    assert "rather than merely repeating that the list is needed and setting record_only" in prompt
    assert (
        "without inventing real report numbers, regulatory obligations, or external document names"
        in prompt
    )
    description = schema["$defs"]["GapProposal"]["properties"]["disposition"]["description"]
    assert "business inventory" in description and "actual regulatory sources" in description
    assert translate_ids(unpack_rows(json.loads(messages[1]["content"])), refs) == context
    canonical = {
        "proposals": [
            {
                "gap_id": "gap_reports",
                "section_ids": ["section_report"],
                "content": "拟设账务核对报告，展示核对日期、差异笔数与差异金额，并支持按核对日期导出。",
                "disposition": "body",
            },
            {
                "gap_id": "gap_person",
                "section_ids": ["section_report"],
                "content": "真实报表审批人姓名及批准日期未提供，须由项目人员填写，不推测具体人名或日期。",
                "disposition": "record_only",
            },
        ]
    }
    wire = translate_ids(canonical, {v: k for k, v in refs.items()})
    Draft202012Validator(schema).validate(wire)
    restored = GapCompletion.model_validate(translate_ids(wire, refs))
    assert restored.model_dump() == canonical
    assert context == before


async def test_validation_distinguishes_planned_visual_from_real_content_omission(runtime):
    context = {
        "content": {
            "blocks": [{"block_id": "steps", "text": "交易员提交后，由审核员复核。"}],
            "visuals": [
                {
                    "figure_id": "f",
                    "kind": "flowchart",
                    "caption": "审批流程",
                    "source_block_ids": ["steps"],
                    "after_block_id": "steps",
                }
            ],
        }
    }
    messages, _, _, _, refs = runtime.request_parts(
        "validate", context, LocalReview.model_json_schema()
    )
    prompt = messages[0]["content"]
    assert "Valid visuals requests schedule later illustration work" in prompt
    assert 'absence of a phrase such as "see Figure X"' in prompt
    assert "actual missing business steps" in prompt
    assert "required but unplanned figure kinds" in prompt
    assert "nonexistent source_block_ids or after_block_id" in prompt
    assert translate_ids(unpack_rows(json.loads(messages[1]["content"])), refs) == context


async def test_extraction_contract_preserves_cooperating_roles_and_specific_conditions(runtime):
    messages, _, _, _, _ = runtime.request_parts("extract", {}, Extraction.model_json_schema())
    prompt = messages[0]["content"]
    assert "same specific business object, attribute, and applicability conditions" in prompt
    assert (
        "Several objects or roles collaborating on one activity do not imply that one subject can have only one value"
        in prompt
    )
    assert "Front office submits; middle office reviews" in prompt
    assert (
        'rather than become two single facts with "approval responsibility=front office/middle office."'
        in prompt
    )
    assert "Use multiple or unspecified when uniqueness is not explicit" in prompt
