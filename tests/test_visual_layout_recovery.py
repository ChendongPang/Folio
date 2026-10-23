"""Compact layout and grounded diagnoses preserve business flow and hard limits."""

import json
from pathlib import Path

import pytest
from test_visual_renderers import browser_ready, mermaid_ready  # noqa: F401
from test_visual_review_grounding import (
    CONTEXT,
    finding,
    forbidden,
    graph,
    grounded_material,
    indexed_renderer,  # noqa: F401
)

from folio.visuals.render import (
    MAX_HEIGHT,
    MAX_PIXELS,
    MAX_WIDTH,
    VisualRenderError,
    render_flowchart,
)
from folio.visuals.review import VisualReview, assess_review
from folio.visuals.workflow import VisualSource, build_visuals


def test_an_annotation_cannot_hide_an_existing_solid_path_or_a_prohibited_path():
    index = graph()
    index["edges"].append(
        {
            "from_node": "Normal",
            "to_node": "Virtual",
            "label": "独立状态说明",
            "type": "arrow_point",
            "stroke": "dotted",
        }
    )
    result = assess_review(VisualReview(findings=[finding(), forbidden()]), CONTEXT, index)
    assert result["problems"] == [forbidden().description]
    assert result["unverified"] == []
    assert result["dismissed"][0]["finding"]["description"] == finding().description


def test_unknown_link_still_prevents_claiming_a_forbidden_path_is_absent():
    index = graph(virtual=False)
    index["edges"].append(
        {
            "from_node": "V_Book",
            "to_node": "Settlement",
            "label": "",
            "type": "unknown_arrow",
            "stroke": "normal",
        }
    )
    result = assess_review(VisualReview(findings=[forbidden()]), CONTEXT, index)
    assert result["unverified"] == [forbidden().description]
    assert not result["dismissed"]


@pytest.mark.parametrize("verdict", ["satisfied", "advisory"])
def test_positive_semantic_commentary_is_audited_without_becoming_a_repair(verdict):
    satisfied = finding(
        "semantic",
        verdict=verdict,
        description="图中簿记后进入结算的顺序已正确表达，无问题。",
    )
    result = assess_review(VisualReview(findings=[satisfied, forbidden()]), CONTEXT, graph())
    assert result["problems"] == [forbidden().description]
    assert result["dismissed"][0]["finding"]["verdict"] == verdict


@pytest.mark.parametrize("verdict", ["satisfied", "advisory"])
@pytest.mark.parametrize("kind", ["missing_node", "missing_edge", "forbidden_path"])
def test_classification_cannot_waive_a_real_structural_business_violation(kind, verdict):
    issue = (
        forbidden().model_copy(update={"verdict": verdict})
        if kind == "forbidden_path"
        else finding(
            kind, verdict=verdict, from_node="Absent" if kind == "missing_node" else "Settlement"
        )
    )
    result = assess_review(VisualReview(findings=[issue]), CONTEXT, graph())
    assert result["problems"] == [issue.description]
    assert not result["dismissed"]


def test_unanchored_or_legacy_semantic_diagnoses_are_not_silently_accepted():
    legacy = finding("semantic", description="冻结状态仍可继续推进，违反来源。")
    unanchored = finding("semantic", verdict="satisfied", from_node="Unknown", to_node="Unknown2")
    result = assess_review(VisualReview(findings=[legacy, unanchored]), CONTEXT, graph())
    assert result["problems"] == [legacy.description]
    assert result["unverified"] == [unanchored.description]
    assert not result["dismissed"]


@pytest.mark.usefixtures("browser_ready", "mermaid_ready")
async def test_tall_flow_reduces_spacing_without_omitting_nodes_or_shrinking_type(tmp_path):
    source = "flowchart TD\n" + "\n".join(
        f"Step{i}[业务步骤{i}] --> Step{i + 1}[业务步骤{i + 1}]" for i in range(39)
    )
    result = await render_flowchart(source, tmp_path)
    assert result["layout"]["rank_spacing"] < 45
    assert result["layout"]["font_size"] == 18
    assert result["width"] <= MAX_WIDTH and result["height"] <= MAX_HEIGHT
    assert result["width"] * result["height"] <= MAX_PIXELS
    assert Path(result["source_path"]).read_text() == source
    index = result["graph_index"]
    assert {node["id"] for node in index["nodes"]} == {f"Step{i}" for i in range(40)}
    assert {(edge["from_node"], edge["to_node"]) for edge in index["edges"]} == {
        (f"Step{i}", f"Step{i + 1}") for i in range(39)
    }


@pytest.mark.usefixtures("browser_ready", "mermaid_ready")
async def test_unfit_flow_still_fails_after_bounded_local_relayout(tmp_path):
    source = "flowchart TD\n" + "\n".join(
        f"A{i}[业务步骤{i}] --> A{i + 1}[业务步骤{i + 1}]" for i in range(119)
    )
    with pytest.raises(VisualRenderError, match="dimensions") as failure:
        await render_flowchart(source, tmp_path)
    assert failure.value.code == "too_large"
    assert not list(tmp_path.iterdir())


@pytest.mark.usefixtures("indexed_renderer")
@pytest.mark.parametrize("old_version", [1, 5])
async def test_preclassification_forbidden_constraint_survives_contract_upgrade(tmp_path, old_version):
    plan, states = grounded_material()

    async def call(task, context, model):
        return (
            VisualReview(findings=[forbidden()])
            if task == "validate_visual"
            else VisualSource(
                lines=["flowchart TD", "N_Book --> Settlement", "V_Book --> Settlement"]
            )
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(result.issues) == 1
    path = next(tmp_path.glob("visuals/*/review-constraints.json"))
    saved = json.loads(path.read_text())
    saved["contract"] = {"version": old_version, "instruction": "旧审阅契约"}
    for issue in saved["findings"]:
        issue.pop("verdict")
    path.write_text(json.dumps(saved))

    async def empty_review(task, context, model):
        return VisualReview() if task == "validate_visual" else await call(task, context, model)

    resumed = await build_visuals(plan, states, tmp_path, empty_review, lambda _: None)
    assert len(resumed.issues) == 1
    assert forbidden().description in resumed.issues[0]["reason"]
    assert json.loads(path.read_text())["contract"]["version"] == 6
