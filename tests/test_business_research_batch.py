"""Native research batches overlap reads without losing IDs or durable results."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from folio.research.contracts import ResearchBatch, ResearchConfig, ResearchStep
from folio.research.ledger import ResearchLedger, ResearchLimitReached
from folio.research.workflow import ResearchTools, execute_research_batch
from folio.research.workspace import WorkspaceManager


class BatchSandbox:
    def verify_execution(self, key):
        return {}

    async def cancel_execution(self, key):
        pass


@pytest.fixture
def batch_tools(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md", "e.md"):
        (sources / name).write_text("# 业务规则\n审批必须复核：" + name, encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "task")
    events = []
    engine = SimpleNamespace(
        request=SimpleNamespace(research=ResearchConfig()), job_id="job",
        store=SimpleNamespace(work_event=lambda *args: events.append(args),
                              audit_event=lambda *args: events.append(args)),
    )
    task = SimpleNamespace(task_id="owner", title="规则研究")
    ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    session = ledger.load_session("owner", "round") | {
        "quota_task_id": "owner", "round_id": "round", "phase": "local", "round_steps": 0,
    }
    return ResearchTools(engine, task, SimpleNamespace(evidence=[]), workspace,
                         BatchSandbox(), ledger, session)


def batch_for(*paths, tools=None, extra=None):
    steps = []
    for index, path in enumerate(paths):
        tool = tools[index] if tools else "fs.read"
        arguments = {} if path is None else {"path": path}
        steps.append(ResearchStep(tool=tool, arguments=arguments, tool_call={
            "id": f"call-{index}", "type": "function",
            "function": {"name": tool.replace(".", "_"), "arguments": json.dumps(arguments)},
        }, **(extra or {})))
    return ResearchBatch(steps=steps, assistant_content="将分别读取这些资料。")


def save_pending(tools, batch):
    tools.session.update(pending_batch=batch.model_dump(exclude_unset=True),
                         pending_batch_start=tools.session["step"], pending_batch_cursor=0)
    tools.save_memory()


async def test_reads_run_in_real_threads_and_history_retains_all_ids(batch_tools, monkeypatch):
    barrier = threading.Barrier(2)
    original = batch_tools.workspace.read
    active, maximum = 0, 0
    lock = threading.Lock()

    def read(**args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=5)
            return original(**args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(batch_tools.workspace, "read", read)
    batch = batch_for("a.md", "b.md")
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert maximum == 2
    observations = batch_tools.session["observations"]
    assert [item["tool_call"]["id"] for item in observations] == ["call-0", "call-1"]
    assert all(len(item["batch_tool_calls"]) == 2 for item in observations)
    assert len({item["batch_id"] for item in observations}) == 1
    assert all(item["assistant_content"] == batch.assistant_content for item in observations)
    assert len(batch_tools.evidence) == 4
    assert batch_tools.ledger.usage("owner")["calls"] == 2
    assert "pending_batch" not in batch_tools.session


async def test_failure_keeps_successful_sibling_and_id(batch_tools):
    batch = batch_for("missing.md", "a.md")
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    observations = batch_tools.session["observations"]
    assert observations[0]["tool_call"]["id"] == "call-0"
    assert observations[0]["result"]["status"] == "failed"
    assert observations[1]["result"]["evidence"]
    assert [item["status"] for item in batch_tools.ledger.runs("owner")] == ["failed", "completed"]


async def test_finish_mixed_with_reads_executes_nothing(batch_tools, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("mixed finish batch must not dispatch a read")

    monkeypatch.setattr(batch_tools.workspace, "read", forbidden)
    batch = batch_for("a.md", None, tools=["fs.read", "finish"])
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert len(batch_tools.session["observations"]) == 2
    assert all(item["result"]["status"] == "failed" for item in batch_tools.session["observations"])
    assert batch_tools.session["phase"] == "local"
    assert not batch_tools.evidence


async def test_budget_charges_each_tool_and_returns_rejected_ids(batch_tools):
    batch_tools.config.max_tool_calls = 1
    batch = batch_for("a.md", "b.md")
    save_pending(batch_tools, batch)
    with pytest.raises(ResearchLimitReached):
        await execute_research_batch(batch_tools, batch)
    observations = batch_tools.session["observations"]
    assert len(observations) == 2
    assert observations[0]["result"]["evidence"]
    assert observations[1]["tool_call"]["id"] == "call-1"
    assert observations[1]["result"]["status"] == "failed"
    assert batch_tools.ledger.usage("owner")["calls"] == 1


async def test_recovery_does_not_reexecute_or_double_charge_committed_call(batch_tools, monkeypatch):
    batch = batch_for("a.md", "b.md")
    save_pending(batch_tools, batch)
    original = batch_tools.ledger.finish
    interrupted = False

    def finish(*args, **kwargs):
        nonlocal interrupted
        original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(batch_tools.ledger, "finish", finish)
    with pytest.raises(asyncio.CancelledError):
        await execute_research_batch(batch_tools, batch)
    monkeypatch.setattr(batch_tools.ledger, "finish", original)
    batch_tools.session.update(batch_tools.ledger.load_session("owner", "round"))
    await execute_research_batch(batch_tools, batch)
    assert len(batch_tools.session["observations"]) == 2
    assert batch_tools.ledger.usage("owner")["calls"] == 2
    assert all(item["result"]["evidence"] for item in batch_tools.session["observations"])


def test_single_saved_step_is_migrated_without_losing_provider_call():
    step = batch_for("a.md").steps[0]
    assert ResearchBatch.model_validate(step.model_dump()).steps[0] == step


async def test_read_concurrency_is_bounded_at_four(batch_tools, monkeypatch):
    barrier = threading.Barrier(4)
    original = batch_tools.workspace.read
    active, maximum = 0, 0
    lock = threading.Lock()

    def read(**args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if args["path"] != "e.md":
                barrier.wait(timeout=5)
            return original(**args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(batch_tools.workspace, "read", read)
    batch = batch_for("a.md", "b.md", "c.md", "d.md", "e.md")
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert maximum == 4
    assert len(batch_tools.session["observations"]) == 5


async def test_cancel_joins_source_workers_and_resume_executes_queued_read(batch_tools, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = batch_tools.workspace.read
    active = 0
    lock = threading.Lock()

    def read(**args):
        nonlocal active
        with lock:
            active += 1
            if active == 4:
                started.set()
        try:
            assert release.wait(timeout=5)
            return original(**args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(batch_tools.workspace, "read", read)
    batch = batch_for("a.md", "b.md", "c.md", "d.md", "e.md")
    save_pending(batch_tools, batch)
    operation = asyncio.create_task(execute_research_batch(batch_tools, batch))
    assert await asyncio.to_thread(started.wait, 5)
    operation.cancel()
    await asyncio.sleep(0.02)
    assert not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert active == 0
    batch_tools.session.update(batch_tools.ledger.load_session("owner", "round"))
    await execute_research_batch(batch_tools, batch)
    observations = batch_tools.session["observations"]
    assert len(observations) == 5
    assert all(item["result"]["status"] == "interrupted" for item in observations[:4])
    assert all("must not be treated as successful" in item["result"]["message"] for item in observations[:4])
    assert observations[4]["result"]["evidence"]
    assert batch_tools.ledger.usage("owner")["calls"] == 5


async def test_mutating_tool_waits_for_reads_and_precedes_next_read(batch_tools, monkeypatch):
    events = []
    original = batch_tools.dispatch

    async def dispatch(step, request_id, timeout, **kwargs):
        events.append((step.tool, "start"))
        if step.tool == "sandbox.exec":
            result = {"status": "completed"}
        else:
            result = await original(step, request_id, timeout, **kwargs)
        events.append((step.tool, "end"))
        return result

    monkeypatch.setattr(batch_tools, "dispatch", dispatch)
    batch = batch_for("a.md", None, "b.md", tools=["fs.read", "sandbox.exec", "fs.read"])
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert events == [(tool, phase) for tool in ("fs.read", "sandbox.exec", "fs.read")
                      for phase in ("start", "end")]


async def test_extra_views_return_correlated_errors_without_overflowing_image_capacity(batch_tools, monkeypatch):
    dispatched = []

    async def dispatch(step, request_id, timeout, **kwargs):
        dispatched.append(step.arguments["path"])
        return {"page_images": [{"path": step.arguments["path"]}], "status": "completed"}

    monkeypatch.setattr(batch_tools, "dispatch", dispatch)
    batch = batch_for("a.md", "b.md", "c.md", tools=["fs.view"] * 3)
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert dispatched == ["a.md", "b.md"]
    assert len(batch_tools.session["page_images"]) == 2
    observations = batch_tools.session["observations"]
    assert len(observations) == 3
    assert observations[2]["tool_call"]["id"] == "call-2"
    assert observations[2]["result"]["status"] == "failed"
    assert 'at most 2 original-page images' in observations[2]["result"]["error"]


async def test_unexpected_tool_error_does_not_discard_other_results(batch_tools, monkeypatch):
    original = batch_tools.workspace.read

    def read(**args):
        if args["path"] == "a.md":
            raise RuntimeError("reader worker failed")
        return original(**args)

    monkeypatch.setattr(batch_tools.workspace, "read", read)
    batch = batch_for("a.md", "b.md")
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    observations = batch_tools.session["observations"]
    assert observations[0]["result"]["error"] == "reader worker failed"
    assert observations[1]["result"]["evidence"]


async def test_successful_last_sibling_does_not_leave_early_stall_transition(batch_tools):
    batch = batch_for("missing1.md", "missing2.md", "missing3.md", "a.md")
    save_pending(batch_tools, batch)
    await execute_research_batch(batch_tools, batch)
    assert batch_tools.session["observations"][-1]["result"]["evidence"]
    assert batch_tools.session["phase"] == "local"
