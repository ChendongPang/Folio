from collections import Counter
from unittest.mock import AsyncMock

import pytest
from test_business_work import document, task
from test_parallel_review import material

from folio.models import (
    Block,
    Claim,
    DrillDown,
    Extraction,
    Finding,
    JobInput,
    SectionContent,
    SectionState,
)
from folio.orchestration.engine import Engine
from folio.orchestration.review import review_groups
from folio.orchestration.review_candidates import deduplicate_review_findings
from folio.orchestration.work import WorkPlan
from folio.runtime.demo import DemoRuntime


def many_sections(count):
    plan = document(tuple(f"s{i}" for i in range(count)))
    work = WorkPlan(tasks=[task(node.section_id, [node.section_id]) for node in plan.sections])
    states = [
        SectionState(
            section_id=node.section_id,
            content=SectionContent(
                blocks=[Block(block_id="b", text="应支持查询", origin="model_proposal")]
            ),
            extraction=Extraction(summary=node.title),
            evidence=[],
        )
        for node in plan.sections
    ]
    return plan, work, states


def test_common_entity_grows_linearly_without_pairwise_explosion():
    plan, work, states = many_sections(40)
    for state in states:
        state.extraction.entities = ["虚拟交易", "系统", "交易"]
    groups = review_groups(plan, work, states, 6)
    assert len(groups) == 8
    assert {s.section_id for group in groups for s in group.sections} == {
        s.section_id for s in states
    }
    assert max(len(group.sections) for group in groups) <= 6
    assert all(any("虚拟交易" in reason for reason in group.reasons) for group in groups)


def test_existing_window_covers_smaller_topic_relationship_without_extra_request():
    plan, work, states = many_sections(12)
    baseline = review_groups(plan, work, states, 6)
    for index in (0, 2, 4):
        states[index].extraction.entities = ["审批规则"]
    groups = review_groups(plan, work, states, 6)
    assert len(groups) == len(baseline)
    assert any("审批规则" in reason for reason in groups[0].reasons)


def test_distant_conflicting_facts_keep_a_shared_scope_review():
    plan, work, states = many_sections(20)
    for index, value in ((0, "100万元"), (19, "200万元")):
        states[index].extraction.claims = [
            Claim(
                claim_id="limit",
                block_ids=["b"],
                subject="交易限额",
                predicate="上限",
                value=value,
                scope="同业业务",
                cardinality="single",
                statement="限额为" + value,
            )
        ]
    groups = review_groups(plan, work, states, 6)
    assert any(
        {"s0", "s19"} <= {s.section_id for s in group.sections}
        and any("same fact" in reason for reason in group.reasons)
        for group in groups
    )


def fact(state, value, *, scope="同业业务"):
    state.extraction.claims = [
        Claim(
            claim_id="limit",
            block_ids=["b"],
            subject="交易限额",
            predicate="上限",
            value=value,
            scope=scope,
            statement="交易限额上限为" + value,
            status="candidate",
            cardinality="unspecified",
        )
    ]


def value_groups(groups):
    return [group for group in groups if any("Cross-check different values" in r for r in group.reasons)]


def test_candidate_values_cross_windows_without_relying_on_a_broad_bridge():
    plan, work, states = many_sections(7)
    for state, value in zip(states, ["100"] * 3 + ["100或200"] + ["200"] * 3, strict=True):
        fact(state, value)
    before = [state.model_dump() for state in states]
    groups = review_groups(plan, work, states, 4)
    assert any(
        {"100", "200"} <= {s.extraction.claims[0].value for s in group.sections}
        for group in value_groups(groups)
    )
    assert all(len(group.sections) <= 4 for group in groups)
    assert [state.model_dump() for state in states] == before


def test_repeated_fact_values_do_not_create_a_pair_for_every_copy():
    plan, work, states = many_sections(40)
    for i, state in enumerate(states):
        fact(state, "100" if i < 20 else "200")
    groups = review_groups(plan, work, states, 6)
    assert len(groups) <= 9
    assert len(value_groups(groups)) == 1
    assert any(
        {"100", "200"} <= {s.extraction.claims[0].value for s in group.sections}
        for group in value_groups(groups)
    )


def test_all_distinct_values_are_preserved_in_one_full_scope_investigation():
    from folio.orchestration.rule_conflicts import wide_fact_candidates

    plan, work, states = many_sections(40)
    for i, state in enumerate(states):
        fact(state, str(i))
    groups = review_groups(plan, work, states, 4)
    keys, findings = wide_fact_candidates(states, 4)
    assert keys == {states[0].extraction.claims[0].key}
    assert len(findings) == 1
    assert set(findings[0].section_ids) == {s.section_id for s in states}
    assert len(groups) <= 14
    assert all(len(group.sections) <= 4 for group in groups)


