"""A waiting conversation must neither block nor receive another one's reply."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.decisions import IntentDecision
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.errors import ModelCallFailed


@pytest.mark.parametrize("second_fails", [False, True])
def test_conversations_route_concurrently_and_keep_replies_and_locks_separate(
    tmp_path, monkeypatch, second_fails,
):
    started = {name: Event() for name in ("first", "second")}
    release = {name: Event() for name in started}

    async def classify(config, messages, workflow=None):
        name = messages[-1]["content"]
        started[name].set()
        async with asyncio.timeout(5):
            while not release[name].is_set():
                await asyncio.sleep(0.01)
        return IntentDecision(intent="chat", summary="回答当前会话的问题")

    async def chat(config, messages, attachments, workflow=None, **kwargs):
        name = messages[-1]["content"]
        if name == "second" and second_fails:
            raise ModelCallFailed("test model failure", "timeout")
        yield "message.delta", {"text": f"answer for {name}"}
        yield "message.completed", {"text": f"answer for {name}"}

    monkeypatch.setattr(workspace, "detect_intent", classify)
    monkeypatch.setattr(workspace, "chat_events", chat)
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True),
    )
    with TestClient(create_app(tmp_path)) as client, ThreadPoolExecutor(max_workers=2) as pool:
        ids = {name: client.post("/conversations").json()["id"] for name in started}

        def send(name):
            return client.post(f"/conversations/{ids[name]}/messages", json={"text": name})

        first = pool.submit(send, "first")
        try:
            assert started["first"].wait(3)
            second = pool.submit(send, "second")
            assert started["second"].wait(3), "A waiting intent must not serialize other sessions"
            for cid in ids.values():
                assert client.post(
                    f"/conversations/{cid}/messages", json={"text": "duplicate"},
                ).status_code == 409

            release["second"].set()
            response = second.result(timeout=5)
            assert response.status_code == 200
            assert ("event: message.failed" in response.text) is second_fails
            assert "answer for first" not in response.text
            assert not first.done()
            assert client.post(
                f"/conversations/{ids['first']}/messages", json={"text": "duplicate"},
            ).status_code == 409
            release["first"].set()
            assert "answer for first" in first.result(timeout=5).text
        finally:
            for event in release.values():
                event.set()

        for name, cid in ids.items():
            messages = client.get(f"/conversations/{cid}").json()["messages"]
            assert len(messages) == 2
            assert messages[0]["content"] == name
            if name == "second" and second_fails:
                assert messages[1]["error_code"] == "timeout"
            else:
                assert messages[1]["content"] == f"answer for {name}"
