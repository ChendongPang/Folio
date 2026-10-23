import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from folio.api.app import create_app
from folio.api.workspace import task_usage_summary
from folio.models import DocumentPlan, JobInput, Limits, Section
from folio.orchestration.engine import Engine
from folio.orchestration.work import WorkPlan, WorkTask, validate_work_plan
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


def task(task_id, ids, depends_on=()):
    return WorkTask(
        task_id=task_id,
        title="交易审批",
        objective="说明完整的交易审批业务",
        section_ids=ids,
        depends_on=list(depends_on),
        questions=["谁提交、如何审批？"],
        completion_criteria=["场景、输入、处理规则保持一致"],
        rationale="这些内容共享同一业务规则",
    )


def document(ids=("a", "b")):
    return DocumentPlan(
        sections=[
            Section(
                section_id=sid,
                node_id=sid,
                title=sid,
                level=i + 1,
                display_number=str(i + 1),
                instruction="生成正文",
            )
            for i, sid in enumerate(ids)
        ]
    )


@pytest.mark.parametrize(
    "tasks, error",
    [
        ([task("x", ["a"]), task("x", ["b"])], "Duplicate business task"),
        ([task("x", ["a"]), task("y", ["a", "b"])], "one writing owner"),
        ([task("x", ["a", "a"])], "Duplicate section"),
        ([task("x", ["a"], ["missing"])], "Unknown business"),
        ([task("x", ["a"], ["x"])], "cycle"),
        ([task("x", ["a"], ["y"]), task("y", ["b"], ["x"])], "cycle"),
    ],
)
def test_invalid_graph_is_rejected(tasks, error):
    with pytest.raises(ValidationError, match=error):
        WorkPlan(tasks=tasks)


@pytest.mark.parametrize("ids", [["a"], ["a", "b", "unknown"], ["a", "b", "container"]])
def test_coverage_and_container_ownership(ids):
    doc = document()
    doc.sections.append(
        Section(
            section_id="container",
            node_id="container",
            title="目录",
            level=1,
            display_number="3",
            instruction="",
            content_mode="container",
        )
    )
    with pytest.raises(ValueError, match="cover every content node"):
        validate_work_plan(WorkPlan(tasks=[task("x", ids)]), doc)


class GroupRuntime(DemoRuntime):
    def __init__(self):
        self.calls = []

    async def complete(self, name, context, schema, session):
        self.calls.append((name, context))
        if name == "work_plan":
            plan = WorkPlan(
                tasks=[task("business", [s["section_id"] for s in context["sections"]])]
            )
            return RuntimeResult(plan.model_dump_json())
        return await super().complete(name, context, schema, session)


async def test_task_shares_research_record_across_heading_depths(store, inputs):
    inputs["user_request"] = "# 外汇交易\n## 交易撤销\n### 撤销输入\n#### 撤销处理"
    runtime = GroupRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert len(job["work_plan"]["tasks"]) == 1
    assert len(job["work_plan"]["tasks"][0]["section_ids"]) == 4
    calls = [c for name, c in runtime.calls if name == "generate"]
    assert len(calls) == 4
    assert [c["section"]["level"] for c in calls] == [1, 2, 3, 4]
    assert all(c["business_record"] == calls[0]["business_record"] for c in calls)
    assert all(c["evidence"] == calls[0]["evidence"] for c in calls)
    assert all("peer_sections" not in c for c in calls)
    assert len([1 for n, _ in runtime.calls if n == "query"]) == 1
    reviews = [c for n, c in runtime.calls if n == "review_work"]
    assert len(reviews) == 1 and len(reviews[0]["sections"]) == 4
    usage = store.work_states(job_id)["business"].usage
    assert usage is not None
    assert usage.task_id == "business"
    assert usage.started_at and usage.finished_at
    assert usage.elapsed_ms >= 0
    assert usage.model_calls > 0
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens
    usage_events = [
        event["data"]["task_usage"]
        for event in store.events(job_id)
        if event["kind"] == "work_update" and event["data"].get("task_usage")
    ]
    assert usage_events[-1]["task_id"] == "business"
    assert task_usage_summary(store, job_id)[0]["title"] == "交易审批"
    assert task_usage_summary(store, job_id)[0]["total_tokens"] == 0
    assert Path(job["artifact"]).with_name("work-plan.json").exists()
    assert Path(job["artifact"]).with_name("work-records.json").exists()
    with TestClient(create_app(store.root)) as client:
        response = client.get(f"/jobs/{job_id}/work")
        assert response.status_code == 200
        state = response.json()["states"]["business"]
        assert state["record"]["facts"] and state["review_key"]


