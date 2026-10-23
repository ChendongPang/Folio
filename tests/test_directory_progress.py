"""Directory progress is readable, survives refresh, and never carries model payloads."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from folio.api.directory_progress import DirectoryProgress
from folio.api.workspace import detect_intent
from folio.runtime.activity import ModelActivity, model_activity_task
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime

pytest_plugins = ("test_directory_review",)


def frames(response):
    result = []
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        event = next((line.removeprefix("event: ") for line in lines if line.startswith("event: ")), None)
        data = next((line.removeprefix("data: ") for line in lines if line.startswith("data: ")), None)
        if event and data:
            result.append((event, json.loads(data)))
    return result


def test_model_status_updates_one_row_and_cannot_forward_payloads(monkeypatch):
    clock = [100]
    monkeypatch.setattr("folio.api.directory_progress.monotonic", lambda: clock[0])
    progress = DirectoryProgress()
    progress.update("正在阅读《招标文件》的技术需求。")
    first = progress.event()
    clock[0] += 17
    progress.update({"status": "thinking", "reasoning": "PRIVATE_REASONING", "text": "RAW_JSON"})
    current = progress.event()
    assert current["activity"]["id"] == first["activity"]["id"]
    assert current["model_status"] == "thinking"
    assert current["elapsed_seconds"] == current["step_elapsed_seconds"] == 17
    assert len(progress.activities) == 1
    assert "PRIVATE_REASONING" not in json.dumps(current)
    assert "RAW_JSON" not in json.dumps(current)
    progress.update("正在安排技术响应章节。")
    assert progress.event()["step_elapsed_seconds"] == 0
    assert progress.event()["model_status"] is None
    assert progress.activities[0]["status"] == "completed"
    assert progress.finish(completed=False)[-1]["status"] == "failed"


def test_live_directory_events_preserve_actions_and_refresh_history(setup, inputs, monkeypatch):
    app, _ = setup

    class VisibleRuntime(DemoRuntime):
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            with model_activity_task(task):
                activity = ModelActivity()
                for status in ("waiting", "connected", "thinking", "responding"):
                    activity.emit(status)
            return await super().complete(task, context, schema, session_key)

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", VisibleRuntime)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        source = Path(inputs["knowledge_path"]) / "rules.md"
        uploaded = client.post(f"/conversations/{cid}/sources", params={"filename": "业务规则.md"}, content=source.read_bytes())
        assert uploaded.status_code == 201
        response = client.post(f"/conversations/{cid}/messages", json={"text": "编制交易需求说明书", "action": "generate"})
        events = frames(response)
        assert any(kind == "directory.review" for kind, _ in events)
        progress = [data for kind, data in events if kind == "directory.update"]
        assert {item["model_status"] for item in progress} >= {"waiting", "connected", "thinking", "responding"}
        ids = {item["activity"]["id"] for item in progress}
        assert len(ids) < len(progress), "Transport updates must reuse the current action row"
        text = "\n".join(item["text"] for item in progress)
        assert "业务规则.md" in text
        assert "fs.inspect" not in text and "fs.read" not in text
        assert uploaded.json()["id"] not in text
        saved = client.get(f"/conversations/{cid}").json()
        history = saved["messages"][-1]["activities"]
        assert len(history) == len(ids)
        assert all(item["status"] == "completed" for item in history)
        assert "业务规则.md" in "\n".join(item["text"] for item in history)


def test_directory_failure_keeps_the_completed_action_history(setup, inputs, monkeypatch):
    app, _ = setup

    async def fail(self, config, conversation_id, attachments, request, **kwargs):
        kwargs["on_progress"]("正在查看《业务规则.md》的章节结构。")
        raise TimeoutError()

    monkeypatch.setattr("folio.source_documents.SourceDirectoryDrafts.create_from_sources", fail)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        source = Path(inputs["knowledge_path"]) / "rules.md"
        client.post(f"/conversations/{cid}/sources", params={"filename": "业务规则.md"}, content=source.read_bytes())
        response = client.post(f"/conversations/{cid}/messages", json={"text": "编制交易需求说明书", "action": "generate"})
        assert "message.failed" in response.text
        message = client.get(f"/conversations/{cid}").json()["messages"][-1]
        assert message["activities"][-1]["text"] == "正在查看《业务规则.md》的章节结构。"
        assert message["activities"][-1]["status"] == "failed"


async def test_activity_history_does_not_enter_model_context_or_evict_user_requirements(monkeypatch):
    contexts = []

    class Runtime:
        def __init__(self, config):
            pass

        async def complete(self, task, context, *args):
            contexts.append(context)
            return RuntimeResult(json.dumps({"intent": "requirements", "summary": "补充要求"}))

        async def aclose(self):
            pass

    monkeypatch.setattr("folio.api.workspace.LLMRuntime", Runtime)
    messages = [
        {"role": "user", "content": "必须说明主管审批要求"},
        {"role": "assistant", "content": "目录完成", "activities": [{"text": "过程记录" * 10000}]},
        {"role": "user", "content": "增加异常场景"},
    ]
    await detect_intent(None, messages)
    assert len(contexts[0]["messages"]) == 3
    assert contexts[0]["messages"][0]["content"] == "必须说明主管审批要求"
    assert all("activities" not in item for item in contexts[0]["messages"])
