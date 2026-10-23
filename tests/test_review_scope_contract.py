"""Regressions from 37544e: delegated parent figure and vendor benchmark misuse.

The snippets keep the failed inference, but use local synthetic identifiers and
no private documents, archived run paths, credentials or live model calls.
"""

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import DrillDown, JobInput, SourceApplicabilityCheck
from folio.orchestration.context import project
from folio.orchestration.engine import Engine
from folio.orchestration.focused_review import FocusedReviewWorkflow, material_parts
from folio.orchestration.review_scope import (
    applicability_schema,
    attach_review_scopes,
    section_review_scope,
    validate_applicability,
)
from folio.runtime.business_transport import restore_empty_fields, translate_ids, unpack_rows
from folio.runtime.llm import LLMRuntime


def nodes():
    return [
        {
            "section_id": "parent",
            "title": "业务流程",
            "instruction": "说明业务流程，包括流程图。",
            "parent_id": None,
        },
        {
            "section_id": "child",
            "title": "前中台业务流程",
            "instruction": "说明前中台业务流程并绘制流程图。",
            "parent_id": "parent",
        },
        {
            "section_id": "peer",
            "title": "业务量",
            "instruction": "描述本项目业务量需求。",
            "parent_id": None,
        },
    ]


def child_state():
    return {
        "section_id": "child",
        "revision": 1,
        "content": {
            "blocks": [],
            "visuals": [
                {
                    "figure_id": "flow_1",
                    "kind": "flowchart",
                    "caption": "前中台业务流程概览",
                    "source_block_ids": ["b1"],
                    "after_block_id": "b1",
                }
            ],
        },
    }


def test_real_parent_figure_assignment_survives_every_review_view_without_waiving_visual_quality():
    original = {
        "section": nodes()[0],
        "sections": [{"section_id": "parent", "content": {"blocks": [], "visuals": []}}],
    }
    source = attach_review_scopes(original, nodes(), [child_state()])
    for task in ("generate", "repair", "validate", "review_work", "review", "drill_down"):
        scope = project(task, source)["review_scopes"][0]
        assert scope["figure_policy"]["allowed_visual_kinds"] == []
        assert scope["figure_policy"]["required_visual_kinds"] == []
        assert scope["child_sections"][0]["section_id"] == "child"
        delegated = scope["delegated_visuals"][0]
        assert delegated["section_id"] == "child"
        assert delegated["visual_requests"][0]["source_block_ids"] == ["b1"]
        assert delegated["semantic_validation"] == "not_established_by_request_presence"
    assert "review_scopes" not in original


def test_saved_child_revision_changes_request_identity_and_unsaved_child_is_not_claimed_complete():
    first = section_review_scope("parent", nodes())
    assert first["delegated_visuals"][0]["state"] == "not_saved_yet"
    assert first["delegated_visuals"][0]["visual_requests"] == []
    saved = section_review_scope("parent", nodes(), [child_state()])
    assert first != saved
    revised = child_state() | {"revision": 2, "content": {"blocks": [], "visuals": []}}
    assert saved != section_review_scope("parent", nodes(), [revised])


def test_independent_parent_overview_and_user_ban_are_not_waived_by_child():
    plan = nodes()
    plan[0]["instruction"] = "提供总览流程图。"
    scope = section_review_scope("parent", plan, [child_state()])
    assert scope["figure_policy"]["required_visual_kinds"] == ["flowchart"]
    assert scope["delegated_visuals"] == []
    scope = section_review_scope("parent", plan, [child_state()], "请不要生成任何流程图")
    assert scope["figure_policy"]["allowed_visual_kinds"] == []
    assert scope["figure_policy"]["prohibited_visual_kinds"] == ["flowchart"]