async def test_sibling_writers_run_concurrently_and_merge_new_gaps(store, inputs):
    inputs["user_request"] = "# 外汇交易\n## 交易撤销\n## 交易审批"

    class Siblings(GroupRuntime):
        active = 0
        peak = 0

        async def complete(self, name, context, schema, session):
            if name == "generate" and context["section"]["level"] == 2:
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(0.03)
                    result = await super().complete(name, context, schema, session)
                    data = json.loads(result.content)
                    data["pending_items"] = ["补充规则：" + context["section"]["section_id"]]
                    return RuntimeResult(json.dumps(data))
                finally:
                    self.active -= 1
            return await super().complete(name, context, schema, session)

    runtime = Siblings()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert runtime.peak == 2
    assert len(store.states(job_id)) == 3
    work = store.work_states(job_id)["business"]
    assert len(work.searches) == 3
    assert len({p.gap_id for p in work.proposals}) == 2
    assert not Engine.unfilled_gaps(work)


async def test_gap_wire_contract_restricts_document_and_gap_ids(store, inputs):
    import jsonschema

    Path(inputs["knowledge_path"], "rules.md").unlink()

    class GapSchema(GroupRuntime):
        checked = 0

        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "complete_gaps":
                self.checked += 1
                data = json.loads(result.content)
                jsonschema.validate(data, schema)
                original = data["proposals"][0]["section_ids"]
                data["proposals"][0]["section_ids"] = ["outside_current_nodes"]
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(data, schema)
                data["proposals"][0]["section_ids"] = original
                data["proposals"][0]["gap_id"] = "unknown_gap"
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(data, schema)
            return result

    runtime = GapSchema()
    job = await Engine(store, runtime).run(store.create(JobInput(**inputs, runtime="demo")))
    assert runtime.checked and job["snapshot"]["review_passed"]


async def test_resume_inside_task_reuses_shared_record_and_evidence(store, inputs):
    class Interrupted(GroupRuntime):
        stopped = False

        async def complete(self, name, context, schema, session):
            if name == "generate" and context["section"]["level"] == 2 and not self.stopped:
                self.stopped = True
                raise asyncio.CancelledError()
            return await super().complete(name, context, schema, session)

    runtime = Interrupted()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, runtime).run(job_id)
    record = store.work_states(job_id)["business"].record
    assert len(store.states(job_id)) == 1
    await Engine(store, runtime).run(job_id)
    assert store.work_states(job_id)["business"].record == record
    assert len([1 for n, _ in runtime.calls if n == "understand_work"]) == 1
    assert len([1 for n, _ in runtime.calls if n == "query"]) == 1


async def test_invalid_work_plan_gets_specific_retry_feedback(store, inputs):
    class Corrected(GroupRuntime):
        attempts = 0
        feedback = ""

        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                self.attempts += 1
                if self.attempts == 1:
                    return RuntimeResult(WorkPlan(tasks=[task("x", ["unknown"])]).model_dump_json())
                self.feedback = context["format_feedback"]
            return await super().complete(name, context, schema, session)

    runtime = Corrected()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await Engine(store, runtime).run(job_id)
    assert runtime.attempts == 2
    assert "missing=" in runtime.feedback and "unknown" in runtime.feedback
    assert store.work_plan(job_id).tasks[0].task_id == "business"


async def test_work_plan_repairs_keep_last_candidate_with_bounded_budget(store, inputs):
    class Corrected(GroupRuntime):
        attempts = 0

        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                self.attempts += 1
                if self.attempts > 1:
                    assert context["previous_invalid_work_plan"]["tasks"][0]["task_id"] == (
                        "bad" + str(self.attempts - 1)
                    )
                if self.attempts < 3:
                    return RuntimeResult(
                        WorkPlan(
                            tasks=[task("bad" + str(self.attempts), ["unknown"])]
                        ).model_dump_json()
                    )
            return await super().complete(name, context, schema, session)

    runtime = Corrected()
    limits = Limits(call_attempts=1, work_plan_attempts=3)
    assert limits.max_searches == 3
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=limits))
    await Engine(store, runtime).run(job_id)
    assert runtime.attempts == 3
    assert store.work_plan(job_id).tasks[0].task_id == "business"


