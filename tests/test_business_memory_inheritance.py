"""A writing-gap continuation keeps compatible business research, not execution state."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from docx import Document
from native_calls import research_call
from test_research_workflow import (
    ScriptedResearchRuntime,
    ledger_for,
    make_request,
    use_fixture_sandbox,
)
from test_research_workflow import research_inputs as research_inputs

from folio.orchestration.engine import Engine
from folio.orchestration.work import WorkState
from folio.research.contracts import ResearchBatch
from folio.research.workflow import ResearchTools, local, research_business
from folio.research.workspace import WorkspaceManager


class MemoryRuntime(ScriptedResearchRuntime):
    def __init__(self):
        super().__init__(mode="read_only")
        self.research_contexts = []
        self.followup_action = None

    async def complete(self, name, context, schema, session):
        if name != "research_step":
            return await super().complete(name, context, schema, session)
        self.research_contexts.append(deepcopy(context))
        self.calls.append((name, deepcopy(context)))
        if context.get("memory_checkpoint_required"):
            return research_call("research.save", research_notes=self.research_notes(context))
        if context["writing_gaps"]:
            if context["step"] == 0 and self.followup_action:
                tool, arguments = self.followup_action
                return research_call(tool, arguments)
            # Omitting notes is intentional: inherited notes must survive this.
            return research_call("finish", missing_information=[],
                                 research_notes=self.research_notes(context))
        if context["step"] == 0:
            return research_call("fs.read", {"path": "requirements.docx", "limit": 1},
                                 notes="已确认必填字段按名称去重；新增审批问题应另行补查。")
        return research_call("finish", missing_information=[],
                             research_notes=self.research_notes(context))


async def started(store, inputs, monkeypatch):
    use_fixture_sandbox(monkeypatch)
    document_path = Path(inputs["knowledge_path"]) / "requirements.docx"
    document = Document(document_path)
    document.add_paragraph("主管复核后方可变更必填标记。")
    document.save(document_path)
    runtime = MemoryRuntime()
    engine = Engine(store, runtime)
    job_id = store.create(make_request(inputs))
    await engine.run(job_id)
    work = store.work_states(job_id)["fields"]
    assert work.research["research_memory"]["notes"]
    return engine, runtime, work, ledger_for(store, job_id)


@pytest.mark.usefixtures("research_inputs")
@pytest.mark.parametrize("legacy_session", [False, True])
async def test_writing_gap_inherits_notes_history_and_read_progress(store, inputs, monkeypatch, legacy_session):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    old = ledger.load_session("fields", previous.research["round_id"])
    if legacy_session:
        old.pop("memory_compatibility_key", None)
        old.pop("workspace_root", None)
        ledger.save_session("fields", previous.research["round_id"], old)
    initial_usage = ledger.usage("fields")["calls"]
    runtime.research_contexts.clear()

    work = await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["补查审批规则"])

    context = runtime.research_contexts[0]
    assert context["writing_gaps"] == ["补查审批规则"]
    assert context["notes"] == old["notes"]
    assert "必填字段按字段名称去重" in context["research_notes"]
    assert context["read_progress"] == list(old["read_progress"].values())
    assert [item["request_id"] for item in context["observations"]] == [
        item["request_id"] for item in old["observations"]
    ]
    read = next(item for item in old["observations"] if item["tool"] == "fs.read")
    assert read["request_id"] in context["research_history"]
    assert context["observations"][0]["tool_call"] == old["observations"][0]["tool_call"]
    assert context["step"] == 0 and context["local_round"] == 2
    assert work.research["round_id"] != previous.research["round_id"]
    assert work.research["notes"] == old["notes"]
    assert work.research["research_memory"]["notes"] == old["research_memory"]["notes"]
    assert ledger.usage("fields")["calls"] == initial_usage  # Finish needs no repeated read.


@pytest.mark.usefixtures("research_inputs")
async def test_new_session_does_not_replay_old_pending_batch_and_can_use_cursor(store, inputs, monkeypatch):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    old = ledger.load_session("fields", previous.research["round_id"])
    old["pending_batch"] = ResearchBatch.model_validate({"steps": [{
        "tool": "fs.read", "arguments": {"path": "must-not-execute.txt"},
    }]}).model_dump()
    old["pending_batch_start"] = 300
    old["pending_batch_cursor"] = 0
    ledger.save_session("fields", previous.research["round_id"], old)
    aliases_path = Path(previous.research["task_root"]) / ".workspace" / "model-references.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
    cursor = next(iter(aliases["cursors"]))
    runtime.followup_action = ("fs.read", {"cursor": cursor})
    runtime.research_contexts.clear()

    work = await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["继续读下一段"])

    assert runtime.research_contexts[0]["step"] == 0
    new_runs = [item for item in ledger.runs("fields") if item["round_id"] == work.research["round_id"]]
    assert len(new_runs) == 1
    assert new_runs[0]["arguments"] == {"cursor": cursor}
    assert "error" not in new_runs[0]["result"]
    assert "主管复核" in new_runs[0]["result"]["model_text"]
    assert ledger.load_session("fields", previous.research["round_id"]) == old


@pytest.mark.usefixtures("research_inputs")
async def test_current_continuation_resume_keeps_pending_call_and_newer_notes(store, inputs, monkeypatch):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    aliases_path = Path(previous.research["task_root"]) / ".workspace" / "model-references.json"
    cursor = next(iter(json.loads(aliases_path.read_text(encoding="utf-8"))["cursors"]))
    runtime.followup_action = ("fs.read", {"cursor": cursor})
    runtime.research_contexts.clear()
    original_model = ResearchTools.model
    interrupted = False

    async def interrupt_after_pending_saved(tools, action, context, schema):
        nonlocal interrupted
        result = await original_model(tools, action, context, schema)
        if action == "research_step" and context["writing_gaps"] and not interrupted:
            interrupted = True
            tools.session["notes"] = "补研究已写入的新检查点，不能被上一轮旧笔记覆盖。"
            await local(tools.save_memory)
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(ResearchTools, "model", interrupt_after_pending_saved)
    with pytest.raises(asyncio.CancelledError):
        await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["继续处理新缺口"])
    session_id = ledger.local_rounds("fields")[-1]["session_id"]
    interrupted_session = ledger.load_session("fields", session_id)
    pending_call = interrupted_session["pending_batch"]["steps"][0]["tool_call"]
    assert interrupted_session["notes"] != previous.research["notes"]

    work = await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["继续处理新缺口"])

    assert work.research["round_id"] == session_id
    assert work.research["notes"] == interrupted_session["notes"]
    # No second model decision for the already persisted pending call.
    assert [context["step"] for context in runtime.research_contexts] == [0, 1]
    saved = ledger.load_session("fields", session_id)
    observation = next(item for item in saved["observations"] if item.get("tool_call") == pending_call)
    assert "error" not in observation["result"]
    assert "主管复核" in observation["model_text"]
    assert "pending_batch" not in saved


@pytest.mark.usefixtures("research_inputs")
async def test_legacy_memory_survives_cancelled_continuation_and_different_new_gaps(store, inputs, monkeypatch):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    old_round = previous.research["round_id"]
    old = ledger.load_session("fields", old_round)
    old.pop("memory_compatibility_key", None)
    old.pop("workspace_root", None)
    ledger.save_session("fields", old_round, old)
    original_model = ResearchTools.model
    interrupted = False

    async def interrupt_first_continuation(tools, action, context, schema):
        nonlocal interrupted
        result = await original_model(tools, action, context, schema)
        if action == "research_step" and context["writing_gaps"] and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(ResearchTools, "model", interrupt_first_continuation)
    with pytest.raises(asyncio.CancelledError):
        await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["第一次补查审批规则"])
    # The first continuation has overwritten the shared workspace assignment,
    # while the business checkpoint still refers to the legacy research session.
    assert store.work_states(engine.job_id)["fields"].research["round_id"] == old_round
    runtime.research_contexts.clear()

    await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["改为补查异常处理"])

    context = runtime.research_contexts[0]
    assert context["writing_gaps"] == ["改为补查异常处理"]
    assert context["notes"] == old["notes"]
    assert "必填字段按字段名称去重" in context["research_notes"]
    assert context["read_progress"] == list(old["read_progress"].values())
    assert [item["request_id"] for item in context["observations"]] == [
        item["request_id"] for item in old["observations"]
    ]


@pytest.mark.usefixtures("research_inputs")
@pytest.mark.parametrize("changed", ["facts", "sources"])
async def test_incompatible_snapshot_does_not_inherit_memory(store, inputs, monkeypatch, changed):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    if changed == "facts":
        previous.global_facts = {"different-section": []}
    else:
        original_init = WorkspaceManager.__init__

        def changed_snapshot(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.source_snapshot_id = "new-source-snapshot"

        monkeypatch.setattr(WorkspaceManager, "__init__", changed_snapshot)
        # Directory handoff rejects changed sources independently. This test
        # isolates the business-session compatibility boundary.
        monkeypatch.setattr("folio.research.planning_handoff.seed_directory_research", lambda *_: {})
    runtime.research_contexts.clear()

    await research_business(engine, engine.work_plan.tasks[0], previous, writing_gaps=["重新核实"])

    context = runtime.research_contexts[0]
    assert context["notes"] == ""
    assert context["observations"] == []
    assert context["read_progress"] == []
    assert "首段摘录" not in context["research_notes"]


@pytest.mark.usefixtures("research_inputs")
async def test_another_business_task_does_not_receive_memory(store, inputs, monkeypatch):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    task = engine.work_plan.tasks[0].model_copy(update={"task_id": "another-business"})
    work = WorkState(task_id=task.task_id, context_key=previous.context_key,
                     global_facts=previous.global_facts)
    runtime.research_contexts.clear()

    await research_business(engine, task, work, writing_gaps=["独立研究"])

    context = runtime.research_contexts[0]
    assert context["notes"] == ""
    assert context["observations"] == []
    assert context["read_progress"] == []


@pytest.mark.usefixtures("research_inputs")
async def test_existing_continuation_rejects_changed_facts_before_model_request(store, inputs, monkeypatch):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    gaps = ["补查审批规则"]
    work = await engine.research_work(engine.work_plan.tasks[0], writing_gaps=gaps)
    saved = ledger.load_session("fields", work.research["round_id"])
    work.global_facts = {"changed-facts": []}
    runtime.research_contexts.clear()

    with pytest.raises(ValueError, match="old research checkpoint cannot be reused"):
        await research_business(engine, engine.work_plan.tasks[0], work, writing_gaps=gaps)

    assert runtime.research_contexts == []
    assert ledger.load_session("fields", work.research["round_id"]) == saved


@pytest.mark.usefixtures("research_inputs")
@pytest.mark.parametrize("history_owner", ["fields", "another-business"])
async def test_new_grant_history_access_is_inherited_and_task_scoped(store, inputs, monkeypatch, history_owner):
    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    old_run = ledger.runs("fields")[0]
    requested_id = old_run["id"]
    if history_owner != "fields":
        requested_id = "another-business-private-read"
        ledger.begin(requested_id, history_owner, "other-round", 0, "fs.read", {},
                     config=engine.request.research)
        ledger.finish(requested_id, {"text": "其他业务的独有研究内容"})
    previous_usage = ledger.usage("fields")
    # Grant creation has separate API tests; here only its research boundary matters.
    monkeypatch.setattr("folio.confirmations.rounds", lambda *_: [{"request_id": "second-grant"}])
    runtime.followup_action = ("fs.read", {"scope": "history", "request_id": requested_id})
    runtime.research_contexts.clear()

    work = await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["回顾已有依据"])

    assert runtime.research_contexts[0]["notes"] == previous.research["notes"]
    assert runtime.research_contexts[0]["local_round"] == 1
    new_runs = ledger.runs("fields:second-grant")
    assert len(new_runs) == 1
    result = new_runs[0]["result"]
    if history_owner == "fields":
        assert "error" not in result
        assert result["original_request_id"] == old_run["id"]
        assert "必填字段按字段名称去重" in result["text"]
    else:
        assert "error" in result
        assert "其他业务的独有研究内容" not in json.dumps(result, ensure_ascii=False)
    assert ledger.usage("fields") == previous_usage
    assert work.research["quota_task_id"] == "fields:second-grant"


@pytest.mark.usefixtures("research_inputs")
@pytest.mark.parametrize("old_key_kind", ["current", "instruction_key", "no_key"])
async def test_repair_hint_preserves_coverage_and_migrates_older_knowledge_keys(
    store, inputs, monkeypatch, old_key_kind,
):
    from folio.research.session_memory import compatibility_key, prepare_session_memory
    from folio.research.settings import load_research_policy

    engine, runtime, previous, ledger = await started(store, inputs, monkeypatch)
    task = engine.work_plan.tasks[0]
    old_round = previous.research["round_id"]
    old_root = Path(previous.research["task_root"])
    old = ledger.load_session(task.task_id, old_round)
    assignment = json.loads((old_root / "context" / "sessions" / old_round / "assignment.json").read_text())
    policy = load_research_policy(store.root)
    if old_key_kind == "instruction_key":
        old["memory_compatibility_key"] = compatibility_key(assignment, policy, legacy_instruction=True)
    elif old_key_kind == "no_key":
        old.pop("memory_compatibility_key")
    coverage_fields = {
        "coverage_contract": {"status": "ready", "required_scopes": [{"source_id": "requirements.docx"}]},
        "coverage_confirmed_scopes": [{"source_id": "requirements.docx", "node_id": "N001"}],
        "coverage_auto_scopes": {"one": "scope-1"},
        "coverage_ids": ["scope-1"], "required_coverage_ids": ["scope-1"],
        "manual_coverage_ids": [], "coverage_unresolved": ["尚需确认某项适用范围"],
        "coverage_supersessions": {"previous": "scope-1"},
        "coverage_review_attempts": 2,
        "coverage_review_navigation": {"requirements.docx": {"offset": 0}},
        "coverage_review_observations": [{"tool": "fs.inspect", "result": {"text": "完整结构"}}],
        "coverage_review_pending": {"scope": "requirements.docx"},
    }
    old.update(deepcopy(coverage_fields))
    ledger.save_session(task.task_id, old_round, old)
    assignment["instruction"] = "针对新发现的审批疑点补查，不改变正式章节职责。"
    assignment["writing_gaps"] = ["补查主管复核条件"]
    next_round = "new-repair-round"
    workspace = WorkspaceManager(old_root.parents[2] / "sources", old_root.parent / next_round)
    session = ledger.load_session(task.task_id, next_round)

    chosen = prepare_session_memory(
        ledger, task, previous, workspace, session, assignment, policy, round_id=next_round,
    )

    assert chosen == old_root
    assert session["inherited_from"] == old_round
    assert session["notes"] == old["notes"]
    assert session["observations"] == old["observations"]
    assert session["missing"][-1] == "补查主管复核条件"
    assert session["finished"] is False and session["step"] == 0
    assert all(session[field] == value for field, value in coverage_fields.items())
    session["coverage_contract"]["required_scopes"].clear()
    assert ledger.load_session(task.task_id, old_round)["coverage_contract"]["required_scopes"]
    migrated = ledger.load_session(task.task_id, old_round)
    assert migrated["memory_compatibility_key"] == compatibility_key(assignment, policy)


@pytest.mark.usefixtures("research_inputs")
async def test_existing_session_upgrades_instruction_key_without_replaying_tools(store, inputs, monkeypatch):
    from folio.research.session_memory import compatibility_key
    from folio.research.settings import load_research_policy

    engine, runtime, work, ledger = await started(store, inputs, monkeypatch)
    old_round = work.research["round_id"]
    root = Path(work.research["task_root"])
    saved = ledger.load_session(work.task_id, old_round)
    assignment = json.loads((root / "context" / "sessions" / old_round / "assignment.json").read_text())
    policy = load_research_policy(store.root)
    saved["memory_compatibility_key"] = compatibility_key(assignment, policy, legacy_instruction=True)
    ledger.save_session(work.task_id, old_round, saved)
    usage = ledger.usage(work.task_id)
    runtime.research_contexts.clear()

    resumed = await research_business(engine, engine.work_plan.tasks[0], work)

    assert not runtime.research_contexts
    assert ledger.usage(work.task_id) == usage
    assert resumed.research["round_id"] == old_round
    updated = ledger.load_session(work.task_id, old_round)
    assert updated["memory_compatibility_key"] == compatibility_key(assignment, policy)
    assert updated["observations"] == saved["observations"]
