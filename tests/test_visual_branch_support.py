"""The run-03 approval-return/risk-stop branches need positive source support."""

import json

import pytest
from test_visual_topology_review import equivalent, graph
from test_visual_workflow import material, png

from folio.visuals.review import LiveVisualReview
from folio.visuals.semantic import SemanticReview, reconcile_semantics, semantic_cases
from folio.visuals.workflow import VisualSource, build_visuals


def run03_case():
    # Actual run-03 prose promised review and risk interception, never either
    # failure outcome. The graph's invented return/termination must not pass
    # just because an adjudicator lists every relation and says "equivalent".
    context = {
        "source_blocks": [
            {
                "block_id": "b2",
                "origin": "evidence",
                "text": "完成交易录入与内部审批，并与外汇交易中心完成交易后确认。"
                "交易过程中实时联动授信，实现交易全流程风险拦截。",
            }
        ]
    }
    edges = [
        ("TradeEntry", "InternalCheck"),
        ("InternalCheck", "TradeEntry"),
        ("InternalCheck", "RiskPass"),
        ("RiskPass", "Clearing"),
        ("RiskPass", "RiskBlock"),
        ("RiskBlock", "EndRisk"),
    ]
    index = graph(edges)
    source = "flowchart TD\n" + "\n".join(f"{left} --> {right}" for left, right in edges)
    checked = {"problems": [], "unverified": [], "dismissed": []}
    cases = semantic_cases(LiveVisualReview(), checked, context, index)
    return context, index, source, checked, cases


def test_run03_equivalent_with_all_relation_ids_but_no_branch_sources_is_unverified():
    context, index, source, checked, cases = run03_case()
    response = equivalent(cases, context["source_blocks"])
    response["checks"][0].pop("edge_support")  # Replay the old accepted response shape.
    response["checks"][0]["reason"] = "含不通过回退、拦截终止，全部与原文一致。"
    result = reconcile_semantics(
        SemanticReview.model_validate(response), cases, checked, context, source, index
    )
    assert not result["problems"]  # Lack of evidence is not a proven contradiction.
    assert result["unverified"] and not result["dismissed"]
    schema = SemanticReview.model_json_schema()["$defs"]["SemanticDecision"]
    assert "edge_support" in schema["required"]


@pytest.mark.parametrize(
    "bad_support", ["uncertain", "invented_quote", "wrong_origin", "duplicate"]
)
def test_equivalent_never_waives_unknown_or_unanchored_branch_support(bad_support):
    context, index, source, checked, cases = run03_case()
    response = equivalent(cases, context["source_blocks"])
    supports = response["checks"][0]["edge_support"]
    if bad_support == "uncertain":
        supports[0].update(
            basis="uncertain", source_block_id="", source_quote="", reason="未说明失败后去向"
        )
    elif bad_support == "invented_quote":
        supports[0]["source_quote"] = "审批失败退回录入，风险拦截则终止流程。"
    elif bad_support == "wrong_origin":
        supports[0]["basis"] = "model_proposal"  # The actual block is evidence.
    else:
        supports.append(dict(supports[0]))
    result = reconcile_semantics(
        SemanticReview.model_validate(response), cases, checked, context, source, index
    )
    assert result["unverified"] and not result["dismissed"] and not result["problems"]


@pytest.mark.parametrize("origin", ["evidence", "model_proposal"])
async def test_explicitly_sourced_failure_branch_passes_with_its_original_origin(
    tmp_path, monkeypatch, origin
):
    plan, states = material()
    states[0].content.blocks[0].text = "录入后审批，通过则完成；拒绝则退回录入。"
    states[0].content.blocks[0].origin = origin
    states[0].content.blocks[0].evidence_ids = ["ev_approval"] if origin == "evidence" else []
    edges = [("Entry", "Approval"), ("Approval", "Done"), ("Approval", "Entry")]
    calls = []

    async def renderer(source, folder):
        image, path = folder / "image.png", folder / "source.mmd"
        png(image)
        path.write_text(source)
        return {
            "image_path": str(image),
            "source_path": str(path),
            "width": 1,
            "height": 1,
            "graph_index": graph(edges),
        }

    monkeypatch.setattr("folio.visuals.render.render_flowchart", renderer)

    async def call(task, context, model):
        calls.append((task, model))
        if task == "design_flowchart":
            assert "Each edge" in context["visual_scope"]["branch_policy"]
            return VisualSource(lines=["flowchart TD", *(f"{a} --> {b}" for a, b in edges)])
        if model is SemanticReview:
            return SemanticReview.model_validate(
                equivalent(context["semantic_cases"], context["source_blocks"])
            )
        return LiveVisualReview()

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues and result["s1"][0]["origin"] == origin
    assert len(calls) == 3  # Same design + primary review + batched semantic review.
    audit = json.loads(next(tmp_path.glob("visuals/*/semantic-review-1.json")).read_text())
    assert {s["basis"] for s in audit["checks"][0]["edge_support"]} == {origin}


async def test_unsupported_branch_remains_unverified_and_stops_after_three_attempts(
    tmp_path, monkeypatch
):
    plan, states = material()
    context, index, source, _, _ = run03_case()
    states[0].content.blocks[0].text = context["source_blocks"][0]["text"]
    designs = semantics = 0

    async def renderer(source, folder):
        image, path = folder / "image.png", folder / "source.mmd"
        png(image)
        path.write_text(source)
        return {
            "image_path": str(image),
            "source_path": str(path),
            "width": 1,
            "height": 1,
            "graph_index": index,
        }

    monkeypatch.setattr("folio.visuals.render.render_flowchart", renderer)

    async def call(task, request, model):
        nonlocal designs, semantics
        if task == "design_flowchart":
            designs += 1
            if designs > 1:
                assert "Unverified" in request["repair_feedback"]
            return VisualSource(lines=source.splitlines())
        if model is SemanticReview:
            semantics += 1
            response = equivalent(request["semantic_cases"], request["source_blocks"])
            # An aggregate equivalent cannot override this branch's uncertainty.
            response["checks"][0]["edge_support"][0].update(
                basis="uncertain", source_block_id="", source_quote="", reason="未说明失败后去向"
            )
            return SemanticReview.model_validate(response)
        return LiveVisualReview()

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert designs == semantics == 3 and result.issues
    assert "未说明失败后去向" in result.issues[0]["reason"]
    assert not list(tmp_path.glob("visuals/*/asset.json"))
