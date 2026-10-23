"""The entrance repairs format once, preserves intent, and logs no private bodies."""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


def install_runtime(monkeypatch, results):
    calls, closed = [], []

    class Runtime:
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            calls.append(json.loads(json.dumps(context)))
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return await result()
            return RuntimeResult(result)

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(workspace, "LLMRuntime", Runtime)
    return calls, closed


def configured_app(root):
    ModelSettingsStore(root).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    return create_app(root)


@pytest.mark.parametrize(
    "invalid",
    [
        "not JSON",
        '["generate"]',
        '{"action":"generate"}',
        '{"intent":"generate","summary":""}',
    ],
)
async def test_format_repair_once_keeps_latest_request_and_original_response(monkeypatch, invalid):
    valid = json.dumps({"intent": "chat", "summary": "讨论生成方式，尚未要求开始"})
    calls, closed = install_runtime(monkeypatch, [invalid, valid])
    messages = [{"role": "user", "content": "生成文档是怎样工作的？"}]
    result = await workspace.detect_intent(
        None, messages, {"generation_requires_confirmation": True}
    )
    assert result.intent == "chat"  # Never reinterpret the malformed action as generate.
    assert len(calls) == 2 and closed == [True]
    assert "intent_format_retry" not in calls[0]
    assert calls[1]["intent_format_retry"] is True
    assert calls[1]["previous_invalid_intent"] == invalid
    assert calls[1]["format_feedback"]
    assert calls[1]["messages"] == messages
    assert calls[1]["latest_user_message"] == messages[0]["content"]
    assert calls[1]["workflow"] == calls[0]["workflow"]
    if invalid == '{"action":"generate"}':
        assert {"field": "intent", "type": "missing"} in calls[1]["format_feedback"]
        assert {"field": "summary", "type": "missing"} in calls[1]["format_feedback"]


async def test_second_invalid_response_stops_and_bounds_repair_payload(monkeypatch):
    calls, closed = install_runtime(monkeypatch, ["sensitive " * 2000, '{"action":"generate"}'])
    with pytest.raises(ModelCallFailed) as caught:
        await workspace.detect_intent(None, [{"role": "user", "content": "开始"}])
    assert caught.value.code == "protocol"
    assert caught.value.diagnostics["attempts"] == 2
    assert len(calls) == 2 and closed == [True]
    assert len(calls[1]["previous_invalid_intent"]) == workspace.INTENT_REPAIR_RESULT_CHARS
    assert "sensitive" not in str(caught.value)


async def test_transport_error_is_not_format_retry(monkeypatch):
    calls, closed = install_runtime(monkeypatch, [ModelCallFailed("secret body", "authentication")])
    with pytest.raises(ModelCallFailed) as caught:
        await workspace.detect_intent(None, [{"role": "user", "content": "你好"}])
    assert caught.value.code == "authentication"
    assert len(calls) == 1 and closed == [True]


async def test_deadline_bounds_the_whole_intent_operation(monkeypatch):
    async def slow():
        await asyncio.sleep(10)

    monkeypatch.setattr(workspace, "INTENT_TIMEOUT_SECONDS", 0.01)
    calls, closed = install_runtime(monkeypatch, ['{"action":"generate"}', slow])
    with pytest.raises(ModelCallFailed) as caught:
        await workspace.detect_intent(None, [{"role": "user", "content": "开始"}])
    assert caught.value.code == "timeout"
    assert caught.value.diagnostics == {"attempts": 2, "timeout_seconds": 0.01}
    assert len(calls) == 2 and closed == [True]


def test_repaired_generate_directly_plans_directory_but_requires_confirmation(
    tmp_path, inputs, monkeypatch
):
    valid = '{"intent":"generate","summary":"用户请求编写文档"}'
    calls, _ = install_runtime(monkeypatch, ['{"action":"generate"}', valid] * 3)

    class Planner(DemoRuntime):
        def __init__(self, config):
            pass

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", Planner)
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )
    app = configured_app(tmp_path / "app")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        path = Path(inputs["knowledge_path"]) / "rules.md"
        uploaded = client.post(
            f"/conversations/{cid}/sources",
            params={"filename": path.name},
            content=path.read_bytes(),
        )
        assert uploaded.status_code == 201
        revision = None
        for text in ("编写文档", "开始生成", "直接开始生成"):
            response = client.post(f"/conversations/{cid}/messages", json={"text": text})
            assert "directory.review" in response.text
            assert "job.created" not in response.text and "template.selection" not in response.text
            saved = client.get(f"/conversations/{cid}").json()
            assert saved["directory_review"]["phase"] == "directory_review"
            assert saved["directory_review"]["directory"]["sections"]
            if revision:
                assert saved["directory_review"]["revision"] == revision
            revision = saved["directory_review"]["revision"]
            assert saved["active_job"] is None
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert len(calls) == 6


def test_format_failure_logs_original_fields_but_no_private_response(tmp_path, monkeypatch, caplog):
    secret = "sk-fake-secret-private-material"
    invalid = json.dumps({"action": "generate", secret: "source-body"})
    calls, _ = install_runtime(monkeypatch, [invalid, invalid])
    app = configured_app(tmp_path)
    with (
        caplog.at_level(logging.WARNING, logger="folio.api.workspace"),
        TestClient(app) as client,
    ):
        cid = client.post("/conversations").json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"text": "user-private-text"})
        saved = client.get(f"/conversations/{cid}").json()
    assert len(calls) == 2 and "message.failed" in response.text
    assert "job.created" not in response.text
    details = saved["messages"][-1]["error_diagnostics"]
    assert details["conversation_id"] == cid and details["stage"] == "intent"
    assert details["cause_type"] == "ValidationError" and details["code"] == "protocol"
    assert details["attempts"] == 2 and details["output_chars"] == len(invalid)
    assert {"field": "intent", "type": "missing"} in details["format_errors"]
    assert "workspace_intent_format_retry" in caplog.text
    assert "workspace_request_failed" in caplog.text and cid in caplog.text
    assert "user-private-text" not in caplog.text
    for private in (secret, "source-body"):
        assert private not in caplog.text + response.text + json.dumps(saved)
    assert workspace.conversation_log_id.get() is None


def test_chat_failure_keeps_actual_stage_and_safe_numeric_diagnostics(
    tmp_path, monkeypatch, caplog
):
    install_runtime(monkeypatch, ['{"intent":"chat","summary":"正常聊天"}'])

    async def broken_chat(*args, **kwargs):
        error = ModelCallFailed("private-upstream-body", "timeout")
        error.diagnostics = {
            "phase": "first_content",
            "received_chars": 7,
            "elapsed_seconds": 3.5,
            "api_key": "sk-fake-secret",
            "raw_body": "sensitive",
            "unknown": 42,
        }
        raise error
        yield

    monkeypatch.setattr(workspace, "chat_events", broken_chat)
    with (
        caplog.at_level(logging.ERROR, logger="folio.api.workspace"),
        TestClient(configured_app(tmp_path)) as client,
    ):
        cid = client.post("/conversations").json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"text": "你好"})
        saved = client.get(f"/conversations/{cid}").json()
    details = saved["messages"][-1]["error_diagnostics"]
    assert details["stage"] == "chat" and details["transport_phase"] == "first_content"
    assert details["received_chars"] == 7 and details["elapsed_seconds"] == 3.5
    for private in ("sk-fake-secret", "private-upstream-body", "sensitive", "unknown"):
        assert private not in caplog.text + response.text + json.dumps(details)