async def test_dependencies_gate_execution_and_pass_supported_sources(store, inputs):
    class Dependencies(GroupRuntime):
        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                # Put the dependent first to prove order is determined by dependencies.
                ids = [s["section_id"] for s in context["sections"]]
                return RuntimeResult(
                    WorkPlan(
                        tasks=[task("later", [ids[1]], ["first"]), task("first", [ids[0]])]
                    ).model_dump_json()
                )
            return await super().complete(name, context, schema, session)

    runtime = Dependencies()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await Engine(store, runtime).run(job_id)
    assert not any(n == "select_work" for n, _ in runtime.calls)
    writes = [c["work_task"]["task_id"] for n, c in runtime.calls if n == "generate"]
    assert writes == ["first", "later"]
    later = next(
        c for n, c in runtime.calls if n == "generate" and c["work_task"]["task_id"] == "later"
    )
    facts = later["dependencies"][0]["sections"][0]["supported_facts"]
    assert facts
    assert set(facts[0]["evidence_ids"]) <= {e["evidence_id"] for e in later["evidence"]}


async def test_record_cannot_cite_invented_evidence(store, inputs):
    class Invented(GroupRuntime):
        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "understand_work":
                data = json.loads(result.content)
                data["facts"][0]["evidence_ids"] = ["invented"]
                return RuntimeResult(json.dumps(data))
            return result

    runtime = Invented()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(ValueError, match="unknown evidence"):
        await Engine(store, runtime).run(job_id)
    assert not any(n == "generate" for n, _ in runtime.calls)


async def test_review_dependency_change_rechecks_without_restarting_research(store, inputs):
    from unittest.mock import AsyncMock

    from folio.models import Finding, Review

    runtime = GroupRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    engine = Engine(store, runtime)
    await engine.run(job_id)
    owner = engine.work_plan.tasks[0]
    before = store.work_states(job_id)[owner.task_id].model_dump()
    state = store.states(job_id)[0]
    source = state.evidence[0].model_copy(update={"evidence_id": "upstream_new_source"})
    state.evidence.append(source)
    store.save_section(job_id, state)
    dependencies = [
        {
            "task_id": "upstream",
            "findings": [{"description": "上游待核实"}],
            "sections": [
                {
                    "section_id": "upstream_node",
                    "revision": 2,
                    "supported_facts": [{"evidence_ids": [source.evidence_id]}],
                }
            ],
        }
    ]
    engine.dependency_context = lambda _: dependencies
    engine.research_work = AsyncMock(side_effect=AssertionError("Review must not restart research"))
    issue = Finding(
        kind="pending",
        key="upstream-change",
        description="需核对上游变化",
        section_ids=owner.section_ids,
    )
    engine.call = AsyncMock(return_value=Review(findings=[issue]))

    assert issue in await engine.review_business(owner)
    context = engine.call.call_args.args[1]
    assert context["dependencies"] == dependencies
    assert source.model_dump() in context["evidence"]
    after = store.work_states(job_id)[owner.task_id].model_dump()
    for field in ("searches", "proposals", "record", "evidence", "context_key", "dependency_key"):
        assert after[field] == before[field]
    assert after["review_key"] != before["review_key"]
    await engine.review_business(owner)
    assert engine.call.await_count == 1
    dependencies[0]["sections"][0]["revision"] = 3
    await engine.review_business(owner)
    assert engine.call.await_count == 2
    engine.research_work.assert_not_awaited()


async def test_repair_instructions_do_not_reset_search_budget_or_completed_proposals(store, inputs):
    Path(inputs["knowledge_path"], "rules.md").unlink()
    runtime = GroupRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    engine = Engine(store, runtime)
    await engine.run(job_id)
    owner = engine.work_plan.tasks[0]
    before = store.work_states(job_id)[owner.task_id]
    assert len(before.searches) == 3 and before.proposals
    query_calls = sum(n == "query" for n, _ in runtime.calls)
    for instruction in ("合并重复规则", "澄清角色职责"):
        after = await engine.research_work(owner, instruction)
        assert after.searches == before.searches
        assert {p.gap_id for p in before.proposals} <= {p.gap_id for p in after.proposals}
        assert after.research_instruction == instruction and after.researched
    assert sum(n == "query" for n, _ in runtime.calls) == query_calls


