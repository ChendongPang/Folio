"""Schema rejections remain actionable without exposing nested upstream errors."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.diagnostics import error_diagnostics
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.demo import DemoRuntime
from folio.runtime.diagnostics import safe_failure_diagnostics
from folio.runtime.errors import (
    ModelCallFailed,
    ModelContextLimit,
    model_http_error,
    public_model_error,
)


def nested_schema_rejection():
    return {
        "error": {
            "message": "Provider returned error private-api-key",
            "code": 400,
            "metadata": {
                "provider_error_code": "invalid_json_schema",
                "raw": json.dumps(
                    {
                        "error": {
                            "message": "Invalid schema for response_format 'folio_result': "
                            "'required' is required to be supplied and to be an array including "
                            "every key in properties. Missing 'private-field'. private-source",
                            "type": "invalid_request_error",
                            "param": "text.format.schema",
                            "code": "invalid_json_schema",
                        }
                    }
                ),
            },
        }
    }


def test_nested_schema_rejection_keeps_only_safe_classification():
    failure = model_http_error(nested_schema_rejection(), 400)
    assert failure.code == "schema_unsupported"
    assert safe_failure_diagnostics(failure) == {
        "status_code": 400,
        "backend_error": "invalid_json_schema",
        "schema_constraint": "required_all_properties",
    }
    public = public_model_error(failure)
    assert "对象字段未全部声明为必填" in public["text"]
    assert "检查连接" not in public["text"]
    details = error_diagnostics(
        failure, conversation_id="a" * 32, stage="directory_planning", code=failure.code
    )
    assert details["code"] == "schema_unsupported"
    assert details["backend_error"] == "invalid_json_schema"
    assert details["schema_constraint"] == "required_all_properties"
    assert "private" not in str(failure) + json.dumps(public) + json.dumps(details)


def test_protocol_api_diagnostics_preserve_safe_cause_and_request_id():
    failure = ModelCallFailed("private-provider-message", "protocol")
    metrics = {
        "protocol_issue": "tool_arguments_json",
        "finish_reason": "missing",
        "tool_call_chars": 158,
        "tool_calls_received": 1,
        "done_received": False,
        "stream_event_index": 4,
        "failed_tool_index": 0,
        "tool_argument_error_position": 17,
    }
    failure.diagnostics = metrics | {
        "phase": "streaming_tool_call",
        "raw_tool_calls": "private-source-body",
    }
    outer = ModelCallFailed("private-wrapped-message", "protocol")
    outer.__cause__ = failure
    outer.diagnostics = {"research_request_id": "research_004"}
    details = error_diagnostics(
        outer, conversation_id="a" * 32, stage="directory_planning", code="protocol"
    )
    assert metrics.items() <= details.items()
    assert details["transport_phase"] == "streaming_tool_call"
    assert details["research_request_id"] == "research_004"
    assert "private" not in json.dumps(details)


def test_protocol_api_diagnostics_reject_untrusted_or_malformed_values():
    failure = ModelCallFailed("private-message", "protocol")
    unsafe = {
        "protocol_issue": "private-provider-error",
        "finish_reason": "private-finish-reason",
        "tool_call_chars": -1,
        "tool_calls_received": True,
        "done_received": "private-bool",
        "stream_event_index": 1.5,
        "failed_tool_index": -1,
        "tool_argument_error_position": "private-position",
        "research_request_id": "research_004-private",
    }
    failure.diagnostics = unsafe | {"phase": "private-phase"}
    details = error_diagnostics(
        failure, conversation_id="a" * 32, stage="directory_planning", code="protocol"
    )
    assert not unsafe.keys() & details.keys()
    assert "transport_phase" not in details
    assert "private" not in json.dumps(details)


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"error": {"code": "invalid_json_schema"}}, 400, "schema_unsupported"),
        ({"error": {"metadata": {"raw": "private invalid JSON"}}}, 400, "invalid_request"),
        ({"error": {"metadata": {"raw": "x" * 65_000}}}, 400, "invalid_request"),
        ({"error": {"message": "required source private-field"}}, 400, "invalid_request"),
        (None, 401, "authentication"),
        (None, 402, "billing"),
        (None, 429, "rate_limit"),
    ],
)
def test_http_rejection_classification_is_bounded_and_conservative(body, status, code):
    failure = model_http_error(body, status)
    assert failure.code == code
    assert failure.diagnostics["status_code"] == status
    assert "private" not in str(failure) + json.dumps(failure.diagnostics)


def test_http_context_limit_retains_numeric_budget_recovery():
    failure = model_http_error(
        {
            "error": {
                "message": "maximum context length is 32,768 tokens; "
                "prompt contains at least 30,000 input tokens private-api-key",
                "code": "context_length_exceeded",
            }
        },
        400,
    )
    assert isinstance(failure, ModelContextLimit)
    assert failure.window == 32_768 and failure.input_tokens == 30_000
    assert failure.reduced_output(8192) == 2512
    assert "private" not in str(failure)


@pytest.mark.parametrize("existing_directory", [False, True])
def test_directory_schema_rejection_persists_safe_error_without_creating_job(
    tmp_path, monkeypatch, caplog, existing_directory
):
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    app = create_app(tmp_path)
    calls = []

    class Planner(DemoRuntime):
        def __init__(self, config):
            pass

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", Planner)
    monkeypatch.setattr("folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"})

    async def rejected(*args, **kwargs):
        calls.append(True)
        raise model_http_error(nested_schema_rejection(), 400)

    with (
        caplog.at_level(logging.ERROR, logger="folio.api.workspace"),
        TestClient(app) as client,
    ):
        cid = client.post("/conversations").json()["id"]
        assert client.post(
            f"/conversations/{cid}/sources", params={"filename": "source.md"},
            content="# 功能说明\n支持批量资料上传。".encode(),
        ).status_code == 201
        payload = {"text": "编写产品说明文档", "action": "generate"}
        if existing_directory:
            assert "directory.review" in client.post(f"/conversations/{cid}/messages", json=payload).text
            previous = client.get(f"/conversations/{cid}").json()["directory_review"]
            payload.update(action="revise_directory", advice="增加功能介绍", selection_revision=previous["revision"])
        monkeypatch.setattr("folio.directory_drafts.DirectoryDrafts.create", rejected)
        response = client.post(
            f"/conversations/{cid}/messages", json=payload,
        )
        saved = client.get(f"/conversations/{cid}").json()
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert len(calls) == 1
    assert "message.failed" in response.text and "job.created" not in response.text
    assert "结构化输出 Schema" in response.text
    details = saved["messages"][-1]["error_diagnostics"]
    assert details["code"] == "schema_unsupported"
    assert details["stage"] == "directory_planning" and details["status_code"] == 400
    assert details["backend_error"] == "invalid_json_schema"
    if existing_directory:
        review = saved["directory_review"]
        assert review["phase"] == "directory_review"
        assert review["revision"] == previous["revision"]
        assert review["operation_error"]["diagnostics"] == details
        assert review["failed_advice"] == "增加功能介绍"
    else:
        assert not saved.get("directory_review")
    assert "private" not in caplog.text + response.text + json.dumps(saved)
