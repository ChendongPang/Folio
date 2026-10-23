import json
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.artifacts import artifact_filename
from folio.models import JobInput


@pytest.mark.parametrize(
    ("title", "status", "expected"),
    [
        ("软件需求规格说明书", "published", "软件需求规格说明书.docx"),
        ("软件需求规格说明书", "needs_confirmation", "软件需求规格说明书-草稿.docx"),
        ("测试需求说明书", "draft", "测试需求说明书-草稿.docx"),
        ("运行手册.docx.DOCX", "draft", "运行手册-草稿.docx"),
        ("运行手册-草稿.docx", "draft", "运行手册-草稿.docx"),
        ('../a\\b:/c?*<>|"\r\n\x00.docx', "draft", "a_b_c-草稿.docx"),
        ("...", "draft", "文档-草稿.docx"),
        ("CON.docx", "published", "_CON.docx"),
    ],
)
def test_artifact_name_preserves_title_and_is_portable(title, status, expected):
    job = {"input": {"title": title}, "status": status}
    assert artifact_filename(job) == expected
    assert job["input"]["title"] == title


def test_artifact_name_has_a_portable_utf8_length():
    name = artifact_filename({"input": {"title": "测试" * 100}, "status": "draft"})
    assert len(name.encode("utf-8")) < 255
    assert name.endswith("-草稿.docx")


@pytest.mark.parametrize("status", ["published", "draft", "needs_confirmation"])
@pytest.mark.parametrize("title", ["软件需求规格说明书", "旧任务需求说明书", '../运行\\手册.docx'])
def test_download_header_matches_persisted_sse_filename(tmp_path, inputs, status, title):
    app = create_app(tmp_path / "app")
    store = app.state.store
    job_id = store.create(JobInput(**(inputs | {"title": title}), runtime="demo"))
    artifact = tmp_path / "physical-name.docx"
    artifact.write_bytes(b"PK-test-document")
    store.update(job_id, status=status, artifact=str(artifact))
    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}/artifact")
        assert response.status_code == 200 and response.content == b"PK-test-document"
        expected = artifact_filename(store.get(job_id))
        header = unquote(response.headers["content-disposition"])
        assert expected in header
        stream = client.get(f"/jobs/{job_id}/stream").text
        ready = next(
            json.loads(line[6:])
            for line in stream.splitlines()
            if line.startswith("data: ") and '"download_url"' in line
        )
        assert ready["filename"] == expected
        assert ready["download_url"] == f"/jobs/{job_id}/artifact"
