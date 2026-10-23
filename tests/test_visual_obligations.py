from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from folio.orchestration.visual_obligations import (
    instruction_visual_kinds,
    missing_visual_obligations,
    required_visual_kinds,
    validate_required_visuals,
    visual_obligation_schema,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("说明各功能模块间的业务关联及流程图。", ("flowchart",)),
        ("描述前中台业务流程，包括流程图和详细说明。", ("flowchart",)),
        ("描述前中台交易、清算等关键业务流程，附流程图和说明。", ("flowchart",)),
        ("绘制流程图。提供用户界面示意图。", ("flowchart", "ui")),
        ("附上对应的用户界面原型图。", ("ui",)),
        ("系统采用分层设计，应提供流程图。", ("flowchart",)),
        ("描述静态数据的录入、修改、查询等功能步骤及界面。", ()),
        ("描述流程和界面要求。", ()),
        ("流程图说明。", ()),
        ("说明流程图中的审批规则。", ()),
        ("不需要绘制流程图。", ()),
        ("不要求提供界面示意图。", ()),
        ("无需流程图，仅描述操作步骤。", ()),
        ("按需提供流程图。", ()),
        ("必要时绘制流程图。", ()),
        ("若涉及多分支则绘制流程图。", ()),
        ("根据实际需要提供界面示意图。", ()),
        ("可以绘制流程图辅助说明。", ()),
        ("例如：提供流程图或界面示意图。", ()),
        ("示例：附流程图。", ()),
        ("例如，绘制流程图辅助说明。", ()),
        ("若业务复杂，提供流程图。", ()),
        ("按需，附上界面示意图。", ()),
        ("不要求业务关联图，也不要求提供流程图。", ()),
        ("系统支持流程编排，绘制流程图。", ()),
        ("提供流程图，界面示意图按需提供。", ("flowchart",)),
        ("按需绘制流程图，但必须提供界面示意图。", ("ui",)),
        ("系统支持绘制流程图。", ()),
        ("提供流程图编辑功能。", ()),
        ("描述业务功能及流程图工具。", ()),
        ("无需流程图，但必须提供界面示意图。", ("ui",)),
        ("提供流程图；界面示意图按需提供。", ("flowchart",)),
    ],
)
def test_only_explicit_positive_writing_instructions(text, expected):
    assert instruction_visual_kinds(text) == expected


def fixture_sections():
    return [
        {
            "section_id": "relations",
            "title": "业务关联关系",
            "instruction": "说明业务关联及流程图。",
        },
        {
            "section_id": "flow",
            "title": "业务流程",
            "instruction": "描述业务流程，包括流程图和详细说明。",
        },
        {
            "section_id": "child",
            "parent_id": "flow",
            "title": "前中台业务流程",
            "instruction": "描述交易与清算，附流程图和说明。",
        },
        {"section_id": "title_only", "title": "业务流程图", "instruction": "描述业务规则。"},
    ]


def test_parent_uses_explicit_child_but_sibling_does_not_erase_requirement():
    sections = fixture_sections()
    states = [{"section_id": "child", "content": {"visuals": [{"kind": "flowchart"}]}}]
    assert required_visual_kinds("flow", sections) == ()
    assert required_visual_kinds("child", sections) == ("flowchart",)
    assert required_visual_kinds("relations", sections) == ("flowchart",)
    assert required_visual_kinds("title_only", sections) == ()
    assert [(x.section_id, x.kind) for x in missing_visual_obligations(sections, states)] == [
        ("relations", "flowchart")
    ]


def test_actual_child_chart_can_satisfy_parent_even_if_not_preplanned():
    sections = fixture_sections()
    sections[2]["instruction"] = "描述交易审批规则。"
    assert required_visual_kinds("flow", sections) == ("flowchart",)
    states = [{"section_id": "child", "content": {"visuals": [{"kind": "flowchart"}]}}]
    assert "flow" not in {x.section_id for x in missing_visual_obligations(sections, states)}


def test_delegation_is_not_completion_until_child_really_has_correct_figure():
    sections = fixture_sections()
    states = [{"section_id": "child", "content": {"visuals": [{"kind": "ui"}]}}]
    missing = {x.section_id for x in missing_visual_obligations(sections, states)}
    assert missing == {"relations", "flow", "child"}


def test_deep_descendants_with_intermediate_nodes_and_multiple_types():
    sections = fixture_sections()
    sections[1]["instruction"] += "提供界面示意图。"
    sections.extend(
        [
            {"section_id": "middle", "parent_id": "flow", "instruction": "业务规则。"},
            {"section_id": "ui", "parent_id": "middle", "instruction": "绘制界面原型图。"},
        ]
    )
    assert required_visual_kinds("flow", sections) == ()
    states = [
        {"section_id": "child", "content": {"visuals": [{"kind": "flowchart"}]}},
        {"section_id": "ui", "content": {"visuals": [{"kind": "ui"}]}},
    ]
    assert [x.section_id for x in missing_visual_obligations(sections, states)] == ["relations"]


def test_schema_minimum_and_local_check_do_not_disable_other_figures_or_text_validation():
    schema = {
        "type": "object",
        "properties": {"visuals": {"type": "array", "maxItems": 3, "items": {"type": "object"}}},
    }
    before = deepcopy(schema)
    actual = visual_obligation_schema(schema, ["flowchart", "ui"])
    assert actual["properties"]["visuals"]["minItems"] == 2
    assert actual["properties"]["visuals"]["maxItems"] == 3
    assert schema == before
    validator = Draft202012Validator(actual)
    assert not validator.is_valid({})
    assert not validator.is_valid({"visuals": []})
    duplicate = {"visuals": [{"kind": "ui"}, {"kind": "ui"}]}
    assert validator.is_valid(duplicate)
    with pytest.raises(ValueError, match="flowchart"):
        validate_required_visuals(duplicate, ["flowchart", "ui"])
    valid = {"visuals": [{"kind": "flowchart"}, {"kind": "ui"}, {"kind": "ui"}]}
    assert validator.is_valid(valid)
    validate_required_visuals(valid, ["flowchart", "ui"])
    assert visual_obligation_schema(schema, []) is schema


@pytest.mark.parametrize(
    "instruction", ["提供流程图或界面示意图。", "流程图与界面示意图任选一种。"]
)
def test_alternative_figure_types_are_not_made_conjunctive(instruction):
    assert instruction_visual_kinds(instruction) == ()


def test_container_without_figure_owner_is_pending_not_an_endless_body_repair():
    from folio.orchestration.visual_obligations import visual_obligation_findings

    sections = [
        {"section_id": "parent", "content_mode": "container", "instruction": "提供流程图。"},
        {
            "section_id": "child",
            "parent_id": "parent",
            "content_mode": "generate",
            "instruction": "说明业务规则。",
        },
    ]
    states = [{"section_id": "parent", "content": {}}, {"section_id": "child", "content": {}}]
    issues = visual_obligation_findings(sections, states)
    assert len(issues) == 1 and issues[0].kind == "pending"
    states[1]["content"]["visuals"] = [{"kind": "flowchart"}]
    assert visual_obligation_findings(sections, states) == []
