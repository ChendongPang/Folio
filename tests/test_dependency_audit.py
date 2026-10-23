"""Optional dependency review preserves graph ownership, checkpoints, and call budgets."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import (
    DocumentPlan,
    Extraction,
    JobInput,
    Limits,
    Section,
    SectionContent,
    SectionState,
    digest,
)
from folio.orchestration import dependency_audit
from folio.orchestration.engine import Engine
from folio.orchestration.work import WorkPlan, WorkState, WorkTask
from folio.runtime.base import RuntimeResult
from folio.runtime.llm import LLMRuntime


class Runtime:
    identity = {"model": "dependency-test"}

    def __init__(self):
        self.calls = []
        self.result_hook = None

    async def complete(self, task, context, schema, session):
        assert task == "audit_work_dependencies"
        self.calls.append(deepcopy(context))
        ids = [edge["edge_id"] for edge in context["candidate_edges"]]
        assert schema["properties"]["edges"]["minItems"] == len(ids)
        assert schema["properties"]["edges"]["maxItems"] == len(ids)
        assert schema["$defs"]["EdgeVerdict"]["properties"]["edge_id"]["enum"] == ids
        result = {
            "edges": [
                {
                    "edge_id": edge["edge_id"],
                    "verdict": "keep" if edge["dependent"] == "t1" else "remove",
                    "reason": "需要新增编号" if edge["dependent"] == "t1" else "仅共享流程资料",
                }
                for edge in context["candidate_edges"]
            ]
        }
        if self.result_hook:
            result = await self.result_hook(result)
        return RuntimeResult(
            result if isinstance(result, str) else json.dumps(result), usage={"prompt_tokens": 42}
        )


@pytest.fixture
def audit_engine(store, inputs):
    doc = DocumentPlan(
        sections=[
            Section(
                section_id=f"s{i}",
                node_id=f"o{i}",
                title=f"业务{i}",
                level=1,
                display_number=str(i + 1),
                instruction="描述业务要求",
            )
            for i in range(5)
        ]
    )
    deps = [[], ["t0"], ["t1"], ["t1"], ["t2", "t3"]]
    plan = WorkPlan(
        tasks=[
            WorkTask(
                task_id=f"t{i}",
                title=f"任务{i}",
                objective="描述本业务的规则",
                section_ids=[f"s{i}"],
                questions=["业务规则是什么？"],
                depends_on=deps[i],
                completion_criteria=["规则有依据"],
                rationale="共享业务上下文",
            )
            for i in range(5)
        ]
    )
    job_id = store.create(JobInput(**inputs, runtime="demo"), plan=doc)
    store.save_work_plan(job_id, plan)
    runtime = Runtime()
    engine = Engine(store, runtime)
    engine.job_id, engine.plan, engine.work_plan = job_id, doc, plan
    engine.limits = Limits()
    return engine, runtime, plan


def audit_calls(engine):
    with engine.store.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM tasks WHERE job_id=? AND task_key LIKE 'audit_work_dependencies:%'",
                (engine.job_id,),
            )
        ]


async def test_audit_changes_only_confirmed_edges_and_resume_is_idempotent(audit_engine):
    engine, runtime, original = audit_engine
    await engine.prepare_work_plan()  # Exercise the real entry point with an already stored plan.
    updated = engine.store.work_plan(engine.job_id)
    assert updated == engine.work_plan
    assert updated.tasks[1].depends_on == ["t0"]  # Genuine output dependency is preserved.
    assert all(not t.depends_on for t in updated.tasks[2:])
    for old, new in zip(original.tasks, updated.tasks, strict=True):
        assert old.model_dump(exclude={"depends_on"}) == new.model_dump(exclude={"depends_on"})
    assert len(runtime.calls) == 1 and engine.store.get(engine.job_id)["calls"] == 1
    cached = audit_calls(engine)[0]
    result = json.loads(cached["result"])
    assert result["source_hash"] == digest(original.model_dump_json())
    assert result["target_hash"] == digest(updated.model_dump_json())
    assert result["status"] == "applied"
    assert json.loads(cached["usage"])["prompt_tokens"] == 42
    # Simulate recovery in a new Engine, even after changing the configured model.
    recovery = Engine(engine.store, Runtime())
    recovery.runtime.identity = {"model": "changed-model"}
    recovery.job_id, recovery.plan, recovery.limits = engine.job_id, engine.plan, engine.limits
    await recovery.prepare_work_plan()
    assert recovery.work_plan == updated and recovery.runtime.calls == []
    assert engine.store.get(engine.job_id)["calls"] == 1


async def test_started_tasks_keep_edges_and_research_context_on_resume(audit_engine):
    engine, runtime, plan = audit_engine
    started = plan.tasks[2]
    before_key = digest(
        json.dumps([started.model_dump(), engine.dependency_context(started), ""], sort_keys=True)
    )
    work = WorkState(
        task_id=started.task_id,
        context_key=before_key,
        researched=True,
        review_key="already-reviewed",
        searches=[["已有检索"]],
    )
    engine.store.save_work_state(engine.job_id, work)
    # A legacy task with saved text but no work state must also be protected.
    engine.store.save_section(
        engine.job_id,
        SectionState(
            section_id="s3",
            content=SectionContent(),
            extraction=Extraction(summary="已有摘要"),
            evidence=[],
            revision=1,
        ),
    )
    engine.work_plan = await dependency_audit.audit_dependencies(engine, plan)
    assert engine.work_plan.tasks[2].depends_on == ["t1"]
    assert engine.work_plan.tasks[3].depends_on == ["t1"]
    assert not {"t2", "t3"}.intersection(
        edge["dependent"] for edge in runtime.calls[0]["candidate_edges"]
    )
    cached = await engine.research_work(engine.work_plan.tasks[2])
    assert cached == work and len(runtime.calls) == 1
    assert engine.store.work_states(engine.job_id)["t2"] == work


@pytest.mark.parametrize("bad", ["json", "missing", "duplicate", "unknown", "long_reason", "error"])
async def test_invalid_audit_falls_back_once_and_does_not_block_resume(audit_engine, bad):
    engine, runtime, plan = audit_engine

    async def corrupt(data):
        if bad == "json":
            return "not JSON"
        if bad == "missing":
            data["edges"].pop()
        if bad == "duplicate":
            data["edges"][-1] = data["edges"][0]
        if bad == "unknown":
            data["edges"][-1]["edge_id"] = "not-listed"
        if bad == "long_reason":
            data["edges"][0]["reason"] = "长" * 121
        if bad == "error":
            raise RuntimeError("secret-provider-body")
        return data

    runtime.result_hook = corrupt
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert engine.store.work_plan(engine.job_id) == plan and len(runtime.calls) == 1
    assert audit_calls(engine)[0]["error"] == "dependency_audit_invalid_or_unavailable"
    assert "secret-provider-body" not in json.dumps(audit_calls(engine))
    assert engine.store.get(engine.job_id)["status"] != "failed"


async def test_timeout_is_optional_and_cached(audit_engine, monkeypatch):
    engine, runtime, plan = audit_engine
    monkeypatch.setattr(dependency_audit, "AUDIT_TIMEOUT_SECONDS", 0.01)

    async def slow(data):
        await asyncio.sleep(10)

    runtime.result_hook = slow
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert len(runtime.calls) == 1 and audit_calls(engine)[0]["error"] == "dependency_audit_timeout"


async def test_keep_and_uncertain_verdicts_preserve_every_edge(audit_engine):
    engine, runtime, plan = audit_engine

    async def conservative(data):
        for index, edge in enumerate(data["edges"]):
            edge["verdict"] = "keep" if index % 2 else "uncertain"
        return data

    runtime.result_hook = conservative
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert len(runtime.calls) == 1
    assert json.loads(audit_calls(engine)[0]["result"])["status"] == "unchanged"


@pytest.mark.parametrize("guard", ["small", "shallow", "serial_limit", "budget"])
async def test_no_extra_call_for_small_graph_or_without_call_budget(audit_engine, guard):
    engine, runtime, plan = audit_engine
    if guard == "small":
        plan = WorkPlan(tasks=plan.tasks[:3])
    elif guard == "shallow":
        plan = WorkPlan(tasks=[t.model_copy(update={"depends_on": []}) for t in plan.tasks])
    elif guard == "serial_limit":
        engine.limits = Limits(max_parallel_tasks=1)
    else:
        engine.limits = Limits(max_calls=1)
        engine.store.begin_task(engine.job_id, "already-used", 1)
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert runtime.calls == [] and audit_calls(engine) == []


async def test_task_started_during_audit_rejects_whole_update_atomically(audit_engine):
    engine, runtime, plan = audit_engine

    async def race(data):
        engine.store.save_work_state(
            engine.job_id, WorkState(task_id="t2", context_key="new-progress")
        )
        return data

    runtime.result_hook = race
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert engine.store.work_plan(engine.job_id) == plan
    assert json.loads(audit_calls(engine)[0]["result"])["status"] == "stale"
    assert await dependency_audit.audit_dependencies(engine, plan) == plan
    assert len(runtime.calls) == 1


def test_plan_and_audit_cache_commit_or_rollback_together(audit_engine, monkeypatch):
    engine, _, plan = audit_engine
    store, job_id = engine.store, engine.job_id
    key = "audit_work_dependencies:transaction-test"
    store.begin_task(job_id, key, 100)
    original_event = store._event

    def fail_last_event(conn, job, kind, data):
        if kind == "dependency_audit_completed":
            raise RuntimeError("simulated failure before commit")
        return original_event(conn, job, kind, data)

    monkeypatch.setattr(store, "_event", fail_last_event)
    with pytest.raises(RuntimeError, match="before commit"):
        store.apply_dependency_audit(
            job_id, key, digest(plan.model_dump_json()), {("t2", "t1")}, {"policy": 1}, {}
        )
    assert store.work_plan(job_id) == plan and store.task(job_id, key)["result"] is None
    monkeypatch.setattr(store, "_event", original_event)
    updated, applied = store.apply_dependency_audit(
        job_id, key, digest(plan.model_dump_json()), {("t2", "t1")}, {"policy": 1}, {}
    )
    assert applied and updated.tasks[2].depends_on == []
    assert store.completed_dependency_audit(job_id, digest(updated.model_dump_json()), 1)
    # Replaying a previously applied response cannot drop further edges.
    replayed, applied = store.apply_dependency_audit(
        job_id, key, digest(plan.model_dump_json()), {("t1", "t0")}, {"policy": 1}, {}
    )
    assert not applied and replayed == updated
    assert replayed.tasks[1].depends_on == ["t0"]


async def test_cancelled_audit_preserves_job_cancellation(audit_engine):
    engine, runtime, plan = audit_engine
    started = asyncio.Event()

    async def slow(data):
        started.set()
        await asyncio.sleep(10)

    runtime.result_hook = slow
    operation = asyncio.create_task(dependency_audit.audit_dependencies(engine, plan))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert engine.store.work_plan(engine.job_id) == plan
    assert audit_calls(engine)[0]["error"] == "dependency_audit_cancelled"


async def test_audit_uses_primary_llm_with_compact_typed_output_limit(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://unused.test/v1",
            model="test",
            no_auth=True,
            max_tokens=4096,
            structured_output="json_schema",
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    schema = dependency_audit.DependencyAudit.model_json_schema()
    schema["$defs"]["EdgeVerdict"]["properties"]["edge_id"]["enum"] = ["e1"]

    def transport(request):
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 4096
        actual = payload["response_format"]["json_schema"]["schema"]
        assert actual["$defs"]["EdgeVerdict"]["properties"]["edge_id"]["enum"] == ["e1"]
        assert "Use uncertain when evidence is insufficient" in payload["messages"][0]["content"]
        response = {
            "choices": [
                {
                    "delta": {
                        "content": json.dumps(
                            {
                                "edges": [
                                    {"edge_id": "e1", "verdict": "keep", "reason": "需要新增编号"}
                                ]
                            }
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        result = await runtime.complete(
            dependency_audit.TASK, {"candidate_edges": []}, schema, "test"
        )
        assert (
            dependency_audit.DependencyAudit.model_validate_json(result.content).edges[0].verdict
            == "keep"
        )
    finally:
        await runtime.aclose()
