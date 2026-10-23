"""Recursive validation pages carry their own complete sources, never sibling payloads."""

import json
from copy import deepcopy

import pytest

from folio.models import JobInput, LocalReview
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


def source(identifier, size):
    return {
        "evidence_id": identifier,
        "source_id": "original-document",
        "source_version": "v1",
        "locator": identifier,
        "text": identifier + "完整原文：" + "规" * size + "。末尾否定约束不得省略。",
    }


def validation_context(count=4):
    blocks = [
        {
            "block_id": f"b{i}",
            "kind": "paragraph",
            "text": f"第{i}条完整正文" + ("，另有未抽取断言。" if i == count - 1 else "。"),
            "evidence_ids": [f"e{i}", "shared"],
        }
        for i in range(count)
    ]
    return {
        "section": {"section_id": "s", "title": "业务约束"},
        "content": {
            "blocks": blocks,
            "visuals": [
                {
                    "figure_id": "cross_block_flow",
                    "kind": "flowchart",
                    "caption": "业务流程",
                    "description": "依次展示完整业务步骤",
                    "source_block_ids": ["b0", f"b{count - 1}"],
                    "after_block_id": f"b{count - 1}",
                }
            ],
        },
        "extraction": {
            "claims": [
                {
                    "claim_id": f"c{i}",
                    "statement": f"第{i}条已抽取断言",
                    "block_ids": [f"b{i}"],
                    "evidence_ids": [f"e{i}", f"claim_source{i}"],
                }
                for i in range(count)
            ]
        },
        "evidence": [source(f"e{i}", 2000) for i in range(count)]
        + [source(f"claim_source{i}", 350) for i in range(count)]
        + [source("shared", 80), source("project_a", 300), source("project_b", 400)],
        "required_project_evidence_ids": ["project_a", "project_b"],
    }


class BoundedValidator(DemoRuntime):
    def __init__(self, *, provider_rejection=False, limit=4400):
        self.provider_rejection = provider_rejection
        self.limit = limit
        self.sent = []
        self.rejected = []

    @staticmethod
    def size(context):
        return len(json.dumps(context, ensure_ascii=False))

    async def measure(self, task, context, schema, char_limit):
        assert task == "validate"
        size = self.size(context)
        return {"characters": size, "fits": self.provider_rejection or size <= self.limit}

    async def complete(self, task, context, schema, session):
        assert task == "validate"
        if self.size(context) > self.limit:
            self.rejected.append(deepcopy(context))
            raise ModelCallFailed("Server input limit reached", "context_limit")
        self.sent.append(deepcopy(context))
        findings = []
        if context.get("validation_scope") == "body":
            for block in context["content"]["blocks"]:
                if "未抽取断言" in block.get("text", ""):
                    findings.append(
                        {
                            "kind": "unsupported",
                            "section_ids": ["s"],
                            "key": "unextracted_" + block["block_id"],
                            "description": "本块未抽取的断言仍须核对。",
                            "block_refs": [{"section_id": "s", "block_id": block["block_id"]}],
                        }
                    )
        return RuntimeResult(
            json.dumps(
                {
                    "verdicts": [
                        {"claim_id": claim["claim_id"], "supported": True, "reason": "逐条核对"}
                        for claim in context["extraction"]["claims"]
                    ],
                    "findings": findings,
                }
            )
        )


def engine_for(store, inputs, runtime):
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    return engine


@pytest.mark.parametrize("provider_rejection", [False, True])
async def test_recursive_claim_and_body_pages_keep_full_own_sources_and_distinct_scopes(
    store, inputs, provider_rejection
):
    context = validation_context()
    original = deepcopy(context)
    originals = {item["evidence_id"]: item for item in context["evidence"]}
    runtime = BoundedValidator(provider_rejection=provider_rejection)
    engine = engine_for(store, inputs, runtime)
    result = await engine.call_bounded("validate", context, LocalReview)
    assert {verdict.claim_id for verdict in result.verdicts} == {f"c{i}" for i in range(4)}
    assert [finding.key for finding in result.findings] == ["unextracted_b3"]
    assert bool(runtime.rejected) == provider_rejection
    claims = [page for page in runtime.sent if page["validation_scope"] == "claims"]
    bodies = [page for page in runtime.sent if page["validation_scope"] == "body"]
    assert len(claims) == len(bodies) == 4
    for page in claims + bodies:
        assert len(page["content"]["blocks"]) == 1
        block = page["content"]["blocks"][0]
        expected = set(block["evidence_ids"])
        for claim in page["extraction"]["claims"]:
            assert claim["block_ids"] == [block["block_id"]]
            expected.update(claim["evidence_ids"])
        assert {item["evidence_id"] for item in page["evidence"]} == expected
        assert all(item == originals[item["evidence_id"]] for item in page["evidence"])
        assert page["required_project_evidence_ids"] == []
        assert page["content"].get("visuals", []) == []
    coverage = [page for page in runtime.sent if page["validation_scope"] == "coverage"]
    assert len(coverage) == 2
    for page in coverage:
        assert page["content"] == original["content"]
        assert len(page["required_project_evidence_ids"]) == 1
        identifier = page["required_project_evidence_ids"][0]
        assert page["evidence"] == [originals[identifier]]
        assert page["extraction"]["claims"] == []
    visual = [page for page in runtime.sent if page["validation_scope"] == "visual_plan"]
    assert len(visual) == 1
    assert visual[0]["content"] == {"blocks": [], "visuals": original["content"]["visuals"]}
    assert visual[0]["evidence"] == []
    assert visual[0]["required_project_evidence_ids"] == []
    assert context == original
    calls = len(runtime.sent) + len(runtime.rejected)
    assert await engine.call_bounded("validate", context, LocalReview) == result
    assert len(runtime.sent) + len(runtime.rejected) == calls