def test_confirmed_parent_repair_is_checked_against_structured_assignment_not_word_matching():
    context = attach_review_scopes(
        {
            "finding": {"kind": "coverage", "section_ids": ["parent"], "key": "opaque-928ab"},
            "sections": [{"section_id": "parent", "content": {"blocks": [], "visuals": []}}],
        },
        nodes(),
        [child_state()],
    )
    # This is the old failed adjudication's shape; its free prose cannot override assignment.
    old = DrillDown(
        confirmed=True,
        reason="需调整本节",
        affected_section_ids=["parent"],
        repair_instruction="添加图片。",
    )
    with pytest.raises(ValueError, match="must identify repair_targets"):
        validate_applicability(old, context)
    for kind in ("flowchart", "ui"):
        result = old.model_copy(update={"repair_targets": []}).model_dump()
        result["repair_targets"] = [
            {"section_id": "parent", "component": "visuals", "action": "add", "visual_kind": kind}
        ]
        with pytest.raises(ValueError, match="conflicts with confirmed figure_policy"):
            validate_applicability(DrillDown.model_validate(result), context)
    # A legitimate independent parent prose error remains repairable.
    body = old.model_dump() | {
        "repair_instruction": "补足父节明确的共用控制条件。",
        "repair_targets": [
            {"section_id": "parent", "component": "body", "action": "update", "visual_kind": ""}
        ],
    }
    validate_applicability(DrillDown.model_validate(body), context)
    # Parent's own independent overview is never waived by a child figure.
    plan = nodes()
    plan[0]["instruction"] = "提供总览流程图。"
    own = attach_review_scopes(context, plan, [child_state()])
    result["repair_targets"][0]["visual_kind"] = "flowchart"
    validate_applicability(DrillDown.model_validate(result), own)


def test_applicability_twelve_block_boundary_retains_every_target_without_truncation():
    context = applicability_context()
    template = context["sections"][0]["content"]["blocks"][0]
    context["sections"][0]["content"]["blocks"] = [
        template | {"block_id": f"b{i}"} for i in range(12)
    ]
    context["finding"]["block_refs"] = [
        {"section_id": "peer", "block_id": f"b{i}"} for i in range(12)
    ]
    schema = applicability_schema(DrillDown, DrillDown.model_json_schema(), context)
    assert schema["properties"]["applicability_checks"]["minItems"] == 12
    assert schema["properties"]["applicability_checks"]["maxItems"] == 12
    result = verdict([checked(block_id=f"b{i}") for i in range(12)])
    validate_applicability(result, context)
    with pytest.raises(ValueError, match="exactly"):
        validate_applicability(
            result.model_copy(update={"applicability_checks": result.applicability_checks[:-1]}),
            context,
        )


async def test_real_parent_false_positive_context_retains_child_location_after_focused_selection():
    class Reader(FocusedReviewWorkflow):
        async def select_reads(self, purpose, catalog):
            return []

    context = attach_review_scopes(
        {
            "finding": {
                "kind": "coverage",
                "section_ids": ["parent"],
                "key": "missing_flowchart",
                "description": "章节instruction明确要求包含流程图，但content.visuals为空。",
            },
            "sections": [
                {"section_id": "parent", "content": {"blocks": [], "visuals": []}, "evidence": []}
            ],
            "definitions": [nodes()[0]],
        },
        nodes(),
        [child_state()],
    )
    before = deepcopy(context)
    result = await Reader().prepare_issue_context(context)
    assert result["scope_contract"] == 1
    assert result["review_scopes"] == context["review_scopes"]
    assert len(result["sections"]) == 1  # Child evidence location, not whole sibling body.
    assert result["review_scopes"][0]["delegated_visuals"][0]["visual_requests"]
    assert before == context


BODY = "本项目20,000笔头寸日终处理应在15分钟内完成。"
SOURCE = "供应商产品性能测试：2万笔头寸日终跑批耗时12分钟；并发数800。"


def source(eid, text, role=""):
    return {
        "context_ref": "ctx_" + eid,
        "kind": "evidence",
        "value": {
            "evidence_id": eid,
            "source_id": eid,
            "text": text,
            "metadata": {"role": role},
        },
    }


