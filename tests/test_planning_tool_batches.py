"""Exercise real concurrent readers, ordered commits and native batch round trips."""

import asyncio
import json
import threading

import pytest

from folio.research.planning import PlanningResearchLimits, research_for_directory
from folio.research.transport import research_messages
from folio.research.workspace import WorkspaceManager
from folio.runtime.base import RuntimeResult


def batch(*calls):
    return RuntimeResult("正在核对资料。", tool_calls=[{
        "id": f"call_{index}_{tool}", "type": "function", "function": {
            "name": tool, "arguments": json.dumps({"summary": "核对资料", **args}, ensure_ascii=False),
        },
    } for index, (tool, args) in enumerate(calls)])


class Runtime:
    def __init__(self, responses):
        self.responses, self.contexts = iter(responses), []

    async def complete(self, task, context, schema, session_key):
        self.contexts.append(context)
        response = next(self.responses)
        return response(context) if callable(response) else response


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "a.md").write_text("# 库存\n撤单后释放库存。\n", encoding="utf-8")
    (root / "b.md").write_text("# 审批\n审批必须保留日志。\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_parallel_reads_finish_with_one_assistant_batch_and_all_matching_results(sources, tmp_path, monkeypatch):
    rendezvous = threading.Barrier(2, timeout=5)
    original = WorkspaceManager.read

    def simultaneous(self, **args):
        rendezvous.wait()  # A sequential executor cannot satisfy this barrier.
        return original(self, **args)

    monkeypatch.setattr(WorkspaceManager, "read", simultaneous)

    def finish(context):
        reads = [entry for entry in context["observations"] if entry["tool"] == "fs.read"]
        assert len(reads) == 2 and all("error" not in entry["result"] for entry in reads)
        messages = research_messages(context, {})
        assistant_index = next(index for index, message in enumerate(messages)
                               if message.get("tool_calls", [{}])[0].get("id") == "call_0_fs_read")
        assert len(messages[assistant_index]["tool_calls"]) == 2
        assert [entry["tool_call_id"] for entry in messages[assistant_index + 1:assistant_index + 3]] == [
            "call_0_fs_read", "call_1_fs_read"]
        assert "库存" in messages[assistant_index + 1]["content"]
        assert "审批" in messages[assistant_index + 2]["content"]
        return batch(("finish", {
            "summary": "已核对库存与审批要求。",
            "requirements": [{"requirement_id": "core", "text": "覆盖库存与审批", "evidence_ids": [
                item["evidence_id"] for entry in reads for item in entry["result"]["evidence"]]}],
        }))

    runtime = Runtime([
        batch(("fs_inspect", {"file": "F1"}), ("fs_inspect", {"file": "F2"})),
        batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"})), finish,
    ])
    output = tmp_path / "research"
    report = await research_for_directory(runtime, sources, output, "编写需求目录")
    assert report["status"] == "ready"
    assert report["calls"] == 3 and report["tool_calls"] == 5
    assert [entry["delivered_characters"] for entry in report["files"]] == [12, 13]
    assert (output / "events/research_002_tool_001-tool.json").is_file()
    request = json.loads((output / "events/research_003-request.json").read_text())
    assert request["parallel_tool_calls"] is True


@pytest.mark.asyncio
async def test_one_worker_failure_preserves_sibling_and_returns_both_ids(sources, tmp_path, monkeypatch):
    original = WorkspaceManager.read

    def broken(self, **args):
        if args["path"] == "a.md":
            raise OSError("原文读取失败")
        return original(self, **args)

    monkeypatch.setattr(WorkspaceManager, "read", broken)

    def verify(context):
        observations = context["observations"]
        assert len(observations) == 2
        assert observations[0]["result"]["error"] == "原文读取失败"
        assert observations[1]["result"]["evidence"]
        results = [message for message in research_messages(context, {}) if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["call_0_fs_read", "call_1_fs_read"]
        return batch(("fs_inspect", {"file": "F2"}))

    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"})), verify])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "编写目录",
                                          limits=PlanningResearchLimits(max_calls=2))
    assert [item["read"] for item in report["files"]] == [False, True]


@pytest.mark.asyncio
async def test_finish_mixed_with_read_rejects_whole_batch_with_all_ids(sources, tmp_path):
    def verify(context):
        assert len(context["observations"]) == 2
        results = [message for message in research_messages(context, {}) if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["call_0_fs_read", "call_1_finish"]
        assert all('finish must be called alone' in message["content"] for message in results)
        return batch(("fs_inspect", {"file": "F1"}))

    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("finish", {})), verify])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "编写目录",
                                          limits=PlanningResearchLimits(max_calls=2))
    assert not any(item["read"] for item in report["files"])


