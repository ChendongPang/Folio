from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from folio.models import SectionContent
from folio.orchestration.visual_obligations import (
    figure_policy,
    missing_visual_obligations,
    validate_required_visuals,
    visual_obligation_schema,
)


def node(sid, instruction, parent=None):
    return {"section_id": sid, "instruction": instruction, "parent_id": parent}


def visual(fid="ui1", kind="ui"):
    return {
        "figure_id": fid,
        "kind": kind,
        "caption": "已保存的图",
        "description": "按正文展示",
        "source_block_ids": ["b1"],
        "after_block_id": "b1",
    }


def test_confirmed_parent_obligation_can_be_owned_by_child_without_three_extra_parent_figures():
    sections = [
        node("parent", "描述前中台业务流程，包括流程图和详细说明。"),
        node("child", "描述前中台交易、清算等关键业务流程，附流程图和说明。", "parent"),
        node("relation", "说明各功能模块间的业务关联及流程图。"),
    ]
    before = deepcopy(sections)
    parent = figure_policy("parent", sections)
    assert parent["allowed_visual_kinds"] == parent["required_visual_kinds"] == []
    assert parent["delegated_to_children"] == [{"kind": "flowchart", "section_ids": ["child"]}]
    for sid in ("child", "relation"):
        policy = figure_policy(sid, sections)
        assert policy["allowed_visual_kinds"] == policy["required_visual_kinds"] == ["flowchart"]
    assert sections == before


def test_ui_description_is_not_authorization_for_ui_mockups():
    sections = [
        node("features", "详细描述各功能模块，包括权限、步骤、界面、规则等。"),
        node("fx", "描述外汇即期交易的录入、确认、结算等功能。", "features"),
        node("bond", "描述债券发行信息的录入、审核及公告功能。", "features"),
    ]
    for sid in ("fx", "bond"):
        assert figure_policy(sid, sections)["allowed_visual_kinds"] == []


@pytest.mark.parametrize(
    "instruction",
    [
        "如有需要，附界面图。",
        "可提供流程图或界面图。",
        "必要时提供流程图。",
    ],
)
def test_optional_or_conditional_figure_instruction_stays_optional_and_keeps_its_condition(
    instruction,
):
    policy = figure_policy("s", [node("s", instruction)])
    assert policy["allowed_visual_kinds"]
    assert not policy["required_visual_kinds"]
    assert policy["conditional_instructions"] == [instruction.rstrip("。")]


def test_separate_optional_parent_overview_and_explicit_ancestor_request_remain_authorized():
    sections = [
        node("parent", "附流程图。必要时提供概览流程图。"),
        node("child", "附流程图。", "parent"),
        node("ui_parent", "提供界面图。"),
        node("ui_child", "描述字段。", "ui_parent"),
        node("no_ui", "无需界面图。", "ui_parent"),
    ]
    policy = figure_policy("parent", sections)
    assert policy["allowed_visual_kinds"] == ["flowchart"]
    assert not policy["required_visual_kinds"]
    assert policy["conditional_instructions"] == ["必要时提供概览流程图"]
    assert figure_policy("ui_child", sections)["allowed_visual_kinds"] == ["ui"]
    assert figure_policy("no_ui", sections)["allowed_visual_kinds"] == []
    assert figure_policy("ui_child", [node("ui_child", "描述字段。")], "请提供界面图")[
        "allowed_visual_kinds"
    ] == ["ui"]


def test_new_disallowed_figure_is_rejected_in_schema_and_result_not_silently_deleted():
    content = {"visuals": [visual()]}
    before = deepcopy(content)
    schema = visual_obligation_schema(SectionContent.model_json_schema(), [], [])
    assert schema["properties"]["visuals"]["maxItems"] == 0
    assert list(
        Draft202012Validator(
            schema["properties"]["visuals"] | {"$defs": schema["$defs"]}
        ).iter_errors(content["visuals"])
    )
    with pytest.raises(ValueError, match="outside confirmed"):
        validate_required_visuals(content, [], [])
    assert content == before