async def test_business_review_can_block_otherwise_valid_sections(store, inputs):
    class MissingRule(GroupRuntime):
        async def complete(self, name, context, schema, session):
            if name == "review_work":
                return RuntimeResult(
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "kind": "pending",
                                    "key": "approval-scope",
                                    "description": "审批范围需要用户确认",
                                    "section_ids": [s["section_id"] for s in context["sections"]],
                                }
                            ]
                        }
                    )
                )
            return await super().complete(name, context, schema, session)

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, MissingRule()).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert all(not s.local_findings for s in store.states(job_id))
    assert any(f["key"] == "approval-scope" for f in job["snapshot"]["blocking_issues"])


async def test_repair_retains_business_context_and_invalidates_task_review(store, inputs):
    class Repair(GroupRuntime):
        async def complete(self, name, context, schema, session):
            if name == "drill_down":
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": True,
                            "reason": "正文不符合依据",
                            "affected_section_ids": context["finding"]["section_ids"],
                            "repair_instruction": "恢复有依据的业务规则",
                        }
                    )
                )
            result = await super().complete(name, context, schema, session)
            if name == "generate" and context["section"]["level"] == 2:
                data = json.loads(result.content)
                data["blocks"][0]["text"] = "与证据不符的业务规则"
                return RuntimeResult(json.dumps(data))
            return result

    runtime = Repair()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert sorted(s.revision for s in store.states(job_id)) == [1, 2]
    repair = next(c for n, c in runtime.calls if n == "repair")
    assert repair["business_record"]["facts"]
    assert "peer_sections" not in repair
    assert len([1 for n, _ in runtime.calls if n == "review_work"]) == 2


async def test_work_plan_obeys_configured_task_budget(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_work_sections=1)))
    with pytest.raises(ValueError, match="max_sections_per_task"):
        await Engine(store, GroupRuntime()).run(job_id)
    assert store.work_plan(job_id) is None


async def test_upstream_revision_invalidates_downstream_review_and_refreshes_record(store, inputs):
    class Dependencies(GroupRuntime):
        async def complete(self, name, context, schema, session):
            if name == "work_plan":
                ids = [s["section_id"] for s in context["sections"]]
                return RuntimeResult(
                    WorkPlan(
                        tasks=[task("later", [ids[1]], ["first"]), task("first", [ids[0]])]
                    ).model_dump_json()
                )
            return await super().complete(name, context, schema, session)

    runtime = Dependencies()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    previous = store.work_states(job_id)
    assert all(w.review_key for w in previous.values())
    sid = store.work_plan(job_id).tasks[1].section_ids[0]
    state = next(s for s in store.states(job_id) if s.section_id == sid)
    state.revision += 1
    store.save_section(job_id, state)
    assert all(not w.review_key for w in store.work_states(job_id).values())
    with pytest.raises(ValueError, match="Review"):
        store.publish(job_id, Path(job["artifact"]))
    store.update(job_id, status="running")
    await Engine(store, runtime).run(job_id)
    current = store.work_states(job_id)
    assert current["later"].record_key != previous["later"].record_key
    assert current["later"].review_key != previous["later"].review_key
    assert all(w.review_key for w in current.values())


async def test_published_business_records_are_immutable(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    await Engine(store, GroupRuntime()).run(job_id)
    state = store.work_states(job_id)["business"]
    with pytest.raises(ValueError, match="immutable"):
        store.save_work_state(job_id, state)
    with pytest.raises(ValueError, match="immutable"):
        store.save_work_plan(job_id, store.work_plan(job_id))


async def test_shared_gap_is_completed_after_three_rounds_even_when_reviewer_misses_it(
    store, inputs
):
    class Gap(GroupRuntime):
        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "next_action" and context["evidence"]:
                data = json.loads(result.tool_calls[0]["function"]["arguments"])
                data["missing_information"] = ["审批范围需要业务确认"]
                result.tool_calls[0]["function"]["arguments"] = json.dumps(data)
            return result

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, Gap()).run(job_id)
    assert job["status"] == "draft"
    assert not job["snapshot"]["blocking_issues"]
    work = store.work_states(job_id)["business"]
    assert len(work.searches) == 3 and len(work.proposals) == 1
    assert work.missing == ["审批范围需要业务确认"]  # Source gap remains auditable.
    assert all(not s.content.pending_items for s in store.states(job_id))
    assert all(
        any(
            b.origin == "model_proposal" and not b.text.startswith("【模型建议】")
            for b in s.content.blocks
        )
        for s in store.states(job_id)
    )
    assert all("离线演示建议" not in c["statement"] for c in job["snapshot"]["records"].values())