@pytest.mark.asyncio
async def test_batch_reserves_shared_evidence_budget(sources, tmp_path, monkeypatch):
    original, reads = WorkspaceManager.read, []

    def tracked(self, **args):
        reads.append(args)
        return original(self, **args)

    monkeypatch.setattr(WorkspaceManager, "read", tracked)
    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"}))])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "编写目录",
                                          limits=PlanningResearchLimits(max_calls=1, max_evidence_chars=2000))
    # Small chapters share the actual allowance; a first read does not reserve
    # the entire maximum and incorrectly reject the next small chapter.
    assert len(reads) == 2
    assert sum(len(item["text"]) for item in report["evidence"]) <= 2000
    assert all(item["read"] for item in report["files"])


@pytest.mark.asyncio
async def test_batch_cannot_exceed_remaining_tool_count(sources, tmp_path):
    def verify(context):
        assert all('remaining tool-call budget' in entry["result"]["error"] for entry in context["observations"])
        return batch(("fs_inspect", {"file": "F1"}))

    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"})), verify])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "编写目录",
                                          limits=PlanningResearchLimits(max_tool_calls=1))
    assert report["tool_calls"] == 1 and report["calls"] == 2
    assert not any(item["read"] for item in report["files"])


@pytest.mark.asyncio
async def test_cancellation_drains_entire_batch_and_saves_undelivered_results(sources, tmp_path, monkeypatch):
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    started = []
    original = WorkspaceManager.read

    def delayed(self, **args):
        with lock:
            started.append(args["path"])
            if len(started) == 2:
                entered.set()
        assert release.wait(5)
        return original(self, **args)

    monkeypatch.setattr(WorkspaceManager, "read", delayed)
    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"}))])
    output = tmp_path / "research"
    task = asyncio.create_task(research_for_directory(runtime, sources, output, "编写目录"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    report = json.loads((output / "report.json").read_text())
    assert all(item["read"] and item["delivered_characters"] == 0 for item in report["files"])
    assert report["status"] == "partial"
    for index in (1, 2):
        assert (output / f"events/research_001_tool_{index:03d}-tool.json").is_file()


@pytest.mark.asyncio
async def test_progress_cancellation_preserves_all_completed_batch_results(sources, tmp_path):
    updates = []

    async def progress(message):
        if message.startswith("已取得"):
            updates.append(message)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    runtime = Runtime([batch(("fs_read", {"file": "F1"}), ("fs_read", {"file": "F2"}))])
    output = tmp_path / "research"
    task = asyncio.create_task(research_for_directory(
        runtime, sources, output, "编写目录", on_progress=progress))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(updates) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "partial"
    assert all(item["read"] and item["delivered_characters"] == 0 for item in report["files"])
    results = [json.loads((output / f"events/research_001_tool_{index:03d}-tool.json").read_text())
               for index in (1, 2)]
    assert [entry["tool_call"]["id"] for entry in results] == ["call_0_fs_read", "call_1_fs_read"]
    assert all("error" not in entry["result"] and entry["result"]["evidence"] for entry in results)
