"""Deleting a session removes persisted history without orphaning active work."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from folio.api.app import create_app
from folio.models import JobInput


def bind_job(app, conversation_id, job_id):
    with app.state.store.connect() as conn:
        conn.execute(
            "UPDATE web_conversations SET data=json_set(data,'$.active_job',?) WHERE id=?",
            (job_id, conversation_id),
        )


def test_deleted_history_stays_deleted_and_shared_upload_survives(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other_id = client.post("/conversations").json()["id"]
        upload = client.post(
            f"/conversations/{cid}/sources?filename=facts.md", content="共享业务资料"
        ).json()
        assert client.put(
            f"/conversations/{other_id}/draft",
            json={"text": "保留这份资料", "files": [{"id": upload["id"]}]},
        ).status_code == 200
        state = client.get(f"/conversations/{cid}").json()
        state["messages"] = [{"role": "user", "content": "删除这份历史"}]
        with app.state.store.connect() as conn:
            conn.execute(
                "UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid)
            )
            upload_path = Path(conn.execute(
                "SELECT path FROM web_uploads WHERE id=?", (upload["id"],)
            ).fetchone()[0])
        response = client.delete(f"/conversations/{cid}")
        assert response.status_code == 200
        assert response.json() == {"deleted": cid}
        assert client.get(f"/conversations/{cid}").status_code == 404
        assert client.delete(f"/conversations/{cid}").status_code == 404
        assert client.put(f"/conversations/{cid}/draft", json={"text": "迟到的保存"}).status_code == 404
        assert upload_path.read_text() == "共享业务资料"
    with TestClient(create_app(tmp_path)) as client:
        assert [row["id"] for row in client.get("/conversations").json()["conversations"]] == [other_id]
        remaining = client.get(f"/conversations/{other_id}").json()
        assert remaining["draft"] == "保留这份资料"
        assert remaining["files"][0]["id"] == upload["id"]
        assert client.get(f"/conversations/{cid}").status_code == 404


def test_delete_requires_same_origin_and_free_conversation_lock(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        assert client.delete(
            f"/conversations/{cid}", headers={"Origin": "http://foreign.test"}
        ).status_code == 403
        with FileLock(tmp_path / f"conversation-{cid}.lock"):
            response = client.delete(f"/conversations/{cid}")
        assert response.status_code == 409
        assert "停止处理" in response.json()["detail"]
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert client.delete(
            f"/conversations/{cid}", headers={"Origin": "http://testserver"}
        ).status_code == 200


@pytest.mark.parametrize("status", ["queued", "running", "pausing"])
def test_delete_rejects_active_document_job(tmp_path, inputs, status):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
        app.state.store.update(job_id, status=status)
        bind_job(app, cid, job_id)
        response = client.delete(f"/conversations/{cid}")
        assert response.status_code == 409
        assert "正在生成文档" in response.json()["detail"]
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert app.state.store.job_status(job_id) == status


@pytest.mark.parametrize("status", ["paused", "failed", "draft", "published", "needs_confirmation"])
def test_delete_idle_job_conversation_preserves_frozen_data(tmp_path, inputs, status):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
        folder = tmp_path / "jobs" / job_id
        artifact = folder / "document.docx"
        artifact.write_bytes(b"existing document artifact")
        app.state.store.update(job_id, status=status, artifact=str(artifact))
        bind_job(app, cid, job_id)
        original_job = app.state.store.get(job_id)
        original_files = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        assert client.delete(f"/conversations/{cid}").status_code == 200
        assert app.state.store.get(job_id) == original_job
        assert {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()} == original_files


def test_delete_waits_for_worker_lock_even_after_failure(tmp_path, inputs):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
        app.state.store.update(job_id, status="failed")
        bind_job(app, cid, job_id)
        with FileLock(tmp_path / f"{job_id}.lock"):
            assert client.delete(f"/conversations/{cid}").status_code == 409
        assert client.delete(f"/conversations/{cid}").status_code == 200


def test_delete_waits_for_background_task_cleanup(tmp_path, inputs, monkeypatch):
    entered = threading.Event()

    async def execute(store, job_id):
        store.update(job_id, status="failed")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    app = create_app(tmp_path)
    # Seed the persisted binding before startup so the normal recovery path
    # launches a task; its status changes before its asynchronous cleanup ends.
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
        bind_job(app, cid, job_id)
    with TestClient(create_app(tmp_path)) as client:
        assert entered.wait(timeout=5)
        assert client.delete(f"/conversations/{cid}").status_code == 409
        assert client.get(f"/conversations/{cid}").status_code == 200
    with TestClient(create_app(tmp_path)) as client:
        assert client.delete(f"/conversations/{cid}").status_code == 200


def test_stale_job_binding_does_not_prevent_deletion(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        bind_job(app, cid, "f" * 32)
        assert client.delete(f"/conversations/{cid}").status_code == 200