@pytest.mark.parametrize("scope", ["claims", "body"])
async def test_recursive_scoped_pages_retain_required_project_originals(store, inputs, scope):
    context = validation_context(2)
    context["validation_scope"] = scope
    context["required_project_evidence_ids"] = ["project_a"]
    if scope == "body":
        context["extraction"]["claims"] = []
    original = deepcopy(context)
    runtime = BoundedValidator()
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert len(runtime.sent) == 2
    for page in runtime.sent:
        refs = {ref for block in page["content"]["blocks"] for ref in block["evidence_ids"]}
        refs.update(ref for claim in page["extraction"]["claims"] for ref in claim["evidence_ids"])
        refs.add("project_a")
        assert {item["evidence_id"] for item in page["evidence"]} == refs
        assert page["required_project_evidence_ids"] == ["project_a"]
        assert all(item in original["evidence"] for item in page["evidence"])
    assert context == original


async def test_full_validation_entry_preserves_claim_only_sources_before_partitioning(
    store, inputs
):
    context = validation_context(2)
    original = deepcopy(context)
    runtime = BoundedValidator()
    engine = engine_for(store, inputs, runtime)
    result = await engine.call("validate", context, LocalReview)
    assert {verdict.claim_id for verdict in result.verdicts} == {"c0", "c1"}
    claim_pages = [page for page in runtime.sent if page["validation_scope"] == "claims"]
    assert len(claim_pages) == 2
    source_texts = {item["evidence_id"]: item["text"] for item in original["evidence"]}
    for page in claim_pages:
        claim = page["extraction"]["claims"][0]
        received = {item["evidence_id"]: item["text"] for item in page["evidence"]}
        assert set(claim["evidence_ids"]) <= received.keys()
        assert all(text == source_texts[identifier] for identifier, text in received.items())
        assert any(identifier.startswith("claim_source") for identifier in received)
    archives = (store.root / "jobs" / engine.job_id / "contexts" / "sources").glob("*.json")
    assert any(json.loads(path.read_text())["context"] == original for path in archives)
    assert context == original


async def test_oversized_required_original_is_not_truncated_to_force_validation(store, inputs):
    context = validation_context(2)
    context["validation_scope"] = "body"
    context["extraction"]["claims"] = []
    context["required_project_evidence_ids"] = ["too_large"]
    context["evidence"].append(source("too_large", 5000))
    before = deepcopy(context)
    runtime = BoundedValidator()
    with pytest.raises(ValueError, match="smallest safe request exceeds capacity"):
        await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert runtime.sent == [] and context == before


async def test_unanchored_historical_claim_keeps_all_parent_prose_and_sources(store, inputs):
    context = validation_context(2)
    context["validation_scope"] = "claims"
    context["required_project_evidence_ids"] = []
    context["extraction"]["claims"][0]["block_ids"] = []
    runtime = BoundedValidator(limit=6800)
    # Force output partitioning while each scoped child can fit in full.
    context["extraction"]["claims"] *= 13
    context["extraction"]["claims"] = [
        claim | {"claim_id": f"claim_{index}"}
        for index, claim in enumerate(context["extraction"]["claims"])
    ]
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert runtime.sent
    for page in runtime.sent:
        if any(not claim["block_ids"] for claim in page["extraction"]["claims"]):
            assert page["content"]["blocks"] == context["content"]["blocks"]
            assert {"e0", "e1", "shared"} <= {item["evidence_id"] for item in page["evidence"]}
