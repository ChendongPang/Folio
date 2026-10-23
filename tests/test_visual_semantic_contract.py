"""A grounded difference, not the tone of a sentence, determines visual repair."""

import json

import pytest
from pydantic import ValidationError
from test_visual_review_grounding import (
    CONTEXT,
    SOURCE,
    forbidden,
    graph,
    grounded_material,
    indexed_renderer,  # noqa: F401
)
from test_visual_workflow import material, png

from folio.visuals.review import LiveVisualFinding, LiveVisualReview, VisualReview
from folio.visuals.semantic import SemanticReview, reconcile_semantics
from folio.visuals.workflow import VisualSource, build_visuals


def live_semantic(description="该路径已满足原文", **updates):
    return LiveVisualFinding.model_validate(
        {
            "kind": "semantic",
            "verdict": "violation",
            "source_block_id": "b3",
            "source_quote": SOURCE,
            "from_node": "N_Book",
            "to_node": "Settlement",
            "description": description,
            "difference": {
                "required_behavior": "普通交易应进入结算",
                "actual_behavior": "普通交易已经到达结算",
                "actual_quote": "N_Book --> Settlement",
            },
        }
        | updates
    )


def equivalent(cases, source_blocks):
    return SemanticReview.model_validate(
        {
            "checks": [
                {
                    "case_id": case["case_id"],
                    "result": "equivalent",
                    "reason": "完整原文和实际有向路径表达同一行为",
                    "differences": [],
                    "checked_relation_ids": [r["relation_id"] for r in case.get("relations", [])],
                    "edge_support": [
                        {
                            "edge_id": edge_id,
                            "basis": source_blocks[0].get("origin", "evidence"),
                            "source_block_id": source_blocks[0]["block_id"],
                            "source_quote": source_blocks[0]["text"],
                            "reason": "来源已明确本例中的流转；只用于构造合法审阅响应。",
                        }
                        for edge_id in dict.fromkeys(
                            e["edge_id"] for r in case.get("relations", []) for e in r["edges"]
                        )
                    ],
                }
                for case in cases
            ]
        }
    )


def test_live_contract_cannot_default_a_missing_verdict_or_omit_actual_difference():
    data = live_semantic().model_dump()
    data.pop("verdict")
    with pytest.raises(ValidationError, match="verdict"):
        LiveVisualFinding.model_validate(data)
    with pytest.raises(ValidationError, match="expected and actual"):
        live_semantic(difference=None)
    # Legacy reading is retained, but live responses do not inherit this default.
    assert VisualReview.model_validate({"problems": ["旧审阅记录"]}).problems
    schema = LiveVisualReview.model_json_schema()["$defs"]["LiveVisualFinding"]
    assert {"verdict", "difference"} <= set(schema["required"])


@pytest.mark.usefixtures("indexed_renderer")
async def test_positive_explanation_marked_violation_is_adjudicated_without_redrawing(tmp_path):
    plan, states = grounded_material()
    calls = []

    async def call(task, context, model):
        calls.append((task, model))
        if task == "design_flowchart":
            return VisualSource(lines=["flowchart TD", "N_Book --> Settlement"])
        if model is SemanticReview:
            assert (
                context["source_blocks"] == CONTEXT["source_blocks"]
                or context["source_blocks"][0]["text"] == SOURCE
            )
            assert context["semantic_cases"][0]["finding"]["verdict"] == "violation"
            return equivalent(context["semantic_cases"], context["source_blocks"])
        return LiveVisualReview(findings=[live_semantic()])

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues
    assert [name for name, _ in calls] == ["design_flowchart", "validate_visual", "validate_visual"]
    assert list(tmp_path.glob("visuals/*/semantic-review-1.json"))
    check = json.loads(next(tmp_path.glob("visuals/*/review-analysis-1.json")).read_text())
    assert (
        not check["problems"] and check["dismissed"][-1]["method"] == "independent_semantic_check"
    )


