from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.models import JobInput
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime
from folio.store import PublicationConflict


def test_inputs_are_snapshotted(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    inputs["user_request"] = "# Changed\n"
    job = store.get(job_id)
    assert "外汇交易" in job["input"]["user_request"]


async def test_atomic_publication_rejects_stale_baseline(store, inputs):
    # Inject a deterministic runtime to test transaction semantics, not model quality.
    a = store.create(JobInput(**inputs, runtime="llm", document_id="same_doc"))
    b = store.create(JobInput(**inputs, runtime="llm", document_id="same_doc"))
    assert (await Engine(store, DemoRuntime()).run(a))["status"] == "published"
    with pytest.raises(PublicationConflict):
        await Engine(store, DemoRuntime()).run(b)
    with store.connect() as conn:
        assert (
            conn.execute("SELECT published_version FROM documents WHERE id='same_doc'").fetchone()[
                0
            ]
            == a
        )
    assert store.get(b)["status"] == "failed"


def test_unreviewed_document_cannot_publish(store, demo_job, tmp_path):
    artifact = tmp_path / "fake.docx"
    artifact.write_bytes(b"fake")
    with pytest.raises(ValueError, match="Review"):
        store.publish(demo_job, artifact)


async def test_api_enqueue_poll_and_download(tmp_path, inputs):
    app = create_app(tmp_path / "api-data")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/jobs", json=inputs | {"runtime": "demo"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert client.get(f"/jobs/{job_id}/artifact").status_code == 409
        await Engine(app.state.store, DemoRuntime()).run(job_id)
        assert client.get(f"/jobs/{job_id}").json()["status"] == "draft"
        assert client.get(f"/jobs/{job_id}/artifact").content.startswith(b"PK")
        assert len(client.get(f"/jobs/{job_id}/sections").json()) == 2
        events = client.get(f"/jobs/{job_id}/events").json()
        assert events[0]["kind"] == "created"
        assert client.get(f"/jobs/{job_id}/events?after={events[-1]['id']}").json() == []
        assert client.get("/jobs/missing").status_code == 404


def test_api_rejects_ambiguous_knowledge_sources(tmp_path, inputs):
    with TestClient(create_app(tmp_path / "api")) as client:
        response = client.post(
            "/jobs",
            json=inputs | {"ragflow": {"url": "http://example.test/mcp", "dataset_ids": ["ds"]}},
        )
        assert response.status_code == 422


async def test_changing_section_invalidates_review(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    await Engine(store, DemoRuntime()).run(job_id)
    artifact = Path(store.get(job_id)["artifact"])
    state = store.states(job_id)[0]
    state.content.blocks[0].text = "未经验证的变更"
    store.save_section(job_id, state)
    assert not store.get(job_id)["snapshot"].get("review_passed")
    with pytest.raises(ValueError, match="Review"):
        store.publish(job_id, artifact)


async def test_published_state_cannot_be_modified(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    await Engine(store, DemoRuntime()).run(job_id)
    state = store.states(job_id)[0]
    with pytest.raises(ValueError, match="immutable"):
        store.save_section(job_id, state)
    assert store.get(job_id)["status"] == "published"