async def test_empty_writer_response_enters_research_before_extraction(store, inputs):
    inputs["user_request"] = "# 外汇交易\n说明交易角色、输入和业务规则。"

    class EmptyWriter(GroupRuntime):
        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "generate" and sum(name == "generate" for name, _ in self.calls) == 1:
                return RuntimeResult('{"blocks":[],"pending_items":[],"visuals":[]}')
            return result

    runtime = EmptyWriter()
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=0)))
    await Engine(store, runtime).run(job_id)
    first_extract = next(i for i, (name, _) in enumerate(runtime.calls) if name == "extract")
    earlier = runtime.calls[:first_extract]
    assert any(ctx.get("writing_gaps") for _, ctx in earlier)
    assert sum(name == "generate" for name, _ in earlier) == 2
    assert not any(name == "integrate_supplement" for name, _ in earlier)
    final_writer = next(ctx for name, ctx in reversed(earlier) if name == "generate")
    assert final_writer["model_completion_allowed"] and final_writer["model_proposals"]
    work = store.work_states(job_id)["business"]
    assert len(work.searches) == 3
    assert any("The body has not been generated" in gap for gap in work.missing)
    assert work.proposals
    assert store.states(job_id)[0].content.blocks


async def test_exact_written_proposal_skips_editing_but_retains_independent_review(store, inputs):
    inputs["user_request"] = "# 外汇交易"
    gap = "审批范围需要业务确认"

    class RepeatedPending(GroupRuntime):
        async def complete(self, name, context, schema, session):
            result = await super().complete(name, context, schema, session)
            if name == "next_action" and context["evidence"]:
                value = json.loads(result.tool_calls[0]["function"]["arguments"])
                value["missing_information"] = [gap]
                result.tool_calls[0]["function"]["arguments"] = json.dumps(value)
            if name == "generate":
                proposal = context["model_proposals"][0]
                return RuntimeResult(
                    json.dumps(
                        {
                            "blocks": [
                                {
                                    "block_id": "already_written",
                                    "text": proposal["content"],
                                    "origin": "model_proposal",
                                    "evidence_ids": [],
                                }
                            ],
                            "pending_items": [gap],
                        }
                    )
                )
            return result

    runtime = RepeatedPending()
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=0)))
    await Engine(store, runtime).run(job_id)
    assert not any(name == "integrate_supplement" for name, _ in runtime.calls)
    assert all(
        any(name == action for name, _ in runtime.calls)
        for action in ["extract", "validate", "review_work"]
    )
    work = store.work_states(job_id)["business"]
    assert len(work.searches) == 3 and work.missing == [gap] and len(work.proposals) == 1
    saved = store.states(job_id)[0]
    assert saved.content.blocks[0].text == work.proposals[0].content
    assert saved.content.blocks[0].origin == "model_proposal"
    assert not saved.content.pending_items


async def test_repair_task_id_is_rejected_and_model_can_correct_it(store, inputs):
    class RepairTarget(GroupRuntime):
        attempts = 0
        feedback = ""

        async def complete(self, name, context, schema, session):
            if name == "drill_down":
                self.attempts += 1
                if self.attempts == 2:
                    self.feedback = context["format_feedback"]
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": True,
                            "reason": "有依据的修复",
                            "affected_section_ids": ["business"]
                            if self.attempts == 1
                            else context["finding"]["section_ids"],
                            "repair_instruction": "恢复材料原文",
                        }
                    )
                )
            result = await super().complete(name, context, schema, session)
            if name == "generate" and context["section"]["level"] == 2:
                data = json.loads(result.content)
                data["blocks"][0]["text"] = "无依据错误"
                return RuntimeResult(json.dumps(data))
            return result

    runtime = RepairTarget()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert runtime.attempts == 2 and "document section IDs" in runtime.feedback