def applicability_context():
    return {
        "scope_contract": 1,
        "review_scopes": [{"section_id": "peer", "definition": nodes()[2]}],
        "finding": {
            "kind": "unsupported",
            "section_ids": ["peer"],
            "key": "known_proposal:test",
            "block_refs": [{"section_id": "peer", "block_id": "b3"}],
        },
        "sections": [
            {
                "section_id": "peer",
                "content": {
                    "blocks": [
                        {
                            "block_id": "b3",
                            "text": BODY,
                            "origin": "evidence",
                            "evidence_ids": ["vendor"],
                        }
                    ]
                },
            }
        ],
        "original_materials": [source("vendor", SOURCE)],
    }


def checked(**changes):
    result = dict(
        section_id="peer",
        block_id="b3",
        target_quote=BODY,
        target_subject="本项目头寸日终处理",
        target_role="project_requirement",
        evidence_id="vendor",
        source_quote="2万笔头寸日终跑批耗时12分钟",
        source_subject_quote="供应商产品性能测试",
        source_role="test_result",
        applicability="uncertain",
        scope_reason="原文证明供应商产品在测试条件下的能力，没有说明本项目已采用该验收标准。",
        adoption_evidence_id="",
        adoption_quote="",
    )
    return result | changes


def verdict(checks=None, **changes):
    return DrillDown.model_validate(
        {
            "confirmed": False,
            "reason": "项目是否采用产品测试指标尚无依据。",
            "needs_confirmation": True,
            "applicability_checks": [checked()] if checks is None else checks,
        }
        | changes
    )


def test_actual_benchmark_false_dismissal_without_subject_or_role_support_is_rejected():
    response = DrillDown(
        confirmed=False, reason="证据包含2万头寸12分钟及并发800，与正文数字一致，因此为误报。"
    )
    context = applicability_context()
    with pytest.raises(ValueError, match="target blocks"):
        validate_applicability(response, context)
    schema = applicability_schema(DrillDown, DrillDown.model_json_schema(), context)
    assert schema["properties"]["applicability_checks"]["minItems"] == 1
    assert "applicability_checks" in schema["required"]
    assert "repair_targets" in schema["required"]
    assert "source_role" in schema["$defs"]["SourceApplicabilityCheck"]["required"]
    assert "source_subject_quote" in schema["$defs"]["SourceApplicabilityCheck"]["required"]
    legacy = applicability_schema(
        DrillDown, DrillDown.model_json_schema(), context | {"scope_contract": 0}
    )
    assert "applicability_checks" not in legacy["required"]
    assert "repair_targets" not in legacy["required"]


def test_untraced_body_hard_finding_has_real_block_anchor_for_applicability_review():
    from folio.models import Block, Extraction, Section, SectionContent
    from folio.orchestration.local_checks import hard_findings
    from folio.orchestration.review_scope import applicability_targets

    section = Section(
        section_id="peer",
        node_id="peer",
        title="约束和假设",
        level=1,
        display_number="1",
        instruction="说明项目适用约束。",
    )
    content = SectionContent(
        blocks=[Block(block_id="b3", text="具体业务支持约束未提供，需后续确认。")]
    )
    findings = hard_findings(section, content, [], Extraction(summary="约束"))
    finding = next(f for f in findings if f.kind == "unsupported" and f.key == "b3")
    assert [ref.model_dump() for ref in finding.block_refs] == [
        {"section_id": "peer", "block_id": "b3"}
    ]
    context = attach_review_scopes(
        {
            "finding": finding.model_dump(),
            "sections": [{"section_id": "peer", "content": content.model_dump()}],
        },
        [section],
    )
    assert applicability_targets(context) == [("peer", "b3")]


