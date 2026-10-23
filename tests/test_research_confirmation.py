"""An explicit confirmation grant starts a fresh bounded research session."""

from uuid import uuid4

import pytest
from test_research_workflow import (
    FixtureSandbox,
    ScriptedResearchRuntime,
    ledger_for,
    make_request,
    use_fixture_sandbox,
)
from test_research_workflow import research_inputs as research_inputs

from folio.confirmations import confirmation_view, rounds, submit_confirmation
from folio.models import JobInput
from folio.orchestration.engine import Engine


@pytest.mark.usefixtures("research_inputs")
async def test_budget_only_confirmation_resumes_exhausted_research(store, inputs, monkeypatch):
    use_fixture_sandbox(monkeypatch)
    runtime = ScriptedResearchRuntime()
    job_id = store.create(make_request(inputs, max_tool_calls=2))
    first = await Engine(store, runtime).run(job_id)
    before = store.work_states(job_id)["fields"]
    assert first["status"] == "needs_confirmation"
    assert before.research["incomplete"] and before.research["usage"]["calls"] == 2
    previous_runs = ledger_for(store, job_id).runs("fields")
    request_id = uuid4().hex
    submit_confirmation(
        store, job_id, request_id=request_id,
        revision=confirmation_view(store, job_id)["revision"], answers=[], additional_calls=100,
    )
    assert store.get(job_id)["calls"] == first["calls"]
    assert store.get(job_id)["input"]["limits"]["max_calls"] == first["calls"] + 100
    runtime.mode = "read_only"
    result = await Engine(store, runtime).run(job_id)
    work = store.work_states(job_id)["fields"]
    quota = "fields:" + request_id
    assert work.research["quota_task_id"] == quota
    assert work.research["round_id"] != before.research["round_id"]
    assert not work.research["incomplete"]
    assert 0 < ledger_for(store, job_id).usage(quota)["calls"] <= 2
    assert ledger_for(store, job_id).runs("fields") == previous_runs
    assert rounds(store, job_id)[0]["status"] == "applied"
    assert result["status"] in {"draft", "needs_confirmation"}
    assert not any(
        item["key"].startswith("research-incomplete:")
        for item in result["snapshot"].get("blocking_issues", [])
    )


@pytest.mark.usefixtures("research_inputs")
async def test_new_grant_bypasses_business_cached_research_without_context_change(store, inputs, monkeypatch):
    use_fixture_sandbox(monkeypatch)
    runtime = ScriptedResearchRuntime()
    engine = Engine(store, runtime)
    job_id = store.create(make_request(inputs, max_tool_calls=2))
    await engine.run(job_id)
    before = store.work_states(job_id)["fields"]
    request_id = uuid4().hex
    submit_confirmation(
        store, job_id, request_id=request_id,
        revision=confirmation_view(store, job_id)["revision"], answers=[], additional_calls=100,
    )
    engine.request = JobInput.model_validate(store.get(job_id)["input"])
    engine.limits = engine.request.limits
    runtime.mode = "read_only"
    work = await engine.research_work(engine.work_plan.tasks[0], before.research_instruction)
    assert work.context_key == before.context_key
    assert work.global_facts == before.global_facts
    assert work.research["round_id"] != before.research["round_id"]
    assert work.research["quota_task_id"] == "fields:" + request_id
    assert not work.research["incomplete"]
    assert ledger_for(store, job_id).usage("fields:" + request_id)["calls"] == 1
    # Ordinary repeated access remains cached within this same human grant.
    repeated = await engine.research_work(engine.work_plan.tasks[0], before.research_instruction)
    assert repeated == work
    assert ledger_for(store, job_id).usage("fields:" + request_id)["calls"] == 1


@pytest.mark.usefixtures("research_inputs")
@pytest.mark.parametrize("status", ["artifact_limit", "invalid_artifacts", "unexpected_failure"])
async def test_non_success_sandbox_results_are_not_reported_as_completed(store, inputs, monkeypatch, status):
    class FailedOutputSandbox(FixtureSandbox):
        async def execute(self, code, *, execution_id, profile="base", timeout_seconds=None):
            return {"status": status, "execution_id": execution_id, "artifacts": [], "stderr": "Rejected output"}

    monkeypatch.setattr("folio.research.workflow.SandboxManager", FailedOutputSandbox)
    runtime = ScriptedResearchRuntime(mode="self_correct")
    job_id = store.create(make_request(inputs))
    await Engine(store, runtime).run(job_id)
    execution = next(item for item in ledger_for(store, job_id).runs("fields") if item["tool"] == "sandbox.exec")
    assert execution["status"] == "failed"
    assert execution["result"]["status"] == status