async def test_unexecutable_adjudication_keeps_issue_and_delivers_only_draft(store, inputs):
    class NoTarget(GroupRuntime):
        async def complete(self, name, context, schema, session):
            if name == "drill_down":
                return RuntimeResult(json.dumps({"confirmed": True, "reason": "未给出目标"}))
            result = await super().complete(name, context, schema, session)
            if name == "generate" and context["section"]["level"] == 2:
                data = json.loads(result.content)
                data["blocks"][0]["text"] = "无依据错误"
                return RuntimeResult(json.dumps(data))
            return result

    job_id = store.create(JobInput(**inputs, runtime="llm"))
    job = await Engine(store, NoTarget()).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert Path(job["artifact"]).is_file()
    assert all(s.revision == 1 for s in store.states(job_id))
    assert any(f["key"].startswith("adjudication:") for f in job["snapshot"]["blocking_issues"])
    with store.connect() as conn:
        assert conn.execute("SELECT published_version FROM documents").fetchone()[0] is None


async def test_work_planning_failure_reports_stage_without_provider_details(store, inputs):
    from folio.api.workspace import job_events

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    store.update(
        job_id,
        status="failed",
        stage="work_planning",
        error="ValueError: WorkPlan must cover every content node exactly once; private-provider-detail",
    )
    frames = [frame async for frame in job_events(store, job_id)]
    failed = next(frame for frame in frames if "event: job.failed" in frame)
    assert "任务规划遗漏了部分内容节点" in failed
    assert "尚未开始检索和正文生成" in failed
    assert "已确认的目录已保留" in failed
    assert "private-provider-detail" not in failed


async def test_local_extraction_uses_section_material_and_review_keeps_shared_rules(store, inputs):
    runtime = GroupRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await Engine(store, runtime).run(job_id)
    extracted = [c for name, c in runtime.calls if name == "extract"]
    validated = [c for name, c in runtime.calls if name == "validate"]
    assert extracted and validated
    for current in extracted:
        assert "business_context" not in current
        matching = next(
            c for c in validated if c["section"]["section_id"] == current["section"]["section_id"]
        )
        assert current["content"] == matching["content"]
        assert current["evidence"] == matching["evidence"]
        shared = matching["business_context"]
        assert "business_record" in shared and "model_proposals" in shared
        assert "peer_sections" not in shared and "task_sections" not in shared
    assert any(name == "review_work" for name, _ in runtime.calls)


async def test_exhausted_context_budget_is_not_retried_with_larger_feedback(store, inputs):
    from folio.runtime.errors import ModelContextLimit

    class TooLarge(GroupRuntime):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def complete(self, name, context, schema, session):
            if name == "extract":
                self.attempts.append(context)
                raise ModelContextLimit(
                    "This model's maximum context length is 32768 tokens. Your prompt contains at least 32768 input tokens."
                )
            return await super().complete(name, context, schema, session)

    runtime = TooLarge()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(ModelContextLimit):
        await Engine(store, runtime).run(job_id)
    # Independent nodes may both reach extraction before failure cancels the pool.
    # Neither failed node may retry its oversized request with added feedback.
    assert runtime.attempts
    assert len({c["section"]["section_id"] for c in runtime.attempts}) == len(runtime.attempts)
    assert all("format_feedback" not in c for c in runtime.attempts)
    assert "Model context limit" in store.get(job_id)["error"]


def test_shared_content_references_only_exact_proposals_and_preserves_changes():
    from folio.models import Block, SectionContent
    from folio.orchestration.business import shared_content
    from folio.orchestration.work import GapProposal

    proposal = GapProposal(
        gap_id="g1",
        section_ids=["s1"],
        content="完整的建议规则文本，需要原样保留且不能用摘要代替。",
    )
    original = SectionContent(
        blocks=[
            Block(
                block_id="proposal_g1",
                origin="model_proposal",
                text="【模型建议】" + proposal.content,
            ),
            Block(block_id="fact", text="原始事实", evidence_ids=["e1"]),
        ]
    )
    before = original.model_dump()
    compact = shared_content(original, [proposal])
    assert compact["blocks"][0] == {
        "block_id": "proposal_g1",
        "origin": "model_proposal",
        "proposal_ref": "g1",
    }
    assert compact["blocks"][1]["text"] == "原始事实"
    assert compact["blocks"][1]["evidence_ids"] == ["e1"]
    assert original.model_dump() == before
    original.blocks[0].text += "已修改"
    changed = shared_content(original, [proposal])
    assert changed["blocks"][0]["text"].endswith("已修改")
    assert "proposal_ref" not in changed["blocks"][0]
    assert shared_content(original, [])["blocks"][0]["text"] == original.blocks[0].text