def test_exact_numbers_and_real_benchmark_quotes_still_cannot_establish_project_adoption():
    with pytest.raises(ValueError, match="cannot prove project adoption"):
        validate_applicability(
            verdict([checked(applicability="direct")], needs_confirmation=False),
            applicability_context(),
        )
    with pytest.raises(ValueError, match="cannot prove project adoption"):
        validate_applicability(
            verdict(
                [
                    checked(
                        applicability="direct", adoption_evidence_id="vendor", adoption_quote=SOURCE
                    )
                ],
                needs_confirmation=False,
            ),
            applicability_context(),
        )


def test_unconfirmed_adoption_cannot_be_dismissed_but_remains_pending_without_rewriting_body():
    context = applicability_context()
    before = deepcopy(context)
    validate_applicability(verdict(), context)
    with pytest.raises(ValueError, match="Uncertain subject/adoption"):
        validate_applicability(verdict(needs_confirmation=False), context)
    assert context == before


def test_known_reference_misrepresented_as_project_fact_can_be_corrected_without_user_research():
    result = verdict(
        [checked(applicability="reference_only")],
        confirmed=True,
        needs_confirmation=False,
        affected_section_ids=["peer"],
        repair_instruction="保留为拟议验收要求并标model_proposal，不再称已确认指标。",
    )
    validate_applicability(result, applicability_context())
    with pytest.raises(ValueError, match="cannot be dismissed"):
        validate_applicability(
            result.model_copy(update={"confirmed": False}), applicability_context()
        )


def test_explicit_project_adoption_or_attributed_benchmark_can_legitimately_dismiss():
    adoption = "本项目确认采用20,000笔头寸15分钟以内完成的验收指标。"
    context = applicability_context()
    context["original_materials"].append(source("project", adoption, "project_requirement"))
    validate_applicability(
        verdict(
            [
                checked(
                    applicability="direct", adoption_evidence_id="project", adoption_quote=adoption
                )
            ],
            needs_confirmation=False,
        ),
        context,
    )
    reference = "供应商报告称2万笔头寸日终跑批耗时12分钟，仅作能力参考。"
    context["sections"][0]["content"]["blocks"][0]["text"] = reference
    validate_applicability(
        verdict(
            [
                checked(
                    target_quote=reference, target_role="source_reference", applicability="direct"
                )
            ],
            needs_confirmation=False,
        ),
        context,
    )


@pytest.mark.parametrize(
    ("reported_fact", "source_role"),
    [
        ("外部厂商的训练芯片面向大型模型训练场景。", "product_capability"),
        ("第三方企业的数据中心项目已投运100MW容量。", "external_reference"),
        ("行业市场调查记录的光模块需求增长率为20%。", "external_reference"),
    ],
)
@pytest.mark.parametrize("project_role", ["project_requirement", "project_description"])
def test_third_party_reporting_and_own_project_adoption_keep_distinct_evidence_requirements(
    reported_fact, source_role, project_role
):
    context = applicability_context()
    # A research-report title cannot exempt its user's own project claims.
    context["user_request"] = "撰写AI基础设施行业研究报告。"
    context["original_materials"] = [source("vendor", reported_fact, "public_web")]
    block = context["sections"][0]["content"]["blocks"][0]
    block["text"] = reported_fact
    third_party = checked(
        target_quote=reported_fact,
        target_subject="第三方产品、第三方项目或市场事实",
        target_role="source_reference",
        source_quote=reported_fact,
        source_subject_quote=reported_fact,
        source_role=source_role,
        applicability="direct",
        scope_reason="正文与外部原文报道同一第三方主体和条件，没有声称用户项目采用。",
    )
    # Objective reporting needs no "for reference only" wording or user-project adoption.
    validate_applicability(verdict([third_party], needs_confirmation=False), context)
    # A genuine missing citation can still be repaired without upgrading adoption.
    validate_applicability(
        verdict(
            [third_party],
            confirmed=True,
            needs_confirmation=False,
            affected_section_ids=["peer"],
            repair_instruction="给该第三方事实补充现有原文的引用，不改正文。",
        ),
        context,
    )
    for broken in ("source_quote", "source_subject_quote"):
        invalid = third_party | {broken: "另一个主体或条件，没有出现在所给原文中。"}
        with pytest.raises(ValueError, match=broken):
            validate_applicability(verdict([invalid], needs_confirmation=False), context)

    own_project_claim = "用户项目已采纳上述外部方案和指标作为交付要求。"
    block["text"] = own_project_claim
    own_project = third_party | {
        "target_quote": own_project_claim,
        "target_subject": "用户项目的交付要求或已采用事实",
        "target_role": project_role,
    }
    with pytest.raises(ValueError) as failure:
        validate_applicability(verdict([own_project], needs_confirmation=False), context)
    message = str(failure.value)
    assert "applicability_checks[0].target_role/adoption_evidence_id" in message
    assert f"target_role={project_role}" in message
    assert "cannot prove project adoption" in message
    assert "source_reference" in message and "third-party" in message
    assert "do not relabel an actual adoption claim" in message
    assert reported_fact not in message and own_project_claim not in message


