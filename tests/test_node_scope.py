"""Chapter scope must not turn one research group's gaps into every writer's work."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from decision_wire import payload as decision_payload
from native_calls import action_call, native_call
from test_material_selection import relevance_response, runtime_for

from folio.models import (
    DocumentPlan,
    Evidence,
    JobInput,
    LocalReview,
    Review,
    Section,
    SectionContent,
)
from folio.orchestration.context import project
from folio.orchestration.engine import Engine
from folio.orchestration.work import WorkPlan, WorkState, WorkTask
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


@pytest.mark.parametrize("operation", ["generate", "repair"])
def test_parent_child_view_preserves_all_child_writing_obligations(operation):
    child = {
        "section_id": "settlement",
        "title": "清算",
        "display_number": "4.2",
        "level": 2,
        "parent_id": "functional",
        "heading_path": ["功能", "清算"],
        "instruction": "逐项描述失败处理；绘制审批流程图，保留审批拒绝分支。",
        "content_mode": "generate",
        "expected_table_headers": ["场景", "结果"],
        "required_visual_kinds": ["flowchart"],
        "diagnostic_note": "内部诊断，不是业务写作要求。",
    }
    source = {"section": {"section_id": "functional"}, "child_sections": [child]}
    before = deepcopy(source)
    view = project(operation, source)
    assert source == before
    assert view["child_sections"] == [
        {
            key: value
            for key, value in child.items()
            if key != "diagnostic_note"
        }
    ]
    assert view["section"] == source["section"]
    assert view["writing_scope"]["role"] == "parent_overview_and_shared_rules"
    assert project("validate", source)["child_sections"] == [child]


@pytest.mark.parametrize("operation", ["understand_work", "next_action", "query"])
def test_research_scope_keeps_actual_sections_when_derived_task_summary_is_incomplete(operation):
    source = {
        "work_task": {"title": "附录", "questions": ["参考资料有哪些？"]},
        "sections": [
            {"section_id": "volume", "instruction": "估算业务量，并说明计算口径。"},
            {"section_id": "security", "instruction": "说明本项目适用的安全约束。"},
            {"section_id": "appendix", "instruction": "列明实际引用的参考资料。"},
        ],
        "writing_gaps": ["业务量的估算依据是什么？"],
    }
    before = deepcopy(source)
    view = project(operation, source)
    assert source == before
    assert view["sections"] == source["sections"]
    assert view["writing_gaps"] == source["writing_gaps"]
    assert view["research_scope"]["responsibility_source"] == "sections.instruction"
    assert view["research_scope"]["derived_task_is_navigation"] is True


def test_parent_selection_and_writer_share_child_responsibilities(store, inputs):
    engine = engine_for(store, inputs, DemoRuntime())
    source = {
        "section": {"section_id": "functions", "instruction": "详细说明各模块功能。"},
        "child_sections": [
            {
                "section_id": "approval",
                "title": "审批",
                "instruction": "列明通过与拒绝分支并附流程图。",
                "expected_table_headers": ["条件", "结果"],
            }
        ],
    }
    projected = project("generate", source)
    purpose = engine.writing_purpose(projected)
    assert purpose["writing_scope"] == projected["writing_scope"]
    assert purpose["child_sections"] == projected["child_sections"]
    # Individual supplements must remain scoped to their exact item, not to the
    # entire hierarchy; absence of children preserves ordinary leaf selection.
    integration = engine.writing_purpose(
        projected | {"integration_item": {"gap_id": "gap1", "content": "审批拒绝时保留原因。"}}
    )
    assert integration["action"] == "integrate_one_supplement"
    assert "child_sections" not in integration
    assert "writing_scope" not in engine.writing_purpose({"section": source["section"]})


def engine_for(store, inputs, runtime):
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    return engine


def original(evidence_id, text):
    return {
        "evidence_id": evidence_id,
        "source_id": "business-rules",
        "source_version": "v3",
        "locator": evidence_id,
        "text": text,
        "metadata": {"heading_path": ["数据需求"], "ingestion_debug": "archive-only"},
    }


@pytest.fixture
def grouped_source():
    return {
        "section": {
            "section_id": "register",
            "title": "业务登记簿",
            "heading_path": ["数据需求", "业务登记簿"],
            "instruction": "描述业务登记簿清单及要素。",
        },
        "work_task": {
            "task_id": "data-and-nonfunctional",
            "title": "数据类与非功能需求",
            "section_ids": ["register", "availability"],
            "objective": "完整描述登记簿、可用性、灾难恢复及服务时间。",
            "questions": ["登记簿有哪些要素？", "灾难恢复时间是多少？"],
            "completion_criteria": ["列出登记簿要素", "明确 RTO 和备用机房"],
        },
        "business_record": {
            "summary": "本组还须约定备用机房、灾难恢复时间和服务时间。",
            "terminology": ["登记簿：记载业务办理及核准记录", "RTO：恢复时间目标"],
            "facts": [
                {
                    "fact_id": "register-negative",
                    "statement": "未核准的登记记录不得计入清算登记簿。",
                    "evidence_ids": ["negative-original"],
                },
                {
                    "fact_id": "availability-fact",
                    "statement": "备用机房应支持业务恢复。",
                    "evidence_ids": ["unrelated-original"],
                },
            ],
            "conflicts": [
                "登记簿记账时点的两份规则有冲突，不能自行认定未核准即可记账。",
                "两份运维资料对备用机房位置有不同说明。",
            ],
            "open_questions": ["登记簿是否还应包含核准人？", "备用机房的恢复时间待确认。"],
        },
        "known_gaps": ["登记簿撤销标记的取值待确认。", "每日服务时间待确认。"],
        "model_proposals": [
            {
                "gap_id": "register-proposal",
                "section_ids": ["register"],
                "content": "登记簿中应记录核准人和核准日期，并标记撤销状态。",
            },
            {
                "gap_id": "availability-proposal",
                "section_ids": ["availability"],
                "content": "备用机房应设置业务恢复时间目标。",
            },
        ],
        "evidence": [
            original("body-original", "登记簿包含登记编号、办理日期、币种和金额。"),
            original("negative-original", "未核准的登记记录不得计入清算登记簿。"),
            original("mandatory-original", "本项目登记记录必须保留核准状态。"),
            original("unrelated-original", "备用机房应支持业务恢复。"),
        ],
        "required_project_evidence_ids": ["mandatory-original"],
        "global_facts": [],
        "dependencies": [],
    }


@pytest.mark.parametrize("operation", ["generate", "repair"])
async def test_writer_uses_strict_node_selection_and_keeps_original_risks(
    store, inputs, tmp_path, monkeypatch, grouped_source, operation
):
    source = deepcopy(grouped_source)
    if operation == "repair":
        source.update(
            current={"blocks": [], "pending_items": ["登记簿是否还应包含核准人？"]},
            repair_instruction="纠正登记簿章节的适用范围，保留未核准不得记账的约束。",
        )
    before = deepcopy(source)
    record = source["business_record"]
    # Deliberately do not select negative-original directly. A selected negative
    # fact must bring its complete original back, not only the fact's summary.
    accepted = [
        record["facts"][0],
        record["conflicts"][0],
        record["open_questions"][0],
        record["terminology"][0],
        source["known_gaps"][0],
    ]
    pages, writes = [], []

    def handler(request):
        assert request.url.host == "judge.test", "No primary-model fallback is allowed"
        payload = decision_payload(request)
        pages.append(payload["state"])

        def probability(item):
            value = item["value"]
            return (
                0.95
                if value in accepted
                or (isinstance(value, dict) and value.get("evidence_id") == "body-original")
                else 0.8
            )

        return relevance_response(payload, probability)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    actual_complete = runtime.complete

    async def complete(task, context, schema, session):
        if task == operation:
            writes.append(deepcopy(context))
            return RuntimeResult('{"blocks": []}')
        assert task == "select_context"
        return await actual_complete(task, context, schema, session)

    async def measure(*_args, **_kwargs):
        return {"characters": 100, "fits": True}

    runtime.complete = complete
    runtime.measure = measure
    engine = engine_for(store, inputs, runtime)
    try:
        await engine.call(operation, source, SectionContent)
        assert pages and len(writes) == 1
        view = writes[0]
        assert view["work_task"] == {key: source["work_task"][key] for key in ("task_id", "title")}
        assert view["section"] == source["section"]
        assert view["model_proposals"] == [source["model_proposals"][0]]
        assert view["known_gaps"] == [source["known_gaps"][0]]
        assert not view["business_record"].get("summary")
        assert {
            field: view["business_record"][field]
            for field in ("facts", "conflicts", "open_questions", "terminology")
        } == {
            **{
                field: [record[field][0]]
                for field in ("facts", "conflicts", "open_questions", "terminology")
            },
        }
        assert {item["evidence_id"] for item in view["evidence"]} == {
            "body-original",
            "negative-original",
            "mandatory-original",
        }
        for item in view["evidence"]:
            archived = next(
                e for e in source["evidence"] if e["evidence_id"] == item["evidence_id"]
            )
            assert item["text"] == archived["text"]
            assert item["metadata"]["heading_path"] == archived["metadata"]["heading_path"]
        for page in pages:
            purpose = page["purpose"]
            assert purpose["section"] == source["section"]
            assert "work_task" not in purpose
            assert "灾难恢复时间" not in json.dumps(purpose, ensure_ascii=False)
            if operation == "repair":
                assert purpose["repair_instruction"] == source["repair_instruction"]
        catalog = [item for page in pages for item in page["catalog"]]
        for value in record["conflicts"] + record["open_questions"] + source["known_gaps"]:
            assert any(item["kind"] == "risk" and item["value"] == value for item in catalog)
        assert source == before
        archives = (store.root / "jobs" / engine.job_id / "contexts" / "sources").glob("*.json")
        assert any(json.loads(path.read_text())["context"] == source for path in archives)
        counts = len(pages), len(writes)
        await engine.call(operation, source, SectionContent)
        assert (len(pages), len(writes)) == counts
    finally:
        await runtime.aclose()


class CaptureReview(DemoRuntime):
    def __init__(self):
        self.calls = []

    async def complete(self, task, context, schema, session):
        self.calls.append((task, deepcopy(context)))
        assert task in {"validate", "review_work"}
        value = {"findings": []}
        if task == "validate":
            value["verdicts"] = [
                {"claim_id": claim["claim_id"], "supported": True, "reason": "符合原始规则。"}
                for claim in context["extraction"]["claims"]
            ]
        return RuntimeResult(json.dumps(value))


async def test_validator_preserves_facts_conflicts_originals_and_local_pending_only(
    store, inputs, grouped_source
):
    runtime = CaptureReview()
    engine = engine_for(store, inputs, runtime)
    source = {
        "section": grouped_source["section"],
        "content": {
            "blocks": [
                {"block_id": "body", "text": "登记簿要素。", "evidence_ids": ["body-original"]},
                {
                    "block_id": "proposal",
                    "text": "未经核准可直接记账。",
                    "origin": "model_proposal",
                    "evidence_ids": [],
                },
            ],
            "pending_items": ["登记簿是否还应包含核准人？"],
        },
        "extraction": {"claims": [{"claim_id": "c", "evidence_ids": ["negative-original"]}]},
        "evidence": grouped_source["evidence"],
        "required_project_evidence_ids": ["mandatory-original"],
        "business_context": {
            key: grouped_source[key]
            for key in ("work_task", "business_record", "model_proposals", "dependencies")
        },
    }
    before = deepcopy(source)
    await engine.call("validate", source, LocalReview)
    assert len(runtime.calls) == 1
    task, view = runtime.calls[0]
    assert task == "validate"
    shared = view["business_context"]
    assert shared["work_task"] == {
        "task_id": "data-and-nonfunctional",
        "title": "数据类与非功能需求",
    }
    assert "open_questions" not in shared["business_record"]
    for field in ("facts", "conflicts"):
        assert shared["business_record"][field] == grouped_source["business_record"][field]
    assert shared["model_proposals"] == [grouped_source["model_proposals"][0]]
    assert view["content"] == source["content"]  # Red proposal is still independently checked.
    assert {e["evidence_id"]: e["text"] for e in view["evidence"]} == {
        e["evidence_id"]: e["text"] for e in source["evidence"][:3]
    }
    assert source == before


async def test_group_review_still_receives_complete_research_goals_and_gaps(
    store, inputs, grouped_source
):
    runtime = CaptureReview()
    engine = engine_for(store, inputs, runtime)
    source = deepcopy(grouped_source)
    source.update(
        review_scope="task_consistency",
        sections=[
            {"section_id": sid, "content": {"blocks": []}}
            for sid in source["work_task"]["section_ids"]
        ],
    )
    before = deepcopy(source)
    await engine.call("review_work", source, Review)
    assert len(runtime.calls) == 1
    task, view = runtime.calls[0]
    assert task == "review_work"
    for field in ("work_task", "business_record", "known_gaps", "model_proposals"):
        assert view[field] == source[field]
    assert source == before


@pytest.mark.parametrize("find_original", [False, True])
async def test_empty_node_researches_before_full_write_and_preserves_table_and_figure(
    store, inputs, find_original
):
    searches, calls, writes, completed_at = [], [], [], []
    fresh = Evidence.model_validate(
        original("fresh-register", "登记簿要素。登记编号必填。录入登记记录后先核准，再记账。")
    )

    class Provider:
        async def search(self, queries, **_kwargs):
            assert not writes, "The writer must wait until research supplies its latest context"
            searches.append(queries)
            return [fresh] if find_original and len(searches) == 3 else []

    class Writer(DemoRuntime):
        async def complete(self, operation, context, schema, session):
            calls.append(operation)
            if operation == "select_context":
                refs = [
                    item["context_ref"]
                    for item in context["catalog"]
                    if item["kind"] != "risk"
                    and isinstance(item["value"], dict)
                    and (
                        item["value"].get("evidence_id") == fresh.evidence_id
                        or fresh.evidence_id in item["value"].get("evidence_ids", [])
                    )
                ]
                return RuntimeResult(json.dumps({"selected_refs": refs}))
            if operation == "next_action":
                return action_call("search" if context["search_count"] < 3 else "pending",
                                   summary="核对本节登记簿资料。", missing_information=[])
            if operation == "query":
                return native_call("search_sources", {"queries": [f"登记簿要素 第{context['search_count'] + 1}次检索"]})
            if operation == "complete_gaps":
                completed_at.append(len(searches))
                assert len(searches) == 3, "No proposal completion before all three KB searches"
            if operation == "generate":
                writes.append(deepcopy(context))
                assert len(searches) == 3
                assert context["model_completion_allowed"] is True
                assert context["model_proposals"]
                assert context["section"]["expected_table_headers"] == ["要素", "校验规则"]
                assert context["required_visual_kinds"] == ("flowchart",)
                refs = [fresh.evidence_id] if find_original else []
                origin = "evidence" if find_original else "model_proposal"
                return RuntimeResult(
                    json.dumps(
                        {
                            "blocks": [
                                {
                                    "block_id": "register-table",
                                    "kind": "table",
                                    "text": "登记簿要素",
                                    "rows": [["要素", "校验规则"], ["登记编号", "必填"]],
                                    "origin": origin,
                                    "evidence_ids": refs,
                                },
                                {
                                    "block_id": "register-flow",
                                    "kind": "paragraph",
                                    "text": "录入登记记录后先核准，再记账。",
                                    "origin": origin,
                                    "evidence_ids": refs,
                                },
                            ],
                            "visuals": [
                                {
                                    "figure_id": "register-chart",
                                    "kind": "flowchart",
                                    "caption": "登记簿核准流程",
                                    "description": "录入、核准、记账。",
                                    "source_block_ids": ["register-flow"],
                                    "after_block_id": "register-flow",
                                }
                            ],
                        }
                    )
                )
            assert operation != "integrate_supplement", "An empty first draft needs a full writer"
            return await super().complete(operation, context, schema, session)

    engine = engine_for(store, inputs, Writer())
    section = Section(
        section_id="register",
        node_id="register",
        title="业务登记簿",
        level=1,
        display_number="1",
        heading_path=["业务登记簿"],
        instruction="描述登记簿清单及要素，提供核准流程图。",
        expected_table_headers=["要素", "校验规则"],
    )
    task = WorkTask(
        task_id="data",
        title="数据要求",
        objective="登记簿和其他数据要求",
        section_ids=[section.section_id],
        questions=["登记簿有哪些要素？"],
        completion_criteria=["登记簿要素齐全"],
        rationale="共用资料。",
    )
    engine.plan = DocumentPlan(sections=[section])
    engine.work_plan = WorkPlan(tasks=[task])
    engine.references = SimpleNamespace(evidence=lambda *_: [], comments=lambda *_: [])
    engine.writing_profile = {}
    engine.provider = Provider()
    unrelated = Evidence.model_validate(original("unrelated", "备用机房用于业务恢复。"))
    store.save_work_state(
        engine.job_id,
        WorkState(
            task_id=task.task_id,
            evidence=[unrelated],
            writable=True,
        ),
    )

    result = await engine.generate(section)

    assert len(searches) == 3 and completed_at == [3]
    assert len(writes) == 1
    assert calls.count("extract") == calls.count("validate") == 1
    assert "integrate_supplement" not in calls
    assert {e["evidence_id"] for e in writes[0]["evidence"]} == (
        {fresh.evidence_id} if find_original else set()
    )
    assert writes[0]["known_gaps"] == []  # Completed gaps were refreshed before the writer.
    assert any(
        block.kind == "table" and block.rows[0] == section.expected_table_headers
        for block in result.content.blocks
    )
    assert result.content.visuals[0].kind == "flowchart"
    assert result.content.visuals[0].source_block_ids == ["register-flow"]
    assert not result.local_findings
    assert unrelated in result.evidence  # Selection never destroys the research archive.
    if find_original:
        assert fresh in result.evidence
    saved = store.work_states(engine.job_id)[task.task_id]
    assert len(saved.searches) == 3 and saved.proposals
    assert all(proposal.section_ids == [section.section_id] for proposal in saved.proposals)
