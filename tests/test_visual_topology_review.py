import json

import pytest
from test_visual_workflow import material, png

from folio.visuals.review import LiveVisualFinding, LiveVisualReview
from folio.visuals.semantic import SemanticReview, reconcile_semantics, semantic_cases
from folio.visuals.topology import topology_relations
from folio.visuals.workflow import VisualSource, build_visuals


def graph(edges):
    return {
        "complete": True,
        "nodes": [
            {"id": node, "label": node} for node in sorted({n for pair in edges for n in pair})
        ],
        "edges": [
            {
                "from_node": left,
                "to_node": right,
                "type": "arrow_point",
                "stroke": "normal",
                "label": "",
            }
            for left, right in edges
        ],
    }


def equivalent(cases, source_blocks):
    return {
        "checks": [
            {
                "case_id": case["case_id"],
                "result": "equivalent",
                "reason": "按完整原文逐一核对身份、状态和条件后等价。",
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


def test_graph_forks_merges_and_cycles_are_complete_stable_review_locations_not_errors():
    index = graph([("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("D", "A")])
    relations = topology_relations(index)
    assert {r["kind"] for r in relations} == {"branch", "merge", "cycle"}
    assert next(r for r in relations if r["kind"] == "branch")["nodes"] == ["A"]
    assert next(r for r in relations if r["kind"] == "merge")["nodes"] == ["D"]
    assert len(next(r for r in relations if r["kind"] == "cycle")["edges"]) == 5
    assert relations == topology_relations(index | {"edges": list(reversed(index["edges"]))})
    assert topology_relations(graph([("A", "B"), ("B", "C")])) == []
    annotation = graph([("A", "B"), ("A", "C")])
    annotation["edges"][1]["stroke"] = "dotted"
    assert topology_relations(annotation) == []


def test_uncovered_relations_cannot_be_accepted_as_equivalent_and_paths_must_be_real():
    index = graph([("Start", "Check"), ("Start", "Match"), ("Check", "Clear"), ("Match", "Clear")])
    source = "flowchart TD\nStart --> Check\nStart --> Match\nCheck --> Clear\nMatch --> Clear"
    context = {"source_blocks": [{"block_id": "b1", "text": "清算前必须通过校验。"}]}
    checked = {"problems": [], "unverified": [], "dismissed": []}
    cases = semantic_cases(LiveVisualReview(), checked, context, index)
    response = equivalent(cases, context["source_blocks"])
    response["checks"][0]["checked_relation_ids"].pop()
    result = reconcile_semantics(
        SemanticReview.model_validate(response), cases, checked, context, source, index
    )
    assert result["unverified"] and not result["dismissed"]
    response = equivalent(cases, context["source_blocks"])
    response["checks"][0].update(
        result="actual_difference",
        differences=[
            {
                "source_block_id": "b1",
                "source_quote": "清算前必须通过校验",
                "required_behavior": "先校验",
                "actual_behavior": "跳过校验",
                "actual_quote": "Match --> Clear",
                "from_node": "Match",
                "to_node": "Clear",
                "actual_path": ["Match", "Check", "Clear"],
                "description": "所声称的路径必须实际存在",
            }
        ],
    )
    with pytest.raises(ValueError, match="real directed path"):
        reconcile_semantics(
            SemanticReview.model_validate(response), cases, checked, context, source, index
        )


@pytest.mark.parametrize("scenario", ["cancel", "risk"])
async def test_primary_satisfied_still_checks_bad_merge_or_bypass_and_repairs_once(
    tmp_path, monkeypatch, scenario
):
    plan, states = material()
    text = (
        "同一交易撤销后只退账并保留原始记录，不重新生效；审批通过的交易生效。"
        if scenario == "cancel"
        else "所有清算前必须通过风险校验；匹配成功不能替代风险校验。"
    )
    states[0].content.blocks[0].text = text
    bad = (
        [
            ("Start", "Cancel"),
            ("Start", "Approve"),
            ("Cancel", "Active"),
            ("Approve", "Active"),
            ("Active", "End"),
        ]
        if scenario == "cancel"
        else [("Start", "Check"), ("Start", "Match"), ("Check", "Clear"), ("Match", "Clear")]
    )
    good = (
        [
            ("Start", "Cancel"),
            ("Start", "Approve"),
            ("Cancel", "Cancelled"),
            ("Cancelled", "End"),
            ("Approve", "Active"),
            ("Active", "End"),
        ]
        if scenario == "cancel"
        else [("Start", "Check"), ("Start", "Match"), ("Match", "Check"), ("Check", "Clear")]
    )
    designs, calls = [], []

    async def render(source, folder):
        image, path = folder / "image.png", folder / "source.mmd"
        png(image)
        path.write_text(source)
        return {
            "image_path": str(image),
            "source_path": str(path),
            "width": 1,
            "height": 1,
            "graph_index": graph(designs[-1]),
        }

    monkeypatch.setattr("folio.visuals.render.render_flowchart", render)

    async def call(task, context, model):
        calls.append((task, model))
        if task == "design_flowchart":
            designs.append(bad if not designs else good)
            return VisualSource(lines=["flowchart TD", *(f"{a} --> {b}" for a, b in designs[-1])])
        if model is not SemanticReview:
            return LiveVisualReview(
                findings=[
                    LiveVisualFinding(
                        verdict="satisfied",
                        kind="semantic",
                        source_block_id="b1",
                        source_quote=text,
                        from_node="Start",
                        description="初审称所有业务路径均正确。",
                        difference=None,
                    )
                ]
            )
        assert context["semantic_cases"][-1]["case_id"] == "topology_paths"
        assert context["source_blocks"][0]["text"] == text
        response = equivalent(context["semantic_cases"], context["source_blocks"])
        if len(designs) == 1:
            left, right = ("Cancel", "Active") if scenario == "cancel" else ("Match", "Clear")
            response["checks"][-1].update(
                result="actual_difference",
                differences=[
                    {
                        "source_block_id": "b1",
                        "source_quote": text,
                        "required_behavior": "撤销的原交易不生效"
                        if scenario == "cancel"
                        else "清算前通过风险校验",
                        "actual_behavior": "撤销原交易被合并到生效"
                        if scenario == "cancel"
                        else "匹配路径绕过校验",
                        "actual_quote": f"{left} --> {right}",
                        "from_node": left,
                        "to_node": right,
                        "actual_path": [left, right],
                        "description": "存在原文不允许的状态合并或前置条件绕过。",
                    }
                ],
            )
        return SemanticReview.model_validate(response)

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues and len(designs) == 2
    assert sum(model is SemanticReview for _, model in calls) == 2  # One batched call per attempt.
    audit = json.loads(next(tmp_path.glob("visuals/*/semantic-review-1.json")).read_text())
    assert audit["checks"][-1]["result"] == "actual_difference"


@pytest.mark.parametrize(
    "text,edges",
    [
        (
            "原交易撤销后可从原记录创建新交易，审批通过后新交易生效。",
            [
                ("Start", "Cancel"),
                ("Cancel", "NewTrade"),
                ("Start", "Approve"),
                ("NewTrade", "Approve"),
                ("Approve", "Active"),
            ],
        ),
        (
            "示意图分别展示匹配业务与风险分析业务；原文没有要求匹配必须经过风险分析。",
            [
                ("Start", "Match"),
                ("Start", "RiskAnalysis"),
                ("Match", "End"),
                ("RiskAnalysis", "End"),
            ],
        ),
    ],
)
async def test_legitimate_new_identity_or_no_required_guard_is_not_a_keyword_violation(
    tmp_path, monkeypatch, text, edges
):
    plan, states = material()
    states[0].content.blocks[0].text = text
    seen = []

    async def render(source, folder):
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

    monkeypatch.setattr("folio.visuals.render.render_flowchart", render)

    async def call(task, context, model):
        seen.append(model)
        if task == "design_flowchart":
            return VisualSource(lines=["flowchart TD", *(f"{a} --> {b}" for a, b in edges)])
        if model is SemanticReview:
            assert "do not add ordering, checks or terminal states based on business common knowledge" in context["review_contract"]["instruction"]
            return SemanticReview.model_validate(
                equivalent(context["semantic_cases"], context["source_blocks"])
            )
        return LiveVisualReview()

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues and seen.count(VisualSource) == seen.count(SemanticReview) == 1
