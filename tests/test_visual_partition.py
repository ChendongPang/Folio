"""Partitioned body validation keeps figure coverage without rereading the whole section."""

import json
from copy import deepcopy

import pytest
from test_request_context import engine_for

from folio.models import LocalReview
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


def visual(identifier, sources, after=None):
    return {
        "figure_id": identifier,
        "kind": "flowchart",
        "caption": "审批流程",
        "description": "展示本节审批步骤",
        "source_block_ids": sources,
        "after_block_id": after or sources[-1],
    }


def review_context():
    return {
        "section": {"section_id": "s1", "instruction": "说明审批并提供流程图"},
        "content": {
            "blocks": [
                {"block_id": f"b{i}", "text": f"完整业务步骤{i}", "evidence_ids": []}
                for i in range(10)
            ],
            "visuals": [
                visual("local", ["b0"]),
                visual("cross", ["b0", "b9"]),
                visual("tail", ["b9"]),
            ],
        },
        "evidence": [],
        "extraction": {"claims": []},
        "required_project_evidence_ids": [],
    }


class PartitionReviewer(DemoRuntime):
    def __init__(self, *, whole_section=False, missing=False):
        self.calls = []
        self.whole_section = whole_section
        self.missing = missing

    async def measure(self, task, context, schema, char_limit):
        scope = context.get("validation_scope")
        blocks = context["content"]["blocks"]
        size = sum(len(b.get("rows", [b])) for b in blocks)
        fits = self.whole_section or scope == "visual_plan" or scope == "body" and size <= 2
        return {"characters": size, "fits": fits}

    async def complete(self, task, context, schema, session):
        assert task == "validate"
        self.calls.append(deepcopy(context))
        findings = []
        if context.get("validation_scope") == "visual_plan":
            assert context["content"]["blocks"] == []
            assert context["evidence"] == []
            assert context["extraction"]["claims"] == []
            assert "intentionally omitted: blocks=[] does not mean the document lacks body text" in context["validation_instruction"]
            if self.missing:
                findings.append(
                    {
                        "kind": "coverage",
                        "section_ids": ["s1"],
                        "key": "missing-approval-flow",
                        "description": "本节明确要求审批流程图，但配图清单为空。",
                    }
                )
        return RuntimeResult(json.dumps({"verdicts": [], "findings": findings}))


async def test_partitioned_validation_preserves_all_figures_and_scopes_each_body_page(
    store, inputs
):
    context = review_context()
    before = deepcopy(context)
    runtime = PartitionReviewer()
    engine = engine_for(store, inputs, runtime)
    result = await engine.call_bounded("validate", context, LocalReview)
    coverage = [c for c in runtime.calls if c["validation_scope"] == "visual_plan"]
    assert len(coverage) == 1
    assert coverage[0]["content"]["visuals"] == context["content"]["visuals"]
    pages = [c for c in runtime.calls if c["validation_scope"] == "body"]
    assert len(pages) == 5
    assert {b["block_id"] for c in pages for b in c["content"]["blocks"]} == {
        f"b{i}" for i in range(10)
    }
    for page in pages:
        ids = {b["block_id"] for b in page["content"]["blocks"]}
        for figure in page["content"]["visuals"]:
            assert set(figure["source_block_ids"]) <= ids
            assert figure["after_block_id"] in ids
    # The cross-page figure's complete specification is checked once, not sent
    # into arbitrary body pages that cannot establish its business relationships.
    assert {v["figure_id"] for c in pages for v in c["content"]["visuals"]} == {"local", "tail"}
    assert context == before
    count = len(runtime.calls)
    restored = await engine.call_bounded("validate", context, LocalReview)
    assert restored == result and len(runtime.calls) == count


async def test_empty_figure_list_still_gets_explicit_requirement_coverage(store, inputs):
    context = review_context()
    context["content"]["visuals"] = []
    runtime = PartitionReviewer(missing=True)
    result = await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert len(result.findings) == 1
    assert result.findings[0].key == "missing-approval-flow"
    assert sum(c["validation_scope"] == "visual_plan" for c in runtime.calls) == 1


@pytest.mark.parametrize("has_figures", [True, False])
async def test_whole_section_validation_does_not_add_a_figure_planning_call(
    store, inputs, has_figures
):
    context = review_context()
    if not has_figures:
        context["content"]["visuals"] = []
    runtime = PartitionReviewer(whole_section=True)
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    assert len(runtime.calls) == 1
    assert runtime.calls[0].get("validation_scope") is None
    assert runtime.calls[0]["content"]["visuals"] == context["content"]["visuals"]


async def test_partial_table_rows_never_impersonate_a_figures_full_source(store, inputs):
    context = review_context()
    context["content"]["blocks"] = [
        {
            "block_id": "table",
            "kind": "table",
            "rows": [[f"步骤{i}"] for i in range(5)],
            "evidence_ids": [],
        }
    ]
    context["content"]["visuals"] = [visual("flow", ["table"])]
    runtime = PartitionReviewer()
    await engine_for(store, inputs, runtime).call_bounded("validate", context, LocalReview)
    pages = [c for c in runtime.calls if c["validation_scope"] == "body"]
    assert all(c["content"]["visuals"] == [] for c in pages)
    assert [row for c in pages for row in c["content"]["blocks"][0]["rows"]] == [
        [f"步骤{i}"] for i in range(5)
    ]
    coverage = [c for c in runtime.calls if c["validation_scope"] == "visual_plan"]
    assert len(coverage) == 1 and coverage[0]["content"]["visuals"][0]["figure_id"] == "flow"
