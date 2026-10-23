"""Research audit/download routes enforce job and provenance boundaries."""

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from folio.api.research import install_research_routes
from folio.models import Evidence, JobInput
from folio.research.artifacts import ArtifactManager
from folio.research.workspace import WorkspaceManager


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def research_api(store, inputs, tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_ARTIFACT_KEY_PATH", str(tmp_path / "host" / "signing.key"))
    job = store.create(JobInput(**inputs, runtime="demo"))
    other_job = store.create(JobInput(**inputs, runtime="demo"))
    root = store.root / "jobs" / job / "research"
    sources = root / "sources"
    sources.mkdir(parents=True)
    source = sources / "fields.txt"
    source.write_text("id: required\nname: optional", encoding="utf-8")
    task_root = root / "tasks" / "business" / "round"
    workspace = WorkspaceManager(sources, task_root)
    script = task_root / "scripts" / "count.py"
    script.write_text("print(1)\n")
    output = task_root / "artifacts" / "execution" / "count.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"required_count": 1}', encoding="utf-8")
    receipt = {
        "execution_id": "execution", "status": "completed", "exit_code": 0,
        "image_digest": "sha256:" + "1" * 64, "profile": "base", "dependencies": [],
        "script_path": "scripts/count.py", "script_sha256": sha(script),
        "output_files": [{"path": "artifacts/execution/count.json", "size": output.stat().st_size, "sha256": sha(output)}],
        "source_manifest": [{"path": "fields.txt", "sha256": sha(source)}],
    }
    execution = task_root / ".research" / "executions" / "execution.json"
    execution.parent.mkdir(parents=True)
    execution.write_text(json.dumps(receipt), encoding="utf-8")
    manager = ArtifactManager(task_root, workspace)
    evidence = manager.register(
        execution_id="execution", output_path="artifacts/execution/count.json",
        sources=[Evidence.model_validate(item) for item in workspace.read("fields.txt")["evidence"]],
        methodology="Count required fields once", description="One required field",
    )
    artifact_id = evidence.metadata["derivation"]["artifact_id"]
    manager.record_validation(artifact_id, True, "Verified source, computation and scope", "review-id")
    manifest = task_root / ".research" / "artifact_records" / f"{artifact_id}.json"
    app = FastAPI()
    install_research_routes(app, store)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, store=store, job=job, other_job=other_job, root=root,
            source=source, script=script, output=output, execution=execution,
            manifest=manifest, artifact_id=artifact_id,
            url=f"/jobs/{job}/research/artifacts/{artifact_id}",
        )


def test_valid_artifact_download_and_audit_catalog(research_api):
    fixture = research_api
    response = fixture.client.get(fixture.url)
    assert response.status_code == 200
    assert response.content == fixture.output.read_bytes()
    assert "count.json" in response.headers["content-disposition"]
    report = fixture.client.get(f"/jobs/{fixture.job}/research")
    assert report.status_code == 200
    item = report.json()["artifacts"][0]
    assert item["artifact_id"] == fixture.artifact_id
    assert item["status"] == "verified"
    assert item["download_url"] == fixture.url
    assert str(fixture.store.root) not in report.text


def test_another_job_cannot_download_existing_artifact(research_api):
    fixture = research_api
    url = f"/jobs/{fixture.other_job}/research/artifacts/{fixture.artifact_id}"
    assert fixture.client.get(url).status_code == 404
    assert fixture.client.get(f"/jobs/{fixture.other_job}/research").json()["artifacts"] == []


def test_copying_manifest_to_another_job_cannot_cross_job_boundary(research_api):
    fixture = research_api
    target = fixture.store.root / "jobs" / fixture.other_job / "research" / "tasks" / "copy" / "round" / ".research" / "artifact_records" / fixture.manifest.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(fixture.manifest, target)
    assert fixture.client.get(f"/jobs/{fixture.other_job}/research/artifacts/{fixture.artifact_id}").status_code == 409
    assert fixture.client.get(f"/jobs/{fixture.other_job}/research").json()["artifacts"] == []


@pytest.mark.parametrize("component", ["source", "script", "output", "execution", "manifest"])
def test_changed_provenance_returns_conflict_and_disappears_from_catalog(research_api, component):
    fixture = research_api
    path = getattr(fixture, component)
    path.write_text(path.read_text() + "\nchanged", encoding="utf-8")
    response = fixture.client.get(fixture.url)
    assert response.status_code == 409
    assert fixture.client.get(f"/jobs/{fixture.job}/research").json()["artifacts"] == []


def test_output_symlink_cannot_escape_job(research_api, tmp_path):
    fixture = research_api
    outside = tmp_path / "outside-secret.json"
    outside.write_bytes(fixture.output.read_bytes())
    fixture.output.unlink()
    fixture.output.symlink_to(outside)
    assert fixture.client.get(fixture.url).status_code == 409


@pytest.mark.parametrize("artifact_id", ["not-an-id", "a" * 63, "G" * 64, "a" * 65])
def test_invalid_artifact_identity_returns_not_found(research_api, artifact_id):
    fixture = research_api
    assert fixture.client.get(f"/jobs/{fixture.job}/research/artifacts/{artifact_id}").status_code == 404


def test_missing_job_does_not_create_research_directories(research_api):
    fixture = research_api
    assert fixture.client.get("/jobs/missing/research").status_code == 404
    assert fixture.client.get(f"/jobs/missing/research/artifacts/{fixture.artifact_id}").status_code == 404
    assert not (fixture.store.root / "jobs" / "missing").exists()
