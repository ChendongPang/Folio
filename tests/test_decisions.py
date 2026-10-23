import asyncio
import json

import pytest
from native_calls import action_call, native_call

from folio.models import Evidence, JobInput
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


async def test_parallel_tasks_use_llm_decisions_to_requery_missing_evidence(store, inputs):
    class Knowledge:
        count = 0

        async def search(self, queries, **kwargs):
            self.count += 1
            return [
                Evidence(
                    evidence_id=f"ev_{self.count}",
                    source_id="kb",
                    source_version="v1",
                    locator="chunk",
                    text=f"交易规则{self.count}",
                )
            ]

    class Decisions(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            if task == "next_action":
                return action_call("search" if context["search_count"] < 2 else "write",
                                   summary="补查特定条件")
            if task == "query":
                return native_call("search_sources", {"queries": ["审批" + str(len(context["previous_queries"]))]})
            return await super().complete(task, context, schema, session_key)

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    knowledge = Knowledge()
    result = await Engine(store, Decisions(), knowledge).run(job_id)
    assert result["status"] == "draft"
    plan = result["plan"]["sections"]
    saved = [e["data"]["section_id"] for e in store.events(job_id) if e["kind"] == "section_saved"]
    assert set(saved) == {s["section_id"] for s in plan}
    assert len(saved) == len(plan)
    assert knowledge.count == 4
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM decisions WHERE job_id=?", (job_id,)).fetchall()
    assert len([r for r in rows if r["task"] == "next_action"]) == 6
    assert all("confidence" not in json.loads(r["result"]) for r in rows)


async def test_unavailable_action_never_executes(store, inputs):
    class BadDecision(DemoRuntime):
        feedback = []

        async def complete(self, task, context, schema, session_key):
            if task == "next_action":
                assert schema["properties"]["action"]["enum"] == ["search"]
                if "format_feedback" in context:
                    self.feedback.append(context["format_feedback"])
                return action_call("write", summary="无证据直接写")
            return await super().complete(task, context, schema, session_key)

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    runtime = BadDecision()
    with pytest.raises(ValueError, match="not available"):
        await Engine(store, runtime).run(job_id)
    assert runtime.feedback and all("not available" in f for f in runtime.feedback)
    assert store.states(job_id) == []
    assert store.get(job_id)["artifact"] is None


async def test_cached_decision_restores_audit_record_after_interruption(store, inputs):
    class Interrupted(DemoRuntime):
        async def complete(self, task, *args):
            if task == "query":
                raise asyncio.CancelledError()
            return await super().complete(task, *args)

    job = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, Interrupted()).run(job)
    with store.connect() as conn:
        conn.execute("DELETE FROM decisions WHERE job_id=?", (job,))
    await Engine(store, DemoRuntime()).run(job)
    with store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM decisions WHERE job_id=?", (job,)).fetchone()[0] > 0
        )


async def test_retry_reports_specific_validation_error_and_clears_previous_failure(store, inputs):
    class DuplicateOnce(DemoRuntime):
        seen = set()
        feedback = []

        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "generate":
                section = context["section"]["section_id"]
                if section not in self.seen:
                    self.seen.add(section)
                    data = json.loads(result.content)
                    data["blocks"].append(data["blocks"][0])
                    return RuntimeResult(json.dumps(data))
                self.feedback.append(context.get("format_feedback", ""))
            return result

    runtime = DuplicateOnce()
    job = store.create(JobInput(**inputs, runtime="demo"))
    store.update(job, status="failed", error="previous failure")
    store.resume_job(job)
    result = await Engine(store, runtime).run(job)
    assert result["status"] == "draft" and result["error"] is None
    assert len(runtime.feedback) == 2
    assert all("Duplicate block ID" in f for f in runtime.feedback)
    assert all("input_value" not in f for f in runtime.feedback)


async def test_action_schema_tracks_search_budget_and_gaps_continue_to_writing(store, inputs):
    class Bounded(DemoRuntime):
        selections = []

        async def complete(self, task, context, schema, session_key):
            if task == "next_action":
                allowed = schema["properties"]["action"]["enum"]
                assert allowed == context["available_actions"]
                count = context["search_count"]
                if count == 0:
                    assert allowed == ["search"]
                if count == 3:
                    assert "search" not in allowed
                action = "search" if "search" in allowed else "pending"
                self.selections.append((context["work_task"]["task_id"], count, action))
                return action_call(action, summary="按实际可用动作选择",
                                   missing_information=["缺少业务审批规则"])
            if task == "query":
                return native_call("search_sources", {"queries": ["规则第" + str(len(context["previous_queries"])) + "轮"]})
            return await super().complete(task, context, schema, session_key)

    class Empty:
        calls = 0

        async def search(self, queries, **kwargs):
            self.calls += 1
            return []

    runtime, knowledge = Bounded(), Empty()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, runtime, knowledge).run(job_id)
    assert result["status"] == "draft"
    states = store.work_states(job_id)
    assert all(
        len(w.searches) == 3 and w.researched and w.writable and w.proposals
        for w in states.values()
    )
    assert knowledge.calls == 3 * len(states)
    for task_id in states:
        assert [(n, a) for t, n, a in runtime.selections if t == task_id] == [
            (0, "search"),
            (1, "search"),
            (2, "search"),
            (3, "pending"),
        ]
    assert result["completed_sections"] == result["total_sections"]