def test_repair_can_keep_existing_unrequested_ui_but_cannot_add_another_or_change_its_kind():
    original = visual()
    revised = original | {"caption": "同一已保存图的更新说明", "description": "依据最新版正文修订"}
    schema = visual_obligation_schema(SectionContent.model_json_schema(), [], [], [original])
    validator = Draft202012Validator({"$ref": "#/$defs/VisualRequest", "$defs": schema["$defs"]})
    validator.validate(revised)
    validate_required_visuals({"visuals": [revised]}, [], [], [original])
    assert schema["properties"]["visuals"]["minItems"] == 1
    with pytest.raises(ValueError, match="Existing figures"):
        validate_required_visuals({"visuals": []}, [], [], [original])
    assert list(validator.iter_errors(visual("extra")))
    for unexpected in [visual("extra"), visual(kind="flowchart")]:
        with pytest.raises(ValueError, match="outside confirmed"):
            validate_required_visuals({"visuals": [unexpected]}, [], [], [original])
    # A newly required flow and the old UI may coexist, without authorizing more UI.
    schema = visual_obligation_schema(
        SectionContent.model_json_schema(), ["flowchart"], ["flowchart"], [original]
    )
    assert set(schema["$defs"]["VisualRequest"]["properties"]["kind"]["enum"]) == {
        "flowchart",
        "ui",
    }
    validate_required_visuals(
        {"visuals": [revised, visual("flow", "flowchart")]},
        ["flowchart"],
        ["flowchart"],
        [original],
    )
    with pytest.raises(ValueError, match="missing"):
        validate_required_visuals({"visuals": [revised]}, ["flowchart"], ["flowchart"], [original])


@pytest.mark.parametrize(
    "text,allowed,required",
    [
        ("请生成流程图。", ["flowchart"], ["flowchart"]),
        ("无需流程图，提供界面示意图。", ["ui"], ["ui"]),
        ("不要重复前文，提供流程图。", ["flowchart"], ["flowchart"]),
        ("如需展示流程图，可提供界面原型图。", ["flowchart", "ui"], []),
        ("Draw a flowchart. Provide a UI mockup.", ["flowchart", "ui"], ["flowchart", "ui"]),
        ("Do not draw a flowchart, provide a UI mockup.", ["ui"], ["ui"]),
        ("If needed, draw a flowchart.", ["flowchart"], []),
        ("流程图用来直观说明本节。", ["flowchart"], []),
    ],
)
def test_explicit_figure_names_remain_allowed_without_cross_clause_negation(
    text, allowed, required
):
    policy = figure_policy("s", [node("s", text)])
    assert policy["allowed_visual_kinds"] == allowed
    assert policy["required_visual_kinds"] == required


def test_user_wide_prohibition_overrides_template_but_scoped_prohibition_is_not_global():
    sections = [node("s", "附流程图说明审批过程。")]
    policy = figure_policy("s", sections, "这次不要流程图。")
    assert policy["allowed_visual_kinds"] == policy["required_visual_kinds"] == []
    assert policy["prohibited_visual_kinds"] == ["flowchart"]
    assert missing_visual_obligations(sections, [], user_request="这次不要流程图。") == []
    policy = figure_policy("s", sections, "请不要生成任何界面图或流程图。")
    assert policy["prohibited_visual_kinds"] == ["flowchart", "ui"]
    assert policy["allowed_visual_kinds"] == policy["required_visual_kinds"] == []
    policy = figure_policy("s", sections, "A节不要流程图，B节提供流程图。")
    assert policy["allowed_visual_kinds"] == policy["required_visual_kinds"] == ["flowchart"]
    for scoped in ("不要在A节画流程图。", "Do not draw a flowchart in section A."):
        policy = figure_policy("s", sections, scoped)
        assert policy["prohibited_visual_kinds"] == []


def test_independent_parent_overview_is_not_delegated_to_a_child_approval_chart():
    sections = [
        node("parent", "提供总览流程图。"),
        node("child", "提供审批流程图。", "parent"),
    ]
    policy = figure_policy("parent", sections)
    assert policy["allowed_visual_kinds"] == policy["required_visual_kinds"] == ["flowchart"]
    assert policy["delegated_to_children"] == []
    states = [{"section_id": "child", "content": {"visuals": [{"kind": "flowchart"}]}}]
    assert [x.section_id for x in missing_visual_obligations(sections, states)] == ["parent"]
