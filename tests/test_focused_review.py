import json

import pytest
from test_request_context import engine_for, evidence

from folio.models import Review
from folio.orchestration.focused_review import (
    ReviewFocusPlan,
    ReviewMaterialRead,
    material_parts,
    review_materials,
    validate_review_result,
)
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


async def test_small_drill_down_reads_complete_sources_and_risks_without_selection(store, inputs):
    from folio.models import DrillDown

    class CompleteSources(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def complete(self, task, context, schema, session):
            self.calls.append((task, context))
            assert task == "drill_down"
            return RuntimeResult(json.dumps({"confirmed": False, "reason": "原文核对一致。"}))

    runtime = CompleteSources()
    engine = engine_for(store, inputs, runtime)
    source = {
        "finding": {
            "kind": "conflict",
            "description": "结算规则冲突",
            "section_ids": ["a"],
            "block_refs": [{"section_id": "a", "block_id": "b1"}],
            "evidence_ids": ["e1"],
            "key": "settlement",
        },
        "sections": [
            {
                "section_id": "a",
                "content": {
                    "blocks": [
                        {"block_id": "b1", "text": "虚拟交易不触发结算", "evidence_ids": ["e1"]}
                    ]
                },
                "evidence": [evidence(1), evidence(99, "未引用的反例及否定条件仍必须完整读取。")],
            }
        ],
        "business_contexts": [
            {
                "dependencies": [
                    {
                        "task_id": "upstream",
                        "findings": [{"description": "审批条件尚有矛盾"}],
                        "sections": [
                            {"section_id": "up", "findings": [{"description": "上游角色未确认"}]}
                        ],
                    }
                ]
            }
        ],
    }
    before = json.dumps(source, sort_keys=True)
    result = await engine.call("drill_down", source, DrillDown)
    assert not result.confirmed
    assert [task for task, _ in runtime.calls] == ["drill_down"]
    view = runtime.calls[0][1]
    assert {
        m["value"]["evidence_id"] for m in view["original_materials"] if m["kind"] == "evidence"
    } == {"e1", "e99"}
    risks = json.dumps(
        [m for m in view["original_materials"] if m["kind"] == "risk"], ensure_ascii=False
    )
    assert "审批条件尚有矛盾" in risks and "上游角色未确认" in risks
    assert view["issue_scope"]["material_scope"] == "complete_catalog"
    assert json.dumps(source, sort_keys=True) == before


@pytest.mark.parametrize("large", [False, True])
async def test_issue_requirements_are_mandatory_and_never_sent_to_relevance_selection(
    store, inputs, large
):
    from folio.decisions.routing import DecisionRouter
    from folio.models import DrillDown

    class RequirementReview(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def measure(self, task, context, schema, max_chars):
            size = len(json.dumps(context, ensure_ascii=False))
            return {"characters": size, "fits": task != "drill_down" or size < 12000}

        async def complete(self, task, context, schema, session):
            self.calls.append((task, context))
            if task == "select_context":
                assert all(
                    item["kind"] in {"evidence", "fact", "proposal", "risk"}
                    for item in context["catalog"]
                )
                assert "审批人与发起人必须不同" not in json.dumps(context, ensure_ascii=False)
                _, build = DecisionRouter.questions(task, context)
                answers = {
                    f"item_{i}": {
                        "noul": 0.99 if item["value"].get("evidence_id") == "e1" else 0.01
                    }
                    for i, item in enumerate(context["catalog"])
                }
                return RuntimeResult(json.dumps(build(answers)))
            assert task == "drill_down"
            requirements = [
                m["value"] for m in context["original_materials"] if m["kind"] == "requirement"
            ]
            assert requirements == [{"criterion": "审批人与发起人必须不同"}]
            return RuntimeResult(
                json.dumps({"confirmed": False, "reason": "已对照任务要求和原文核查。"})
            )

    runtime = RequirementReview()
    engine = engine_for(store, inputs, runtime)
    source = {
        "finding": {
            "kind": "conflict",
            "key": "roles",
            "description": "审批角色冲突",
            "section_ids": ["a"],
        },
        "sections": [
            {
                "section_id": "a",
                "content": {
                    "blocks": [
                        {
                            "block_id": "b",
                            "text": "审批人与发起人不得相同。",
                            "evidence_ids": ["e1"],
                        }
                    ]
                },
                "evidence": [evidence(1), evidence(99, "其他不相关原文" * (4000 if large else 1))],
            }
        ],
        "business_contexts": [{"work_task": {"completion_criteria": ["审批人与发起人必须不同"]}}],
    }
    before = json.dumps(source, sort_keys=True)
    result = await engine.call("drill_down", source, DrillDown)
    assert not result.confirmed
    assert [task for task, _ in runtime.calls] == (
        ["select_context", "drill_down"] if large else ["drill_down"]
    )
    assert json.dumps(source, sort_keys=True) == before


def large_source():
    return {
        "work_task": {"title": "审批", "completion_criteria": ["发起与审批权限一致"]},
        "sections": [
            {
                "section_id": str(i),
                "content": {"blocks": [{"text": f"正文{i}", "evidence_ids": [f"e{i}"]}]},
            }
            for i in range(8)
        ],
        "definitions": [{"section_id": str(i), "title": f"业务{i}"} for i in range(8)],
        "evidence": [evidence(i, f"依据{i} " * 800) for i in range(26)],
        "business_record": {"facts": [], "conflicts": ["原始材料审批角色冲突"]},
        "dependencies": [{"task_id": "upstream", "risk_briefs": ["权限尚未确认"]}],
    }


async def test_actual_failure_shape_has_bounded_original_reads_and_no_all_pairs(store, inputs):
    class Focused(DemoRuntime):
        def __init__(self):
            self.calls = []
            self.reads = []
            self.finals = []

        async def measure(self, task, context, schema, char_limit):
            length = len(json.dumps(context, ensure_ascii=False))
            return {"characters": length, "fits": length < 16000}

        async def complete(self, task, context, schema, session):
            assert (await self.measure(task, context, schema, 0))["fits"]
            self.calls.append(task)
            if task == "plan_review":
                return RuntimeResult(
                    json.dumps(
                        {
                            "checks": [
                                {
                                    "question": "发起节点和审批节点的角色是否一致",
                                    "section_ids": ["0", "7"],
                                }
                            ]
                        }
                    )
                )
            if task == "select_context":
                # A selector can miss a cited source; backend must still include it.
                return RuntimeResult(
                    json.dumps(
                        {
                            "selected_refs": [],
                            "risk_refs": [
                                x["context_ref"] for x in context["catalog"] if x["kind"] == "risk"
                            ],
                            "risk_summary": "相关风险"
                            if any(x["kind"] == "risk" for x in context["catalog"])
                            else "",
                        }
                    )
                )
            if task == "read_review_materials":
                self.reads.extend(context["catalog"])
            if task == "review_focus":
                self.reads.extend(context.get("original_materials", []))
                assert (
                    not {"evidence", "business_record", "dependencies", "definitions"}
                    & context.keys()
                )
                self.finals.append(context)
                assert context["body_representation"] == "original"
                assert all(s["content"]["blocks"] for s in context["sections"])
                assert not any(n["source_kind"] == "body" for n in context["material_notes"])
            return await super().complete(task, context, schema, session)

    runtime = Focused()
    engine = engine_for(store, inputs, runtime)
    source = large_source()
    original = json.dumps(source, sort_keys=True)
    result = await engine.call("review_work", source, Review)
    assert result.findings == []
    assert len(runtime.finals) == 9  # 8 nodes + one meaningful relation, not 28 pairs.
    read_evidence = {x["value"]["evidence_id"] for x in runtime.reads if x["kind"] == "evidence"}
    assert read_evidence == {f"e{i}" for i in range(8)}
    assert any(x["kind"] == "risk" for x in runtime.reads)
    assert json.dumps(source, sort_keys=True) == original
    count = len(runtime.calls)
    assert (await engine.call("review_work", source, Review)).findings == []
    assert len(runtime.calls) == count
    root = store.root / "jobs" / engine.job_id / "contexts"
    assert list((root / "review_reads").glob("*.json"))
    for path in (root / "review_read_results").glob("*.json"):
        saved = json.loads(path.read_text())
        assert saved["context"]["reading_complete"]
        assert saved["pages"] or saved.get("mode") == "direct_originals"


def test_single_large_original_is_split_losslessly_with_stable_source_offsets():
    item = {
        "context_ref": "source",
        "kind": "body",
        "value": {"section_id": "a", "rows": [["头1", "头2"], ["很长的表格" * 3000, "最后一格"]]},
    }
    assert material_parts(item) == [item]  # The default budget retains this complete source.
    parts = material_parts(item, size=6000)
    assert len(parts) > 1
    raw = json.dumps(item["value"], ensure_ascii=False, sort_keys=True)
    assert "".join(p["value"]["serialized_fragment"] for p in parts) == raw
    assert [p["value"]["start"] for p in parts] == list(range(0, len(raw), 6000))
    assert all(p["kind"] == "body" for p in parts)


def test_note_cannot_invent_source_promote_proposal_or_silently_drop_body():
    context = {
        "catalog": [
            {"context_ref": "body", "kind": "body"},
            {"context_ref": "proposal", "kind": "proposal"},
        ]
    }
    for notes in (
        [],
        [{"source_refs": ["invented"], "source_kind": "body", "text": "假引用"}],
        [{"source_refs": ["proposal"], "source_kind": "evidence", "text": "错误升格"}],
    ):
        with pytest.raises(ValueError):
            validate_review_result(ReviewMaterialRead(notes=notes), context)
    validate_review_result(
        ReviewMaterialRead(
            notes=[
                {"source_refs": ["body"], "source_kind": "body", "text": "正文要求审批"},
                {"source_refs": ["proposal"], "source_kind": "proposal", "text": "未确认方案"},
            ]
        ),
        context,
    )
    with pytest.raises(ValueError):
        validate_review_result(
            ReviewFocusPlan(checks=[{"question": "问题", "section_ids": ["unknown"]}]),
            {"sections": [{"section_id": "body"}]},
        )


async def test_oversized_notes_are_explicitly_incomplete_not_a_pass(store, inputs):
    class NoSpace(DemoRuntime):
        async def measure(self, task, context, schema, char_limit):
            return {"characters": 1, "fits": task not in {"review_work", "review_focus"}}

    engine = engine_for(store, inputs, NoSpace())
    result = await engine.call("review_work", large_source(), Review)
    assert len(result.findings) == 8
    assert all(f.kind == "pending" and "incomplete" in f.description for f in result.findings)


def test_catalog_keeps_all_risk_types_and_distinguishes_raw_sources():
    source = large_source()
    source["known_gaps"] = ["清算时限待确认"]
    source["model_proposals"] = [{"gap_id": "g", "section_ids": ["0"], "content": "拟定方案"}]
    materials = review_materials(source)
    assert {"evidence", "body", "risk", "requirement", "proposal"} <= {m["kind"] for m in materials}
    assert any(m["value"].get("unfilled_gap") == "清算时限待确认" for m in materials)


async def test_issue_drill_down_keeps_originals_but_drops_task_peers(store, inputs):
    class Selector(DemoRuntime):
        async def complete(self, task, context, schema, session):
            if task == "select_context":
                assert context["purpose"]["action"] == "drill_down"
                assert "finding" in context["purpose"]
                return RuntimeResult('{"selected_refs":[],"risk_refs":[],"risk_summary":""}')
            return await super().complete(task, context, schema, session)

    engine = engine_for(store, inputs, Selector())
    source = {
        "finding": {
            "kind": "conflict",
            "description": "审批角色错误",
            "section_ids": ["a"],
            "key": "approval",
        },
        "sections": [
            {
                "section_id": "a",
                "content": {"blocks": [{"text": "原始正文", "evidence_ids": ["e1"]}]},
                "evidence": [evidence(1), evidence(2)],
                "extraction": {"summary": "重复摘要"},
            }
        ],
        "business_contexts": [
            {
                "peer_sections": [{"content": "其他节点正文" * 1000}],
                "business_record": {"facts": []},
                "dependencies": [],
            }
        ],
        "definitions": [{"section_id": "a", "title": "审批"}],
    }
    view = await engine.prepare_context("drill_down", source)
    assert "business_contexts" not in view
    assert "extraction" not in view["sections"][0] and "evidence" not in view["sections"][0]
    assert view["sections"][0]["content"] == source["sections"][0]["content"]
    assert [m["value"]["evidence_id"] for m in view["original_materials"]] == ["e1"]


def test_review_source_kind_is_fixed_by_current_page_schema():
    from folio.orchestration.focused_review import review_schema

    schema = review_schema(
        ReviewMaterialRead,
        ReviewMaterialRead.model_json_schema(),
        {"catalog": [{"context_ref": "x", "kind": "body"}]},
    )
    assert schema["$defs"]["ReviewNote"]["properties"]["source_kind"]["enum"] == ["body"]
    assert schema["$defs"]["ReviewNote"]["properties"]["source_refs"]["items"]["enum"] == ["x"]
    assert schema["properties"]["notes"]["minItems"] == 1


async def test_single_source_output_truncation_gets_one_changed_bounded_retry(store, inputs):
    from folio.runtime.errors import ModelCallFailed

    class Truncated(DemoRuntime):
        def __init__(self):
            self.reads = []

        async def measure(self, task, context, schema, char_limit):
            return {
                "characters": 1,
                "fits": task != "review_work"
                and not (task == "review_focus" and "original_materials" in context),
            }

        async def complete(self, task, context, schema, session):
            if task == "read_review_materials":
                self.reads.append(context)
                if not context.get("reading_style"):
                    raise ModelCallFailed("Incomplete response", "length_limit")
                assert schema["properties"]["notes"]["maxItems"] == 2
            return await super().complete(task, context, schema, session)

    runtime = Truncated()
    engine = engine_for(store, inputs, runtime)
    source = {"sections": [{"section_id": "a", "content": {"blocks": [{"text": "完整原文"}]}}]}
    result = await engine.call("review_work", source, Review)
    assert not result.findings
    assert len(runtime.reads) == 2
    assert runtime.reads[0]["catalog"] == runtime.reads[1]["catalog"]
    assert runtime.reads[1]["reading_style"] == "concise"


async def test_resume_review_does_not_restart_completed_business_execution(store, inputs):
    from folio.models import JobInput
    from folio.orchestration.engine import Engine

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    first = await Engine(store, DemoRuntime()).run(job_id)
    assert first["status"] == "draft"
    before = [s.model_dump() for s in store.states(job_id)]
    store.update(job_id, status="failed", stage="review", error="interrupted review")
    store.resume_job(job_id)

    class Resume(Engine):
        async def run_business_tasks(self):
            pytest.fail("Completed writing must not restart on review recovery")

    result = await Resume(store, DemoRuntime()).run(job_id)
    assert result["status"] == "draft"
    assert before == [s.model_dump() for s in store.states(job_id)]


async def test_selecting_one_fragment_reads_the_whole_original_including_tail(store, inputs, monkeypatch):
    from functools import partial

    # Exercise recovery from a selected fragment with an explicitly small page
    # size; the application default now keeps this 20K-character source whole.
    monkeypatch.setattr("folio.orchestration.focused_review.material_parts",
                        partial(material_parts, size=6000))

    class Partial(DemoRuntime):
        def __init__(self):
            self.reads = []

        async def measure(self, task, context, schema, char_limit):
            return {
                "characters": 1,
                "fits": task != "review_work"
                and not (task == "review_focus" and "original_materials" in context),
            }

        async def complete(self, task, context, schema, session):
            if task == "select_context":
                first = next(
                    (
                        x["context_ref"]
                        for x in context["catalog"]
                        if x["context_ref"].endswith("_part0")
                    ),
                    None,
                )
                return RuntimeResult(json.dumps({"selected_refs": [first] if first else []}))
            if task == "read_review_materials":
                self.reads.extend(x for x in context["catalog"] if x["kind"] == "evidence")
            return await super().complete(task, context, schema, session)

    runtime = Partial()
    engine = engine_for(store, inputs, runtime)
    e = evidence(1, "业务说明" * 5000 + "例外：未经审批不得执行")
    source = {
        "sections": [{"section_id": "a", "content": {"blocks": [{"text": "待核正文"}]}}],
        "evidence": [e],
    }
    await engine.call("review_work", source, Review)
    from folio.orchestration.context import project

    projected = project("review_work", source)["evidence"][0]
    rebuilt = "".join(item["value"]["serialized_fragment"] for item in runtime.reads)
    assert rebuilt == json.dumps(projected, ensure_ascii=False, sort_keys=True)
    assert "例外：未经审批不得执行" in rebuilt


async def test_single_legacy_finding_does_not_force_every_section_citation(store, inputs):
    class SelectNone(DemoRuntime):
        async def complete(self, task, context, schema, session):
            if task == "select_context":
                return RuntimeResult('{"selected_refs":[],"risk_refs":[],"risk_summary":""}')
            return await super().complete(task, context, schema, session)

    engine = engine_for(store, inputs, SelectNone())
    blocks = [
        {"block_id": f"b{i}", "text": f"业务规则{i}", "evidence_ids": [f"e{i}"]} for i in range(20)
    ]
    blocks[7] = {
        "block_id": "b7_p3",
        "text": "拟支持区块链账户管理",
        "origin": "model_proposal",
        "evidence_ids": [],
    }
    source = {
        "finding": {
            "kind": "coverage",
            "description": "正文b7_p3的区块链账户管理，证据e2未提及，属模型补写。",
            "section_ids": ["a"],
            "key": "区块链功能",
        },
        "sections": [
            {
                "section_id": "a",
                "content": {"blocks": blocks},
                "evidence": [evidence(i) for i in range(20)],
            }
        ],
    }
    original = json.dumps(source, sort_keys=True)
    view = await engine.prepare_context("drill_down", source)
    assert [m["value"]["evidence_id"] for m in view["original_materials"]] == ["e2"]
    assert [b["block_id"] for b in view["sections"][0]["content"]["blocks"]] == [
        "b6",
        "b7_p3",
        "b8",
    ]
    assert view["issue_scope"]["target_blocks"] == [{"section_id": "a", "block_id": "b7_p3"}]
    assert json.dumps(source, sort_keys=True) == original


async def test_structured_finding_keeps_selected_counterevidence_and_exact_target(store, inputs):
    class SelectCounterexample(DemoRuntime):
        async def complete(self, task, context, schema, session):
            if task == "select_context":
                selected = [
                    m["context_ref"]
                    for m in context["catalog"]
                    if m["value"].get("evidence_id") == "e99"
                ]
                return RuntimeResult(
                    json.dumps({"selected_refs": selected, "risk_refs": [], "risk_summary": ""})
                )
            return await super().complete(task, context, schema, session)

    engine = engine_for(store, inputs, SelectCounterexample())
    source = {
        "finding": {
            "kind": "conflict",
            "description": "b10的结算规则错误",
            "section_ids": ["a"],
            "block_refs": [{"section_id": "a", "block_id": "b10"}],
            "evidence_ids": ["e10"],
            "rule": "evidence_conflict",
            "key": "settlement",
        },
        "sections": [
            {
                "section_id": "a",
                "content": {
                    "blocks": [
                        {"block_id": "b1", "text": "账户维护", "evidence_ids": ["e1"]},
                        {"block_id": "b10", "text": "虚拟交易触发结算", "evidence_ids": ["e10"]},
                    ]
                },
                "evidence": [evidence(1), evidence(10), evidence(99, "虚拟交易不触发清算核算")],
            }
        ],
    }
    view = await engine.prepare_context("drill_down", source)
    assert {m["value"]["evidence_id"] for m in view["original_materials"]} == {"e10", "e99"}
    assert view["issue_scope"]["target_blocks"] == [{"section_id": "a", "block_id": "b10"}]


def issue_page_context(count=5):
    return {
        "finding": {
            "kind": "conflict",
            "description": "结算规则冲突",
            "section_ids": ["a"],
            "block_refs": [{"section_id": "a", "block_id": "b1"}],
            "rule": "evidence_conflict",
            "key": "settlement",
        },
        "sections": [
            {
                "section_id": "a",
                "content": {"blocks": [{"block_id": "b1", "text": "虚拟交易不触发清算核算"}]},
            }
        ],
        "original_materials": [
            {
                "context_ref": f"ctx_{i}",
                "kind": "evidence",
                "value": evidence(i, f"原始资料{i}：虚拟交易不触发清算核算"),
            }
            for i in range(count)
        ],
    }


class IssuePages(DemoRuntime):
    def __init__(self, limit=2, mode="clear"):
        self.limit = limit
        self.mode = mode
        self.pages = []

    async def measure(self, task, context, schema, char_limit):
        return {
            "characters": 1,
            "fits": task != "drill_down" or len(context["original_materials"]) <= self.limit,
        }

    async def complete(self, task, context, schema, session):
        if task != "drill_down":
            return await super().complete(task, context, schema, session)
        self.pages.append(context)
        first = context["original_materials"][0]["context_ref"]
        pending = self.mode == "uncertain" and first == "ctx_0"
        confirmed = self.mode == "mixed" and first == "ctx_0"
        if self.mode == "failed" and first == "ctx_0":
            raise ModelCallFailed("truncated", "length_limit")
        return RuntimeResult(
            json.dumps(
                {
                    "confirmed": confirmed,
                    "needs_confirmation": pending,
                    "reason": "各项原文明确排除虚拟交易结算，本批与正文没有冲突。",
                    "affected_section_ids": ["a"],
                    "repair_instruction": "修复结算规则" if confirmed else "",
                }
            )
        )


async def test_bounded_issue_checks_every_original_page_and_reuses_completed_pages(store, inputs):
    from folio.models import DrillDown

    runtime = IssuePages()
    engine = engine_for(store, inputs, runtime)
    source = issue_page_context()
    result = await engine.bounded_drill_down(source, DrillDown)
    assert not result.confirmed and not result.needs_confirmation
    assert len(runtime.pages) == 3
    assert [item for page in runtime.pages for item in page["original_materials"]] == source[
        "original_materials"
    ]
    assert all(page["sections"] == source["sections"] for page in runtime.pages)
    assert all(page["reading_scope"]["mode"] == "source_page" for page in runtime.pages)
    assert all(
        "An absence of supporting evidence in this batch proves neither" in page["reading_scope"]["instruction"] for page in runtime.pages
    )
    assert not any("material_notes" in page for page in runtime.pages)
    second = await engine.bounded_drill_down(source, DrillDown)
    assert second == result
    assert len(runtime.pages) == 3
    assert list(
        (store.root / "jobs" / engine.job_id / "contexts" / "issue_read_results").glob("*.json")
    )


@pytest.mark.parametrize("mode", ["uncertain", "mixed", "failed"])
async def test_partial_or_disagreeing_issue_pages_never_dismiss_finding(store, inputs, mode):
    from folio.models import DrillDown

    runtime = IssuePages(mode=mode)
    engine = engine_for(store, inputs, runtime)
    result = await engine.bounded_drill_down(issue_page_context(), DrillDown)
    assert result.needs_confirmation
    assert not result.confirmed
    assert not result.repair_instruction
    assert len(runtime.pages) == 3


async def test_indivisible_source_keeps_qualifying_tail_and_is_not_silently_truncated(
    store, inputs
):
    from folio.models import DrillDown

    runtime = IssuePages(limit=1)
    engine = engine_for(store, inputs, runtime)
    context = issue_page_context(2)
    large = {
        "context_ref": "large",
        "kind": "evidence",
        "value": {"text": "正常交易支持结算。" * 1000 + "例外：虚拟交易不触发清算核算。"},
    }
    context["original_materials"] = material_parts(large, size=6000) + context["original_materials"]
    result = await engine.bounded_drill_down(context, DrillDown)
    assert result.needs_confirmation
    assert not runtime.pages
    # Parent fragments cannot be split between independent judgments: the final
    # negative condition must accompany the apparent positive rule.
    assert "不触发清算核算" in context["original_materials"][-3]["value"]["serialized_fragment"]


async def test_issue_page_cap_does_not_launch_partial_audit(store, inputs):
    from folio.models import DrillDown

    runtime = IssuePages(limit=1)
    engine = engine_for(store, inputs, runtime)
    result = await engine.bounded_drill_down(issue_page_context(20), DrillDown)
    assert result.needs_confirmation
    assert not runtime.pages
