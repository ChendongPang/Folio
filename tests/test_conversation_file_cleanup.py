import json
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.models import JobInput


def bind(app, cid, **values):
    with app.state.store.connect() as conn:
        state = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
        state.update(values)
        conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))


def upload(client, cid, name="facts.md"):
    response = client.post(f"/conversations/{cid}/sources?filename={name}", content="需要保真的原始资料")
    assert response.status_code == 201
    return response.json()["id"]


def owned_folder(root, category, revision, cid, **metadata):
    folder = root / category / revision
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({"conversation_id": cid, **metadata}))
    (folder / "source.md").write_text("目录或风格的冻结资料")
    return folder


def test_cleanup_removes_owned_uploads_all_job_versions_and_private_workdirs(tmp_path, inputs):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        exclusive = upload(client, cid)
        shared = upload(client, cid, "shared.md")
        assert client.put(f"/conversations/{other}/draft", json={"files": [{"id": shared}]}).status_code == 200
        folders = [
            owned_folder(tmp_path, "directory-drafts", "a" * 32, cid, source_ids=[exclusive, shared]),
            owned_folder(tmp_path, "conversation-styles", "b" * 32, cid),
        ]
        jobs = [app.state.store.create(JobInput(**inputs, runtime="demo", document_id="web_" + cid)) for _ in range(2)]
        for job_id in jobs:
            app.state.store.update(job_id, status="failed")
            folder = tmp_path / "jobs" / job_id
            (folder / "document.docx").write_bytes(b"frozen artifact")
            folders.append(folder)
        bind(app, cid, active_job=jobs[-1])
        folders.append(tmp_path / "uploads" / exclusive)
        expected = sum(p.is_file() for folder in folders for p in folder.rglob("*"))
        cache = tmp_path / "source-cache" / "shared-cache.json"
        cache.parent.mkdir(exist_ok=True)
        cache.write_text("global cache")
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["jobs_deleted"] == 2 and result["files_deleted"] == expected
        assert result["files_retained"] > 0 and result["shared_caches_retained"]
        assert all(not folder.exists() for folder in folders)
        assert (tmp_path / "uploads" / shared / "source.md").read_text() == "需要保真的原始资料"
        assert cache.read_text() == "global cache"
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT id FROM web_uploads").fetchone()[0] == shared
        assert client.get(f"/conversations/{other}").json()["files"][0]["id"] == shared


def test_text_mention_is_not_file_or_job_ownership(tmp_path, inputs):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        file_id = upload(client, other)
        # Preserve an unbound upload and independent job, too: no other session
        # needs to reference them for user text to remain non-authoritative.
        assert client.delete(f"/conversations/{other}").status_code == 200
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo"))
        app.state.store.update(job_id, status="failed")
        bind(app, cid, messages=[{"role": "user", "content": f"讨论 {file_id} 和 /jobs/{job_id}/artifact"}])
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 200
        assert response.json()["files_deleted"] == response.json()["jobs_deleted"] == 0
        assert (tmp_path / "uploads" / file_id / "source.md").is_file()
        assert app.state.store.get(job_id)["status"] == "failed"


def test_shared_job_and_other_job_baseline_are_retained(tmp_path, inputs):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo", document_id="web_" + cid))
        app.state.store.update(job_id, status="failed")
        bind(app, cid, active_job=job_id)
        bind(app, other, active_job=job_id)
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 200 and response.json()["jobs_deleted"] == 0
        assert app.state.store.get(job_id)["status"] == "failed"
        assert client.get(f"/conversations/{other}").json()["active_job"] == job_id


