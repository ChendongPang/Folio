"""The interface preference does not override the requested document language."""

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.artifacts import artifact_filename
from folio.requirements import substantive_text


@pytest.mark.parametrize(("user_request", "ui_language", "expected"), [
    ("Write a report in English.", "zh-CN", "en"),
    ("请根据资料编写报告。", "en", "zh-CN"),
    ("请用英文编写报告。", "zh-CN", "en"),
    ("Write a report in Chinese.", "en", "zh-CN"),
])
def test_create_job_freezes_requested_output_language(tmp_path, inputs, user_request, ui_language, expected):
    app = create_app(tmp_path / "app")
    with TestClient(app) as client:
        response = client.post("/jobs", json=inputs | {"user_request": user_request, "runtime": "demo"},
                               headers={"Accept-Language": ui_language})
    assert response.status_code == 202, response.text
    job = app.state.store.get(response.json()["job_id"])
    assert job["input"]["language"] == expected
    assert job["input"]["user_request"] == user_request


def test_explicit_api_document_language_is_preserved(tmp_path, inputs):
    app = create_app(tmp_path / "app")
    with TestClient(app) as client:
        response = client.post("/jobs", json=inputs | {"runtime": "demo", "language": "en"},
                               headers={"Accept-Language": "zh-CN"})
    job = app.state.store.get(response.json()["job_id"])
    assert job["input"]["language"] == "en"


def test_english_download_suffix_preserves_original_title():
    job = {"input": {"title": "原始项目标题", "language": "en"}, "status": "draft"}
    assert artifact_filename(job) == "原始项目标题-draft.docx"
    job["input"]["title"] = "..."
    assert artifact_filename(job) == "Document-draft.docx"


@pytest.mark.parametrize("command", ["Generate outline", "Confirm and generate", "Regenerate outline", "Continue"])
def test_translated_interface_commands_do_not_become_writing_requirements(command):
    assert not substantive_text(command)
    assert substantive_text("Write a report that explains how to continue after a failure.")