@pytest.mark.parametrize(
    "change",
    [
        {"source_quote": "凭空伪造的数字"},
        {"source_subject_quote": "本项目正式验收需求"},
        {"target_quote": "其他节点正文"},
        {"evidence_id": "missing"},
    ],
)
def test_applicability_evidence_must_resolve_to_actual_body_and_original_source(change):
    with pytest.raises(ValueError):
        validate_applicability(verdict([checked(**change)]), applicability_context())


def table_applicability_context():
    context = applicability_context()
    context["sections"][0]["content"]["blocks"][0].update(
        kind="table",
        text="供应商产品性能测试（2万笔头寸）的耗时与并发数，据测试报告整理。",
        rows=[["测试对象", "耗时", "并发数"], ["2万笔头寸", "12分钟", "800"]],
    )
    return context


@pytest.mark.parametrize("excerpt", ["caption", "caption_part", "cell", "serialized_row"])
def test_table_target_quote_can_locate_its_real_caption_or_rows(excerpt):
    context = table_applicability_context()
    table = context["sections"][0]["content"]["blocks"][0]
    quote = {
        "caption": table["text"],
        "caption_part": "供应商产品性能测试（2万笔头寸）的耗时与并发数",
        "cell": "12分钟",
        "serialized_row": json.dumps(table["rows"][1], ensure_ascii=False),
    }[excerpt]
    before = deepcopy(context)
    validate_applicability(verdict([checked(target_quote=quote)]), context)
    assert context == before


@pytest.mark.parametrize(
    "change",
    ["wording", "number", "punctuation", "splice", "ellipsis", "other_block", "other_section"],
)
def test_table_target_quote_stays_continuous_and_scoped_to_the_exact_block(change):
    context = table_applicability_context()
    table = context["sections"][0]["content"]["blocks"][0]
    another = table | {"text": "另一个测试方案的结果，与本表不同。"}
    context["sections"][0]["content"]["blocks"].append(another | {"block_id": "other"})
    context["sections"].append(
        {
            "section_id": "other_section",
            "content": {"blocks": [table | {"text": "另一章节同编号表格的结果。"}]},
        }
    )
    quote = {
        "wording": table["text"].replace("性能测试", "性能评估"),
        "number": table["text"].replace("2万", "3万"),
        "punctuation": table["text"].replace("（", "(").replace("）", ")"),
        "splice": table["text"] + json.dumps(table["rows"], ensure_ascii=False),
        "ellipsis": "供应商产品性能测试……并发数",
        "other_block": another["text"],
        "other_section": "另一章节同编号表格的结果。",
    }[change]
    with pytest.raises(ValueError, match="target_quote must quote the actual disputed body block"):
        validate_applicability(verdict([checked(target_quote=quote)]), context)


