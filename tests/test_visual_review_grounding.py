"""A hallucinated missing edge cannot conceal a real prohibited branch."""

import asyncio
import json
from copy import deepcopy

import pytest
from test_visual_workflow import material, png

from folio.visuals.review import VisualFinding, VisualReview, assess_review
from folio.visuals.workflow import VisualSource, build_visuals, load_visuals

SOURCE = "虚拟交易不触发清算核算。普通交易完成簿记后进入结算。"
CONTEXT = {"source_blocks": [{"block_id": "b3", "text": SOURCE, "rows": []}]}


def graph(*, virtual=True):
    edges = [("Normal", "N_Book"), ("N_Book", "Settlement"), ("Virtual", "V_Book")]
    if virtual:
        edges.append(("V_Book", "Settlement"))
    return {
        "complete": True,
        "nodes": [
            {"id": node, "label": node}
            for node in ("Normal", "N_Book", "Virtual", "V_Book", "Settlement")
        ],
        "edges": [
            {
                "from_node": left,
                "to_node": right,
                "label": "",
                "type": "arrow_point",
                "stroke": "normal",
            }
            for left, right in edges
        ],
    }


def finding(kind="missing_path", **values):
    return VisualFinding(
        **(
            {
                "kind": kind,
                "source_block_id": "b3",
                "source_quote": SOURCE,
                "from_node": "N_Book",
                "to_node": "Settlement",
                "description": "普通交易缺少结算连接",
            }
            | values
        )
    )


def forbidden():
    return finding("forbidden_path", from_node="V_Book", description="虚拟交易不应进入结算")


def test_existing_normal_edge_is_refuted_but_real_forbidden_virtual_path_is_retained():
    review = VisualReview(findings=[finding(), forbidden()])
    before = review.model_dump()
    checked = assess_review(review, CONTEXT, graph())
    assert checked["problems"] == ["虚拟交易不应进入结算"]
    assert checked["unverified"] == []
    assert checked["dismissed"][0]["finding"]["description"] == "普通交易缺少结算连接"
    assert review.model_dump() == before


@pytest.mark.parametrize("kind,dismissed", [("missing_path", True), ("missing_edge", False)])
def test_transitive_path_does_not_prove_a_direct_edge(kind, dismissed):
    checked = assess_review(
        VisualReview(findings=[finding(kind, from_node="Normal")]), CONTEXT, graph()
    )
    assert bool(checked["dismissed"]) == dismissed
    assert bool(checked["problems"]) != dismissed


@pytest.mark.parametrize("condition,dismissed", [("批准", True), ("拒绝", False)])
def test_direct_edge_conditions_are_not_interchangeable(condition, dismissed):
    index = graph()
    index["edges"][1]["label"] = "批准"
    result = assess_review(
        VisualReview(findings=[finding("missing_edge", condition=condition)]), CONTEXT, index
    )
    assert bool(result["dismissed"]) == dismissed


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "unknown_edge",
        "dotted_edge",
        "unknown_source",
        "invented_quote",
        "conditional_path",
    ],
)
def test_uncertain_graph_or_source_never_silently_passes(mutation):
    index = graph()
    issue = finding()
    if mutation == "incomplete":
        index["complete"] = False
    elif mutation == "unknown_edge":
        index["edges"][1]["type"] = "future_mermaid_shape"
    elif mutation == "dotted_edge":
        index["edges"][1]["stroke"] = "dotted"
    elif mutation == "unknown_source":
        issue.source_block_id = "invented"
    elif mutation == "invented_quote":
        issue.source_quote = "普通交易也禁止清算"
    else:
        issue.condition = "指定业务条件"
    result = assess_review(VisualReview(findings=[issue]), CONTEXT, index)
    assert not result["dismissed"]
    assert result["unverified"] == [issue.description]


def test_legacy_unlocated_problem_remains_unresolved_without_becoming_a_fact():
    result = assess_review(VisualReview(problems=["臆测必须审批"]), CONTEXT, graph())
    assert result == {"problems": [], "unverified": ["臆测必须审批"], "dismissed": []}


def test_unknown_node_ids_do_not_silently_dismiss_a_prohibited_path():
    issue = forbidden().model_copy(update={"from_node": "made_up_id"})
    checked = assess_review(VisualReview(findings=[issue]), CONTEXT, graph())
    assert checked["unverified"] == [issue.description] and not checked["dismissed"]


def test_edge_direction_and_missing_node_are_checked_exactly():
    result = assess_review(
        VisualReview(findings=[finding("missing_edge", from_node="Settlement", to_node="N_Book")]),
        CONTEXT,
        graph(),
    )
    assert result["problems"] and not result["dismissed"]
    result = assess_review(VisualReview(findings=[finding("missing_node")]), CONTEXT, graph())
    assert result["dismissed"] and not result["problems"]