@pytest.mark.usefixtures("indexed_renderer")
async def test_semantic_equivalence_cannot_waive_same_description_forbidden_path(tmp_path):
    plan, states = grounded_material()

    async def call(task, context, model):
        if task == "design_flowchart":
            return VisualSource(
                lines=["flowchart TD", "N_Book --> Settlement", "V_Book --> Settlement"]
            )
        if model is SemanticReview:
            return equivalent(context["semantic_cases"], context["source_blocks"])
        structural = LiveVisualFinding.model_validate(
            forbidden().model_dump() | {"difference": None}
        )
        return LiveVisualReview(
            findings=[live_semantic(description=structural.description), structural]
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert result.issues and forbidden().description in result.issues[0]["reason"]
    assert not list(tmp_path.glob("visuals/*/asset.json"))


async def test_warning_or_stop_may_not_be_reduced_to_stop_even_after_empty_initial_review(
    tmp_path, monkeypatch
):
    plan, states = material()
    states[0].content.blocks[0].text = "超限或预警时，触发警示或终止流程。"
    designs = []

    async def renderer(source, folder):
        image = folder / "image.png"
        png(image)
        path = folder / "source.mmd"
        path.write_text(source)
        return {
            "image_path": str(image),
            "source_path": str(path),
            "width": 1,
            "height": 1,
            "graph_index": {
                "complete": True,
                "nodes": [{"id": n, "label": n} for n in ["Check", "Stop", "Warn"]],
                "edges": [],
            },
        }

    monkeypatch.setattr("folio.visuals.render.render_flowchart", renderer)

    async def call(task, context, model):
        if task == "design_flowchart":
            source = (
                ["flowchart TD", "Check -- 超限/预警 --> Stop"]
                if not designs
                else ["flowchart TD", "Check -- 仅警示 --> Warn", "Check -- 需终止 --> Stop"]
            )
            designs.append(source)
            return VisualSource(lines=source)
        if model is not SemanticReview:
            return LiveVisualReview()
        assert context["semantic_cases"][0]["case_id"] == "outcome_alternatives"
        if len(designs) > 1:
            return equivalent(context["semantic_cases"], context["source_blocks"])
        return SemanticReview.model_validate(
            {
                "checks": [
                    {
                        "case_id": "outcome_alternatives",
                        "result": "actual_difference",
                        "reason": "可选结果被收窄",
                        "differences": [
                            {
                                "source_block_id": "b1",
                                "source_quote": "触发警示或终止流程",
                                "required_behavior": "可以仅警示，也可以按条件终止",
                                "actual_behavior": "所有预警均终止",
                                "actual_quote": "Check -- 超限/预警 --> Stop",
                                "from_node": "Check",
                                "to_node": "Stop",
                                "description": "图中遗漏仅警示的选择，强化为一律终止",
                            }
                        ],
                    }
                ]
            }
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues and len(designs) == 2
    assert "仅警示" in (tmp_path / result["s1"][0]["source_path"]).read_text()


@pytest.mark.usefixtures("indexed_renderer")
@pytest.mark.parametrize("problem", ["missing_case", "invented_quote", "invented_node"])
async def test_incomplete_or_unanchored_adjudication_never_publishes(tmp_path, problem):
    plan, states = grounded_material()

    async def call(task, context, model):
        if task == "design_flowchart":
            return VisualSource(lines=["flowchart TD", "N_Book --> Settlement"])
        if model is not SemanticReview:
            return LiveVisualReview(findings=[live_semantic()])
        if problem == "missing_case":
            return SemanticReview(checks=[])
        return SemanticReview.model_validate(
            {
                "checks": [
                    {
                        "case_id": "semantic_0",
                        "result": "actual_difference",
                        "reason": "候选差异",
                        "differences": [
                            {
                                "source_block_id": "b3",
                                "source_quote": SOURCE,
                                "required_behavior": "原文行为",
                                "actual_behavior": "另一个行为",
                                "actual_quote": "invented"
                                if problem == "invented_quote"
                                else "N_Book --> Settlement",
                                "from_node": "invented" if problem == "invented_node" else "N_Book",
                                "description": "待证实",
                            }
                        ],
                    }
                ]
            }
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert result.issues and not list(tmp_path.glob("visuals/*/asset.json"))


def test_positive_wording_never_overrides_grounded_actual_difference():
    result = SemanticReview.model_validate(
        {
            "checks": [
                {
                    "case_id": "semantic_0",
                    "result": "actual_difference",
                    "reason": "定位到不允许的清算路径",
                    "differences": [
                        {
                            "source_block_id": "b3",
                            "source_quote": "虚拟交易不触发清算核算",
                            "required_behavior": "虚拟交易不进入清算",
                            "actual_behavior": "虚拟交易进入清算",
                            "actual_quote": "V_Book --> Settlement",
                            "from_node": "V_Book",
                            "to_node": "Settlement",
                            "description": "虽然备注写着正确，实际仍存在禁止路径",
                        }
                    ],
                }
            ]
        }
    )
    output = reconcile_semantics(
        result,
        [{"case_id": "semantic_0"}],
        {"problems": [], "unverified": [], "dismissed": []},
        CONTEXT,
        "flowchart TD\nV_Book --> Settlement",
        graph(),
    )
    assert output["problems"] == ["虽然备注写着正确，实际仍存在禁止路径"]


@pytest.mark.usefixtures("indexed_renderer")
async def test_uncertain_semantic_review_stays_unresolved_after_bounded_attempts(tmp_path):
    plan, states = grounded_material()
    designs = 0

    async def call(task, context, model):
        nonlocal designs
        if task == "design_flowchart":
            designs += 1
            return VisualSource(lines=["flowchart TD", "N_Book --> Settlement"])
        if model is SemanticReview:
            return SemanticReview.model_validate(
                {
                    "checks": [
                        {
                            "case_id": "semantic_0",
                            "result": "uncertain",
                            "reason": "无法确认两种表达是否等价",
                            "differences": [],
                        }
                    ]
                }
            )
        return LiveVisualReview(findings=[live_semantic()])

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert designs == 3 and result.issues
    assert "无法确认" in result.issues[0]["reason"]
    assert not list(tmp_path.glob("visuals/*/asset.json"))