@pytest.mark.parametrize(
    "field", ["target_quote", "source_quote", "source_subject_quote", "adoption_quote"]
)
def test_applicability_quote_fields_share_the_long_text_character_ceiling(field):
    schema = applicability_schema(DrillDown, DrillDown.model_json_schema(), applicability_context())
    maximum = schema["$defs"]["SourceApplicabilityCheck"]["properties"][field]["maxLength"]
    assert maximum == 600000
    quote = "原" * maximum
    parsed = SourceApplicabilityCheck.model_validate(checked(**{field: quote}))
    assert getattr(parsed, field) == quote
    with pytest.raises(ValidationError) as failure:
        SourceApplicabilityCheck.model_validate(checked(**{field: quote + "文"}))
    assert failure.value.errors(include_input=False)[0]["loc"] == (field,)
    assert failure.value.errors(include_input=False)[0]["type"] == "string_too_long"


def test_long_complete_quotes_keep_subject_and_project_adoption_validation():
    context = applicability_context()
    conditions = " ".join(
        f"Condition {i} applies only to the measured supplier benchmark." for i in range(20)
    )
    original = "Supplier test: 12–18 months. " + conditions
    adoption = "Our project explicitly adopts the following benchmark conditions: " + conditions
    context["original_materials"] = [
        source("vendor", original, "test_result"),
        source("project", adoption, "project_requirement"),
    ]
    context["sections"][0]["content"]["blocks"][0]["text"] = adoption
    check = checked(
        target_quote=adoption,
        source_quote=original,
        source_subject_quote=original,
        applicability="direct",
        adoption_evidence_id="project",
        adoption_quote=adoption,
    )
    assert all(
        len(check[field]) > 600
        for field in ("target_quote", "source_quote", "source_subject_quote", "adoption_quote")
    )
    validate_applicability(verdict([check], needs_confirmation=False), context)
    for field in ("source_quote", "source_subject_quote"):
        with pytest.raises(ValueError, match=field):
            validate_applicability(
                verdict(
                    [check | {field: original.replace("12–18", "12–19")}], needs_confirmation=False
                ),
                context,
            )
    # Longer quoted prose does not turn supplier data into project approval.
    context["original_materials"] = context["original_materials"][:1]
    with pytest.raises(ValueError, match="cannot prove project adoption"):
        validate_applicability(verdict([check], needs_confirmation=False), context)


def test_over_600_character_quote_does_not_hide_changed_extraction_control_characters():
    context = applicability_context()
    original = "Vendor price: $3.0\x003.3/Gb. " + "Unchanged surrounding evidence. " * 25
    context["original_materials"] = [source("vendor", original)]
    check = checked(source_quote=original.replace("\x00", ""), source_subject_quote="Vendor price")
    assert len(check["source_quote"]) > 600
    parsed = verdict([check])  # The longer response is structurally valid.
    with pytest.raises(ValueError, match=r"applicability_checks\[0\].source_quote"):
        validate_applicability(parsed, context)  # The altered number still fails provenance.


@pytest.mark.parametrize("field", ["source_quote", "source_subject_quote"])
@pytest.mark.parametrize(
    ("original", "quote"),
    [
        (
            "Alpha lab\nreported 12–18 months\r\nunder test conditions.",
            "Alpha lab reported 12–18 months under test conditions.",
        ),
        ("Alpha\t\t lab\u00a0reported 12–18 months.", "Alpha lab reported 12–18 months."),
        ("Alpha lab reported 12–18 months.", "  Alpha\nlab reported\t12–18 months.  "),
        (
            "Alpha lab reported 12–18 months ... pending verification.",
            "12–18 months ... pending verification.",
        ),
    ],
)
def test_applicability_accepts_continuous_original_quotes_with_layout_whitespace_only(
    field, original, quote
):
    context = applicability_context()
    context["original_materials"] = [source("vendor", original)]
    before = deepcopy(context)
    check = checked(source_quote=original, source_subject_quote=original)
    check[field] = quote
    validate_applicability(verdict([check]), context)
    assert context == before  # Matching never rewrites the original evidence.


