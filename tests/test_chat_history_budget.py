"""Chat keeps valid history and enforces the shared token policy at transport."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.decisions import IntentDecision
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.base import RuntimeResult
from folio.runtime.diagnostics import safe_failure_diagnostics
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime


def long_history():
    return [{"role": "user" if index % 2 == 0 else "assistant",
             "content": f"第{index}条对话：" + "交易审批规则需要完整保留。" * 100}
            for index in range(24)]


async def test_intent_keeps_more_than_eight_messages_and_18k_characters(monkeypatch):
    captured = []

    class Runtime:
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session):
            captured.append(context)
            return RuntimeResult('{"intent":"chat","summary":"继续讨论资料"}')

        async def aclose(self):
            pass

    monkeypatch.setattr(workspace, "LLMRuntime", Runtime)
    history = long_history()
    assert len(json.dumps(history, ensure_ascii=False)) > 18000
    result = await workspace.detect_intent(None, history)
    assert result.intent == "chat"
    assert captured[0]["messages"] == history
    assert captured[0]["latest_user_message"] == history[-2]["content"]


def test_message_endpoint_retains_all_valid_history_for_intent_and_chat(tmp_path, monkeypatch):
    captured = {}

    async def intent(config, messages, workflow=None):
        captured["intent"] = messages
        return IntentDecision(intent="chat", summary="继续普通聊天")

    async def chat(config, messages, attachments, workflow=None, **kwargs):
        captured["chat"] = messages
        yield "message.completed", {"text": "完整历史已保留。"}

    monkeypatch.setattr(workspace, "detect_intent", intent)
    monkeypatch.setattr(workspace, "chat_events", chat)
    ModelSettingsStore(tmp_path).save(ModelSettingsInput(
        base_url="http://unused.test/v1", model="test", no_auth=True))
    app = create_app(tmp_path)
    history = long_history()
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        with app.state.store.connect() as connection:
            state = json.loads(connection.execute(
                "SELECT data FROM web_conversations WHERE id=?", (cid,),
            ).fetchone()[0])
            state["messages"] = history + [
                {"role": "assistant", "content": "旧错误", "error_code": "timeout"},
                {"role": "assistant", "content": "旧中断", "interrupted": True},
            ]
            connection.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
        response = client.post(f"/conversations/{cid}/messages", json={"text": "继续讨论审批规则。"})
        assert "message.completed" in response.text
    for messages in captured.values():
        assert [(item["role"], item["content"]) for item in messages] == [
            (item["role"], item["content"]) for item in history
        ] + [("user", "继续讨论审批规则。")]
    assert set(captured) == {"intent", "chat"}


def stream_response():
    chunk = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


async def make_runtime(tmp_path, handler, **settings):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True,
                                  **settings))
    runtime = LLMRuntime(store.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return runtime


@pytest.mark.parametrize("estimated,window,accepted", [
    (600000, 922000, True), (600001, 922000, False), (500000, 600000, False),
])
async def test_raw_stream_checks_input_and_combined_window_before_http(
    tmp_path, monkeypatch, estimated, window, accepted,
):
    sent, counted = [], []

    def handler(request):
        sent.append(json.loads(request.content))
        return stream_response()

    def estimate(messages, tools, **kwargs):
        counted.append(messages)
        return estimated

    runtime = await make_runtime(tmp_path, handler, context_window=window)
    monkeypatch.setattr("folio.runtime.llm.estimate_input", estimate)
    messages = [{"role": "user", "content": "保留完整聊天内容"}]
    try:
        if accepted:
            events = [event async for event in runtime.stream(messages)]
            assert events[-1][1]["text"] == "{}"
            assert sent[0]["messages"] == messages and sent[0]["max_tokens"] == 128000
        else:
            with pytest.raises(ModelCallFailed) as caught:
                _ = [event async for event in runtime.stream(messages)]
            assert caught.value.code == "context_limit"
            assert safe_failure_diagnostics(caught.value)["estimated_input_tokens"] == estimated
            assert not sent
        assert counted == [messages]
    finally:
        await runtime.aclose()


async def test_measured_completion_does_not_reestimate_with_the_raw_stream_policy(tmp_path, monkeypatch):
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 500000})
        return stream_response()

    def forbidden_estimate(*args, **kwargs):
        pytest.fail("The server-tokenizer result must not be replaced by another local estimate")

    runtime = await make_runtime(tmp_path, handler, context_window=922000)
    runtime.capacity.ready = runtime.capacity.counter = True
    monkeypatch.setattr("folio.runtime.llm.estimate_input", forbidden_estimate)
    try:
        result = await runtime.complete("validate", {}, {"type": "object"}, "checked")
        assert json.loads(result.content) == {}
        assert sent == ["/tokenize", "/v1/chat/completions"]
    finally:
        await runtime.aclose()