def test_different_fact_scopes_do_not_create_value_comparison_groups():
    plan, work, states = many_sections(10)
    fact(states[0], "100", scope="一期")
    fact(states[-1], "200", scope="二期")
    assert not value_groups(review_groups(plan, work, states, 4))


@pytest.mark.parametrize("alternate_value", ["100", "200"])
def test_value_representative_uses_another_owner_when_first_members_share_a_task(alternate_value):
    plan, work, states = many_sections(7)
    work.tasks = [task("same-owner", ["s0", "s1"]), *work.tasks[2:]]
    fact(states[0], "100")
    fact(states[1], "200")
    fact(states[-1], alternate_value)
    groups = review_groups(plan, work, states, 2)
    opposite = "s1" if alternate_value == "100" else "s0"
    assert any(
        {opposite, "s6"} <= {s.section_id for s in group.sections} for group in value_groups(groups)
    )


def test_value_text_is_not_normalized_into_an_unchecked_equivalence():
    plan, work, states = many_sections(7)
    fact(states[0], "A B")
    fact(states[-1], "AB")
    assert any(
        {"s0", "s6"} <= {s.section_id for s in group.sections}
        for group in value_groups(review_groups(plan, work, states, 4))
    )


def test_asymmetric_dependency_covers_all_sections_with_opposite_task():
    plan, _, states = many_sections(13)
    work = WorkPlan(
        tasks=[
            task("up", [f"s{i}" for i in range(10)]),
            task("other", ["s10", "s11"]),
            task("down", ["s12"]).model_copy(update={"depends_on": ["up"]}),
        ]
    )
    groups = review_groups(plan, work, states, 4)
    dependency = [group for group in groups if any("Business dependency" in r for r in group.reasons)]
    assert {s.section_id for g in dependency for s in g.sections} >= {
        f"s{i}" for i in range(10)
    } | {"s12"}
    assert all("s12" in {s.section_id for s in g.sections} for g in dependency)


def citation():
    _, _, states = material()
    states[0].content.blocks[0].text = "建议系统支持按交易对手筛选，并允许导出查询结果。"
    finding = Finding(
        kind="unsupported",
        rule="citation_required",
        key="missing-source",
        description="拟议业务设计缺少来源引用",
        section_ids=["a"],
        block_refs=[{"section_id": "a", "block_id": "p"}],
    )
    return finding, states


@pytest.mark.parametrize(
    "text",
    [
        "建议系统支持按交易对手筛选，并允许导出查询结果。",
        "本模块由张三负责，应支持查询。",
        "我行现有三套核心系统，应支持汇总查询。",
        "系统已于2026年10月获得监管批准",
        "系统已获批准上线并支持交易",
        "根据金融监管条例要求，应留存五年",
        "应遵循《金融监管办法》",
        "审核人：张三，系统应支持权限控制",
        "系统现行生产规范要求采用双活",
        "系统应于2026-10-10上线",
        "实测系统支持每秒十万次交易",
        "系统应限制境外机构回购余额不高于持债余额的100%，并校验合规性",
    ],
)
async def test_citation_candidates_always_receive_independent_adjudication(store, inputs, text):
    finding, states = citation()
    states[0].content.blocks[0].text = text
    before = [state.model_dump() for state in states]
    engine = Engine(store, DemoRuntime())
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan, engine.work_plan, _ = material()
    engine.business_context = lambda task: {}
    engine.call = AsyncMock(
        return_value=DrillDown(
            confirmed=False,
            needs_confirmation=True,
            reason="原始依据不足，保留待确认",
        )
    )
    results = await engine.adjudicate_findings([finding], states)
    engine.call.assert_awaited_once()
    assert engine.call.call_args.args[0] == "drill_down"
    assert engine.call.call_args.args[1]["finding"] == finding.model_dump()
    assert results[0][1].needs_confirmation and results[0][2] is None
    assert [state.model_dump() for state in states] == before


def test_exact_duplicates_are_audited_but_distinct_same_key_issues_survive():
    first, _ = citation()
    duplicate = first.model_copy(
        update={"key": "alternate-key", "description": "拟议业务设计 缺少来源引用"}
    )
    different = first.model_copy(update={"description": "该字段约束需要核对"})
    wrong_block = first.model_copy(deep=True)
    wrong_block.block_refs[0].block_id = "other"
    result = deduplicate_review_findings([first, duplicate, different, wrong_block])
    assert len(result.findings) == 3
    assert len({finding.fingerprint for finding in result.findings}) == 3
    assert Counter(record["action"] for record in result.audit) == {
        "duplicate": 1,
        "distinct_issue": 1,
    }
    assert result.audit[0]["original"] == duplicate.model_dump()