@pytest.mark.parametrize("field", ["source_quote", "source_subject_quote"])
@pytest.mark.parametrize(
    ("original", "quote"),
    [
        (
            "Alpha lab reported 12–18 months under test conditions.",
            "Alpha lab ... under test conditions.",
        ),
        (
            "Alpha lab reported 12–18 months under test conditions.",
            "Alpha lab under test conditions.",
        ),
        ("Alpha lab reported 12–18 months.", "Alpha lab reported 12–13 months."),
        ("Alpha lab reported 12–18 months.", "Beta lab reported 12–18 months."),
        ("Alpha lab reported 12–18 months.", "Alpha lab reported 12-18 months."),
        ("Alpha lab reported 12\x0018 months.", "Alpha lab reported 12–18 months."),
        ("Alpha lab reported 12\x0018 months.", "Alpha lab reported 12 18 months."),
        ("Alpha lab reported 12–18 months.", "Alphalab reported 12–18 months."),
        ("Alpha lab reported 12–18 months.", " \n\t "),
    ],
)
def test_applicability_still_rejects_noncontinuous_or_altered_original_quotes(
    field, original, quote
):
    context = applicability_context()
    context["original_materials"] = [source("vendor", original)]
    check = checked(source_quote=original, source_subject_quote=original)
    check[field] = quote
    with pytest.raises(ValueError) as failure:
        validate_applicability(verdict([check]), context)
    message = str(failure.value)
    assert f"applicability_checks[0].{field}" in message
    assert 'original_materials evidence_id="vendor"' in message
    assert "continuous excerpt" in message
    assert "only whitespace differences" in message
    assert original not in message
    assert not quote.strip() or quote.strip() not in message
    assert len(message) < 500


def test_applicability_quote_feedback_distinguishes_unavailable_original_from_bad_excerpt():
    context = applicability_context()
    context["original_materials"] = []
    with pytest.raises(ValueError) as failure:
        validate_applicability(verdict(), context)
    message = str(failure.value)
    assert "applicability_checks[0].source_quote" in message
    assert 'evidence_id="vendor"' in message
    assert "no supplied complete original" in message
    assert "select an evidence_id in original_materials" in message
    assert SOURCE not in message


def test_complete_fragmented_originals_are_reconstructed_but_partial_sources_are_not_faked():
    context = applicability_context()
    item = context["original_materials"][0]
    context["original_materials"] = material_parts(item, size=40)
    validate_applicability(verdict(), context)
    context["original_materials"] = context["original_materials"][:-1]
    with pytest.raises(ValueError, match="complete original"):
        validate_applicability(verdict(), context)


def test_actual_foreign_exchange_versus_bond_option_scope_mismatch_is_not_excused_by_same_operation():
    context = applicability_context()
    body = "系统支持对债券期权交易行权处理。"
    context["review_scopes"][0]["definition"] = {
        "section_id": "peer",
        "title": "外汇期权交易",
        "instruction": "说明外汇期权交易的业务规则。",
    }
    context["sections"][0]["content"]["blocks"][0]["text"] = body
    context["original_materials"] = [source("vendor", "债券期权交易支持行权处理。")]
    mismatch = checked(
        target_quote=body,
        target_subject="节点要求外汇期权，正文却为债券期权",
        source_quote="债券期权交易支持行权处理。",
        source_subject_quote="债券期权交易",
        source_role="other_context",
        applicability="conflict",
        scope_reason="相同的行权操作并不能证明债券期权规则适用于外汇期权。",
    )
    result = verdict(
        [mismatch],
        confirmed=True,
        needs_confirmation=False,
        affected_section_ids=["peer"],
        repair_instruction="按本节外汇期权范围处理，不能以债券期权原文代替。",
    )
    validate_applicability(result, context)
    with pytest.raises(ValueError, match="cannot be dismissed"):
        validate_applicability(result.model_copy(update={"confirmed": False}), context)


