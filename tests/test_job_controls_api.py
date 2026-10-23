"""Conversation-scoped pause/resume controls never start real model calls."""

import asyncio
import json
import threading
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.workspace import job_events
from folio.models import JobInput


def attach_job(client, app, inputs, *, status="queued"):
    cid = client.post("/conversations").json()["id"]
    job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
    app.state.store.update(job_id, status=status)
    with app.state.store.connect() as conn:
        row = conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()
        state = json.loads(row[0])
        state["active_job"] = job_id
        state["directory_review"] = {"phase": "generating", "revision": uuid4().hex}
        state["messages"] = [{"role": "user", "content": "生成文档"}]
        conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
    return cid, job_id


def wait_until(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, "background executor did not reach expected state"
        sleep(0.01)


def test_queued_pause_persists_without_losing_directory_or_conversation(tmp_path, inputs):
    app = create_app(tmp_path / "web")
    with TestClient(app) as client:
        cid, job_id = attach_job(client, app, inputs)
        before = client.get(f"/conversations/{cid}").json()
        for _ in range(2):
            response = client.post(f"/jobs/{job_id}/pause", json={"conversation_id": cid})
            assert response.status_code == 200 and response.json()["status"] == "paused"
        after = client.get(f"/conversations/{cid}").json()
        assert after["active_job"] == job_id and after["active_job_status"] == "paused"
        assert after["directory_review"] == before["directory_review"]
        assert after["messages"] == before["messages"]
        streamed = client.get(f"/jobs/{job_id}/stream").text
        assert "event: job.paused" in streamed and "job.failed" not in streamed
    with TestClient(create_app(app.state.store.root)) as client:
        assert client.get(f"/conversations/{cid}").json()["active_job_status"] == "paused"


@pytest.mark.parametrize("status", ["queued", "running", "pausing", "paused"])
def test_unfinished_job_locks_only_its_own_composer(tmp_path, inputs, status):
    app = create_app(tmp_path / "web")
    with TestClient(app) as client:
        cid, _ = attach_job(client, app, inputs, status=status)
        assert (
            client.post(f"/conversations/{cid}/messages", json={"text": "改写"}).status_code == 409
        )
        assert client.put(f"/conversations/{cid}/draft", json={"text": "新输入"}).status_code == 409
        assert (
            client.put(f"/conversations/{cid}/writing-guide", json={"guide_id": "srs"}).status_code
            == 409
        )
        assert (
            client.post(
                f"/conversations/{cid}/sources?filename=blocked.md", content=b"blocked"
            ).status_code
            == 409
        )
        new = client.post("/conversations", json={"guide_id": "urd"})
        assert new.status_code == 201
        other = new.json()["id"]
        assert (
            client.put(f"/conversations/{other}/draft", json={"text": "另一个任务"}).status_code
            == 200
        )
        assert (
            client.put(
                f"/conversations/{other}/writing-guide", json={"guide_id": "srs"}
            ).status_code
            == 200
        )
        assert client.get(f"/conversations/{other}").json()["active_job_status"] is None


def test_controls_validate_origin_ownership_and_terminal_state(tmp_path, inputs):
    app = create_app(tmp_path / "web")
    with TestClient(app) as client:
        cid, job_id = attach_job(client, app, inputs)
        other = client.post("/conversations").json()["id"]
        for action in ("pause", "resume"):
            url = f"/jobs/{job_id}/{action}"
            assert (
                client.post(
                    url, json={"conversation_id": cid}, headers={"Origin": "https://evil.test"}
                ).status_code
                == 403
            )
            assert client.post(url, json={"conversation_id": other}).status_code == 409
            assert client.post(url, json={"conversation_id": uuid4().hex}).status_code == 404
            assert (
                client.post(
                    f"/jobs/{uuid4().hex}/{action}", json={"conversation_id": cid}
                ).status_code
                == 404
            )
        assert app.state.store.get(job_id)["status"] == "queued"
        app.state.store.update(job_id, status="draft")
        for action in ("pause", "resume"):
            assert (
                client.post(f"/jobs/{job_id}/{action}", json={"conversation_id": cid}).status_code
                == 409
            )


async def test_stream_announces_pausing_then_drains_progress_before_paused(store, demo_job):
    store.update(demo_job, status="running")
    store.request_pause(demo_job)
    stream = job_events(store, demo_job)
    snapshot = await anext(stream)
    assert "event: job.snapshot" in snapshot and '"status": "pausing"' in snapshot
    assert "event: job.pausing" in await anext(stream)
    store.work_event(demo_job, "最后一节已保存")
    store.acknowledge_pause(demo_job)
    remaining = [event async for event in stream]
    assert any("最后一节已保存" in event for event in remaining)
    assert "event: job.paused" in remaining[-1]
    assert not any("job.failed" in event or "artifact.ready" in event for event in remaining)


def test_sessions_run_concurrently_with_independent_pause_and_single_job_ownership(
    tmp_path, inputs, monkeypatch
):
    calls = []
    releases = {}

    async def execute(store, job_id):
        calls.append(job_id)
        store.update(job_id, status="running")
        while not releases[job_id].is_set():
            if store.get(job_id)["status"] == "pausing":
                store.acknowledge_pause(job_id)
                return
            await asyncio.sleep(0.01)
        store.update(job_id, status="draft")

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    app = create_app(tmp_path / "web")
    with TestClient(app) as client:
        cid1, job1 = attach_job(client, app, inputs, status="paused")
        cid2, job2 = attach_job(client, app, inputs, status="paused")
        releases.update({job1: threading.Event(), job2: threading.Event()})
        assert (
            client.post(f"/jobs/{job1}/resume", json={"conversation_id": cid1}).status_code == 200
        )
        wait_until(lambda: calls == [job1])
        for _ in range(2):
            assert (
                client.post(f"/jobs/{job1}/resume", json={"conversation_id": cid1}).status_code
                == 200
            )
            assert (
                client.post(f"/jobs/{job2}/resume", json={"conversation_id": cid2}).status_code
                == 200
            )
        wait_until(lambda: calls == [job1, job2])
        assert app.state.store.get(job1)["status"] == "running"
        assert app.state.store.get(job2)["status"] == "running"
        paused = client.post(f"/jobs/{job2}/pause", json={"conversation_id": cid2})
        assert paused.json()["status"] == "pausing"
        wait_until(lambda: app.state.store.get(job2)["status"] == "paused")
        assert client.get(f"/conversations/{cid2}").json()["active_job_status"] == "paused"
        assert app.state.store.get(job1)["status"] == "running"
        assert calls == [job1, job2]
        assert (
            client.post(f"/jobs/{job2}/resume", json={"conversation_id": cid2}).status_code == 200
        )
        wait_until(lambda: calls == [job1, job2, job2])
        releases[job2].set()
        wait_until(lambda: app.state.store.get(job2)["status"] == "draft")
        assert app.state.store.get(job1)["status"] == "running"
        releases[job1].set()
        wait_until(lambda: app.state.store.get(job1)["status"] == "draft")
        assert calls == [job1, job2, job2]


def test_startup_recovers_sessions_concurrently_and_shutdown_drains_all(
    tmp_path, inputs, monkeypatch
):
    calls = []
    cancelled = set()

    async def execute(store, job_id):
        calls.append(job_id)
        store.update(job_id, status="running")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(job_id)

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    root = tmp_path / "web"
    app = create_app(root)
    with TestClient(app) as client:
        _, job1 = attach_job(client, app, inputs, status="queued")
        _, job2 = attach_job(client, app, inputs, status="running")
    assert not calls

    restarted = create_app(root)
    with TestClient(restarted):
        wait_until(lambda: len(calls) == 2)
        assert set(calls) == {job1, job2}
        assert restarted.state.store.get(job1)["status"] == "running"
        assert restarted.state.store.get(job2)["status"] == "running"
        assert not cancelled
    assert cancelled == {job1, job2}


def test_resume_during_paused_executor_cleanup_is_scheduled_once(tmp_path, inputs, monkeypatch):
    calls = []
    paused = threading.Event()
    release_cleanup = threading.Event()

    async def execute(store, job_id):
        calls.append(job_id)
        store.update(job_id, status="running")
        if len(calls) == 1:
            while store.get(job_id)["status"] != "pausing":
                await asyncio.sleep(0.01)
            store.acknowledge_pause(job_id)
            paused.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.01)
        else:
            store.update(job_id, status="draft")

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    app = create_app(tmp_path / "web")
    with TestClient(app) as client:
        cid, job_id = attach_job(client, app, inputs, status="paused")
        assert (
            client.post(f"/jobs/{job_id}/resume", json={"conversation_id": cid}).status_code == 200
        )
        wait_until(lambda: app.state.store.get(job_id)["status"] == "running")
        response = client.post(f"/jobs/{job_id}/pause", json={"conversation_id": cid})
        assert response.json()["status"] == "pausing"
        assert paused.wait(timeout=3)
        for _ in range(2):
            assert (
                client.post(f"/jobs/{job_id}/resume", json={"conversation_id": cid}).status_code
                == 200
            )
        assert calls == [job_id]
        release_cleanup.set()
        wait_until(lambda: app.state.store.get(job_id)["status"] == "draft")
        assert calls == [job_id, job_id]