@pytest.fixture
def indexed_renderer(monkeypatch):
    calls = []

    async def render(source, folder):
        calls.append(source)
        image = folder / "image.png"
        png(image)
        code = folder / "source.mmd"
        code.write_text(source)
        return {
            "image_path": str(image),
            "source_path": str(code),
            "width": 1,
            "height": 1,
            "graph_index": graph(virtual="V_Book --> Settlement" in source),
        }

    monkeypatch.setattr("folio.visuals.render.render_flowchart", render)
    return calls


def grounded_material():
    plan, states = material()
    states[0].content.blocks[0].block_id = "b3"
    states[0].content.blocks[0].text = SOURCE
    visual = states[0].content.visuals[0]
    visual.source_block_ids = ["b3"]
    visual.after_block_id = "b3"
    return plan, states


async def test_prohibited_path_cannot_be_waived_by_later_empty_review_or_resume(
    tmp_path, indexed_renderer
):
    plan, states = grounded_material()
    calls = []

    async def call(task, context, model):
        calls.append((task, deepcopy(context)))
        if task == "validate_visual":
            assert context["graph_index"] == graph()
            return VisualReview(findings=[finding(), forbidden()] if len(calls) == 2 else [])
        return VisualSource(
            lines=["flowchart TD", "N_Book --> Settlement", "V_Book --> Settlement"]
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(calls) == 6
    assert len(result.issues) == 1
    assert "虚拟交易不应进入结算" in result.issues[0]["reason"]
    assert "普通交易缺少结算连接" not in result.issues[0]["reason"]
    assert "普通交易缺少结算连接" not in calls[2][1]["repair_feedback"]
    assert result["s1"][0]["status"] == "unresolved"
    assert "image_path" not in result["s1"][0]
    assert load_visuals(tmp_path, states).issues == result.issues
    # Known source-grounded prohibitions survive restart without trusting a blank review.
    resumed = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(calls) == 12 and resumed.issues == result.issues

    async def fixed(task, context, model):
        return (
            VisualReview()
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "N_Book --> Settlement"])
        )

    resolved = await build_visuals(plan, states, tmp_path, fixed, lambda _: None)
    assert not resolved.issues and "image_path" in resolved["s1"][0]
    assert not load_visuals(tmp_path, states).issues


async def test_failed_figure_does_not_cancel_concurrent_healthy_figure(tmp_path, indexed_renderer):
    plan, states = grounded_material()
    states[0].content.visuals.append(
        states[0]
        .content.visuals[0]
        .model_copy(update={"figure_id": "healthy", "caption": "成功图"})
    )
    completed = []

    async def call(task, context, model):
        if context["visual"]["figure_id"] == "v1":
            raise ValueError("当前配图源文件无效")
        await asyncio.sleep(0)
        completed.append(task)
        return (
            VisualReview()
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "N_Book --> Settlement"])
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert completed == ["design_flowchart", "validate_visual"]
    assert len(result["s1"]) == 2 and len(result.issues) == 1
    assert result.issues[0]["figure_id"] == "v1"
    assert "image_path" in result["s1"][1]


async def test_task_cancellation_is_not_converted_to_a_failed_figure(tmp_path, indexed_renderer):
    plan, states = grounded_material()

    async def cancelled(*args):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await build_visuals(plan, states, tmp_path, cancelled, lambda _: None)
    assert not (tmp_path / "visuals.json").exists()


async def test_unresolved_manifest_cannot_smuggle_an_unreviewed_image(tmp_path, indexed_renderer):
    plan, states = grounded_material()

    async def failed(*args):
        raise ValueError("明确失败")

    await build_visuals(plan, states, tmp_path, failed, lambda _: None)
    path = tmp_path / "visuals.json"
    data = json.loads(path.read_text())
    data["sections"]["s1"][0]["image_path"] = "unreviewed.png"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="has not passed review"):
        load_visuals(tmp_path, states)


async def test_real_mermaid_parser_reports_normal_and_virtual_edges(tmp_path):
    from folio.visuals.render import render_flowchart

    rendered = await render_flowchart(
        'flowchart TD\nNormal["普通交易"] --> N_Book["簿记"]\n'
        'Virtual["虚拟交易"] --> V_Book["簿记"]\n'
        'V_Book --> Settlement["结算处理"]\nN_Book --> Settlement',
        tmp_path,
    )
    checked = assess_review(
        VisualReview(findings=[finding(), forbidden()]), CONTEXT, rendered["graph_index"]
    )
    assert checked["problems"] == ["虚拟交易不应进入结算"]
    assert len(checked["dismissed"]) == 1 and not checked["unverified"]