async def test_actual_engine_wire_roundtrip_keeps_scope_checks_and_adoption_reference(
    store, inputs, tmp_path
):
    settings = ModelSettingsStore(tmp_path / "model")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    context = applicability_context()
    adoption = "本项目确认采用20,000笔头寸15分钟以内完成的验收指标。"
    context["original_materials"].append(source("project", adoption, "project_requirement"))
    candidate = verdict(
        [checked(applicability="direct", adoption_evidence_id="project", adoption_quote=adoption)],
        needs_confirmation=False,
    ).model_dump()
    long_ids = {
        "peer": "sec_actual_performance_requirement",
        "vendor": "ev_actual_supplier_performance_test",
        "project": "ev_actual_project_adoption_evidence",
    }
    context, candidate = translate_ids(context, long_ids), translate_ids(candidate, long_ids)
    try:
        schema = engine.request_schema("drill_down", context, DrillDown)
        messages, wire_schema, _, _, references = runtime.request_parts(
            "drill_down", context, schema
        )
        role_description = wire_schema["$defs"]["SourceApplicabilityCheck"]["properties"][
            "target_role"
        ]["description"]
        assert "third-party products, projects, and market facts" in role_description
        assert "user's own delivery project" in role_description
        assert (
            "source_reference includes objective reporting about third-party products, third-party projects, or market facts"
            in messages[0]["content"]
        )
        assert "or relabel adoption assertions about the user's own project as source_reference to evade proof" in messages[0]["content"]
        decoded_context = translate_ids(unpack_rows(json.loads(messages[1]["content"])), references)
        assert decoded_context == context
        assert references and long_ids["vendor"] in references.values()
        aliases = {canonical: wire for wire, canonical in references.items()}
        wire_candidate = translate_ids(candidate, aliases)
        assert wire_candidate["applicability_checks"][0]["adoption_evidence_id"].startswith("sfref")
        Draft202012Validator(wire_schema).validate(wire_candidate)
        restored = restore_empty_fields(translate_ids(wire_candidate, references), schema)
        assert restored["applicability_checks"][0]["adoption_evidence_id"] == long_ids["project"]
        engine.check_result(DrillDown.model_validate(restored), context, task="drill_down")
        bad = candidate | {"applicability_checks": [], "reason": "数字相同，所以是误报。"}
        with pytest.raises(ValueError, match="target blocks"):
            engine.check_result(DrillDown.model_validate(bad), context, task="drill_down")
    finally:
        await runtime.aclose()


async def test_source_pages_with_different_repair_components_do_not_merge_as_one_authorized_edit(
    store, inputs
):
    from test_focused_review import IssuePages, issue_page_context
    from test_request_context import engine_for

    from folio.runtime.base import RuntimeResult

    class DifferentComponents(IssuePages):
        async def complete(self, task, context, schema, session):
            if task != "drill_down":
                return await super().complete(task, context, schema, session)
            component = (
                "body"
                if context["original_materials"][0]["context_ref"] == "ctx_0"
                else "extraction"
            )
            return RuntimeResult(
                json.dumps(
                    {
                        "confirmed": True,
                        "reason": "需要修订。",
                        "affected_section_ids": ["a"],
                        "repair_instruction": "修正当前规则。",
                        "repair_targets": [
                            {
                                "section_id": "a",
                                "component": component,
                                "action": "update",
                                "visual_kind": "",
                            }
                        ],
                    }
                )
            )

    result = await engine_for(store, inputs, DifferentComponents()).bounded_drill_down(
        issue_page_context(), DrillDown
    )
    assert result.needs_confirmation and not result.confirmed
    assert "disagree on the repair" in result.reason
