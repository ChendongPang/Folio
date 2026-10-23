"""A disconnected confirmation still commits and starts its persisted document job."""

import asyncio
import threading

import httpx
import pytest

from folio.api.app import create_app
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.demo import DemoRuntime


async def test_confirm_disconnect_during_job_creation_commits_and_recovers(tmp_path, monkeypatch):
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", lambda _: DemoRuntime())
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )

    executions = asyncio.Queue()
    run_count = 0

    async def execute(store, job_id):
        nonlocal run_count
        run_count += 1
        store.update(job_id, status="running" if run_count == 1 else "paused")
        executions.put_nowait(job_id)
        if run_count == 1:
            # Emulate a worker interrupted by server shutdown. The restarted
            # server must discover this job through the conversation binding.
            await asyncio.Event().wait()

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    app = create_app(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    created = []
    original_create = app.state.store.create

    def slow_create(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test did not release job creation")
        job_id = original_create(*args, **kwargs)
        created.append(job_id)
        completed.set()
        return job_id

    monkeypatch.setattr(app.state.store, "create", slow_create)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            cid = (await client.post("/conversations")).json()["id"]
            uploaded = await client.post(
                f"/conversations/{cid}/sources?filename=deployment.md",
                content="# Deployment\nProduction deployments require two reviewers.",
            )
            assert uploaded.status_code == 201
            planned = await client.post(
                f"/conversations/{cid}/messages",
                json={"text": "编写部署技术方案", "action": "generate"},
            )
            assert "directory.review" in planned.text and "message.failed" not in planned.text
            review = (await client.get(f"/conversations/{cid}")).json()["directory_review"]

            confirmation = asyncio.create_task(
                client.post(
                    f"/conversations/{cid}/messages",
                    json={
                        "text": "确认目录，生成文档",
                        "action": "confirm_directory",
                        "selection_revision": review["revision"],
                    },
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                # Simulate navigating away while the non-cancellable thread is
                # still copying inputs and creating the queued job.
                confirmation.cancel()
                await asyncio.sleep(0)
            finally:
                # A correct handler waits for the thread before propagating
                # cancellation; release it before joining the request task.
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(confirmation, timeout=5)
            assert await asyncio.to_thread(completed.wait, 5)
            assert len(created) == 1
            job_id = created[0]

            state = (await client.get(f"/conversations/{cid}")).json()
            assert state["active_job"] == job_id
            assert state["directory_review"]["phase"] == "generating"
            assert await asyncio.wait_for(executions.get(), timeout=5) == job_id
            assert app.state.store.get(job_id)["status"] == "running"
            with app.state.store.connect() as conn:
                assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1

    restarted = create_app(tmp_path)
    async with restarted.router.lifespan_context(restarted):
        assert await asyncio.wait_for(executions.get(), timeout=5) == job_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
        ) as client:
            restored = (await client.get(f"/conversations/{cid}")).json()
            assert restored["active_job"] == job_id
            assert restored["active_job_status"] == "paused"
            stream = await client.get(f"/jobs/{job_id}/stream")
            assert stream.status_code == 200 and "job.paused" in stream.text
            resumed = await client.post(
                f"/jobs/{job_id}/resume", json={"conversation_id": cid}
            )
            assert resumed.status_code == 200
            assert resumed.json()["job_id"] == job_id
            assert await asyncio.wait_for(executions.get(), timeout=5) == job_id
            assert run_count == 3
            with restarted.state.store.connect() as conn:
                assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