def test_retained_directory_protects_its_upload_dependencies(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        file_id = upload(client, cid)
        revision = "a" * 32
        folder = owned_folder(tmp_path, "directory-drafts", revision, cid, source_ids=[file_id])
        # Conservative protection accepts references from another conversation,
        # but that does not grant ownership for selecting deletion candidates.
        bind(app, other, messages=[{"role": "user", "content": f"继续参考目录 {revision}"}])
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 200
        assert response.json()["files_deleted"] == 0
        assert folder.is_dir()
        assert (tmp_path / "uploads" / file_id / "source.md").is_file()
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT 1 FROM web_uploads WHERE id=?", (file_id,)).fetchone()


def test_running_historical_job_blocks_file_cleanup(tmp_path, inputs):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        file_id = upload(client, cid)
        job_id = app.state.store.create(JobInput(**inputs, runtime="demo", document_id="web_" + cid))
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 409
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert (tmp_path / "uploads" / file_id / "source.md").is_file()
        assert app.state.store.job_status(job_id) == "queued"


@pytest.mark.parametrize("failure", ["partial_remove", "database_commit"])
def test_cleanup_failure_restores_files_and_keeps_session(tmp_path, monkeypatch, failure):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        file_id = upload(client, cid)
        folder = tmp_path / "uploads" / file_id
        original = {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        if failure == "partial_remove":
            original_remove = shutil.rmtree

            def failing_remove(path, *args, **kwargs):
                if Path(path) == folder:
                    (folder / "source.md").unlink()
                    raise PermissionError("simulated cleanup failure")
                return original_remove(path, *args, **kwargs)

            monkeypatch.setattr("folio.api.conversation_files.shutil.rmtree", failing_remove)
        else:
            connect = app.state.store.connect

            @contextmanager
            def failed_commit():
                with connect() as conn:
                    deleted = []
                    conn.set_trace_callback(lambda sql: deleted.append(sql) if sql.startswith("DELETE FROM web_conversations") else None)
                    yield conn
                    if deleted:
                        raise sqlite3.OperationalError("simulated commit failure")

            monkeypatch.setattr(app.state.store, "connect", failed_commit)
        response = client.delete(f"/conversations/{cid}?delete_files=true")
        assert response.status_code == 409
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()} == original
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT 1 FROM web_uploads WHERE id=?", (file_id,)).fetchone()


def test_cleanup_rejects_symlink_and_external_upload_paths(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        file_id = upload(client, cid)
        outside = tmp_path / "external.md"
        outside.write_text("must stay")
        folder = app.state.store.root / "uploads" / file_id
        (folder / "external-link.md").symlink_to(outside)
        assert client.delete(f"/conversations/{cid}?delete_files=true").status_code == 409
        assert outside.read_text() == "must stay" and (folder / "source.md").is_file()
        (folder / "external-link.md").unlink()
        with app.state.store.connect() as conn:
            conn.execute("UPDATE web_uploads SET path=? WHERE id=?", (str(outside), file_id))
        assert client.delete(f"/conversations/{cid}?delete_files=true").status_code == 409
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert outside.read_text() == "must stay"


def test_concurrent_new_attachment_binding_cannot_resurrect_deleted_upload(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        file_id = upload(client, cid)
        folder = tmp_path / "uploads" / file_id
        deleting = threading.Event()
        saving = threading.Event()
        remove = shutil.rmtree
        connect = app.state.store.connect

        @contextmanager
        def traced_connect():
            with connect() as conn:
                def trace(sql):
                    # The draft endpoint already selected the upload, but its
                    # final save is waiting for the cleaner's write transaction.
                    if sql == "BEGIN IMMEDIATE" and deleting.is_set():
                        saving.set()
                conn.set_trace_callback(trace)
                yield conn

        def wait_for_binding(path, *args, **kwargs):
            if Path(path) == folder:
                deleting.set()
                assert saving.wait(timeout=5)
            return remove(path, *args, **kwargs)

        monkeypatch.setattr(app.state.store, "connect", traced_connect)
        monkeypatch.setattr("folio.api.conversation_files.shutil.rmtree", wait_for_binding)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.delete, f"/conversations/{cid}?delete_files=true")
            assert deleting.wait(timeout=5)
            response = client.put(f"/conversations/{other}/draft", json={"files": [{"id": file_id}]})
            assert pending.result(timeout=5).status_code == 200
        assert response.status_code == 409
        assert "附件已被删除" in response.json()["detail"]
        assert client.get(f"/conversations/{other}").json()["files"] == []
        assert not folder.exists()
