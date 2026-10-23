import json

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.job_diagnostics import job_failure, retry_details
from folio.api.workspace import job_events
from folio.models import (
    DocumentPlan,
    Extraction,
    JobInput,
    Section,
    SectionContent,
    SectionState,
)


@pytest.fixture
def diagnostic_job(store, inputs):
    plan = DocumentPlan(
        sections=[
            Section(
                section_id="group",
                node_id="g",
                title="业务功能",
                level=1,
                display_number="5",
                instruction="",
                content_mode="container",
            ),
            Section(
                section_id="s1",
                node_id="o1",
                title="估值与定价环境",
                level=2,
                display_number="5.3",
                instruction="说明估值",
                parent_id="group",
            ),
            Section(
                section_id="s2",
                node_id="o2",
                title="前中台业务流程",
                level=2,
                display_number="5.4",
                instruction="说明流程",
                parent_id="group",
            ),
        ]
    )
    job_id = store.create(JobInput(**inputs, runtime="demo"), plan=plan)
    store.update(job_id, status="running", stage="generation")
    for sid in ("group", "s1"):
        store.save_section(
            job_id,
            SectionState(
                section_id=sid,
                content=SectionContent(),
                extraction=Extraction(summary="已保存"),
                evidence=[],
            ),
        )
    return job_id


def attempted_failure(store, job_id, *, task="extract", section="s1", error=None):
    error = (
        error
        or "ModelCallFailed [stalled_output]: Structured response stalled in whitespace before schema completion"
    )
    key = task + ":" + section
    attempt = store.begin_task(job_id, key, 2000)
    ref = ("1" if section == "s1" else "2") * 24
    folder = store.root / "jobs" / job_id / "contexts" / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (ref + ".json")).write_text(
        json.dumps(
            {
                "context": {
                    "section": {"section_id": section},
                    "user_request": "secret-request-body-never-render",
                }
            }
        )
    )
    store.audit_event(
        job_id,
        "request_prepared",
        {"task": task, "task_key": key, "attempt": attempt, "context_ref": ref},
    )
    store.finish_task(
        job_id,
        key,
        error=error,
        usage={
            "diagnostics": {
                "received_chars": 2120,
                "trailing_whitespace_chars": 512,
                "private_raw": "sk-secret-provider-token",
            }
        },
    )
    return key


async def test_explicit_root_failure_wins_over_later_unrelated_failures_and_cancelled_calls(
    store, diagnostic_job
):
    job_id = diagnostic_job
    key = attempted_failure(store, job_id)
    attempted_failure(store, job_id)
    store.audit_event(
        job_id,
        "job_failure",
        {"task": "extract", "task_key": key, "attempt": 2, "code": "stalled_output"},
    )
    attempted_failure(
        store,
        job_id,
        task="integrate_supplement",
        section="s2",
        error="ValueError: Unknown or overlapping supplement edit target",
    )
    store.audit_event(
        job_id,
        "provider_completed",
        {"task": "generate", "outcome": "cancelled", "error_code": "CancelledError"},
    )
    store.update(
        job_id,
        status="failed",
        error="ModelCallFailed: Structured response stalled in whitespace before schema completion",
    )
    frames = [frame async for frame in job_events(store, job_id)]
    failed = next(frame for frame in frames if "event: job.failed" in frame)
    payload = json.loads(failed.split("data: ")[1])
    result = payload["failure"]
    assert result["action"] == "extract" and result["section_labels"] == ["5.3 估值与定价环境"]
    assert result["attempts"] == 2 and result["code"] == "stalled_output"
    assert result["saved_sections"] == 1 and result["total_sections"] == 2
    assert result["details"] == {"received_chars": 2120, "trailing_whitespace_chars": 512}
    assert "secret-" not in "".join(frames) and "CancelledError" not in failed
    assert "正在进行第 2 次提取事实要点" in "".join(frames)


def test_legacy_failure_requires_exact_matching_terminal_exception(store, diagnostic_job):
    job_id = diagnostic_job
    attempted_failure(store, job_id)
    attempted_failure(store, job_id)
    store.update(
        job_id,
        status="failed",
        error="ModelCallFailed: Structured response stalled in whitespace before schema completion",
    )
    # Both attempts occurred in the same second. Multiple indistinguishable
    # legacy candidates must not be guessed as the fatal request.
    assert job_failure(store, store.get(job_id))["action"] == ""
    with store.connect() as conn:
        conn.execute(
            "UPDATE events SET created_at='2020-01-01 00:00:00' WHERE job_id=? AND kind='task_failed' AND id=(SELECT min(id) FROM events WHERE job_id=? AND kind='task_failed')",
            (job_id, job_id),
        )
    failure = job_failure(store, store.get(job_id))
    assert failure["action"] == "extract" and failure["attempts"] == 2
    store.update(job_id, status="failed", error="RuntimeError: unrelated secret-provider-body")
    fallback = job_failure(store, store.get(job_id))
    assert fallback["action"] == "" and fallback["section_labels"] == []
    assert "secret-provider-body" not in json.dumps(fallback)


def test_retry_rows_report_actual_retry_and_preserve_locally_handled_failure_semantics(
    store, diagnostic_job
):
    job_id = diagnostic_job
    key = attempted_failure(store, job_id)
    event = next(e for e in store.events(job_id) if e["kind"] == "task_failed")
    text, diagnostic = retry_details(store, store.get(job_id), event)
    assert diagnostic["retry_status"] == "unknown" and diagnostic["is_terminal"] is False
    assert "随后已发起重试" not in text
    store.begin_task(job_id, key, 2000)
    text, diagnostic = retry_details(store, store.get(job_id), event)
    assert diagnostic["retry_status"] == "retrying" and "随后已发起重试" in text
    store.finish_task(
        job_id, key, error="ValueError: Unknown or overlapping supplement edit target"
    )
    event = [e for e in store.events(job_id) if e["kind"] == "task_failed"][-1]
    text, diagnostic = retry_details(store, store.get(job_id), event)
    assert diagnostic["retry_status"] == "stopped" and diagnostic["is_terminal"] is False
    assert "当前阶段处理" in text and "任务失败" not in text


def test_failed_conversation_restores_diagnostics_without_open_stream(store, diagnostic_job):
    job_id = diagnostic_job
    attempted_failure(store, job_id)
    store.update(
        job_id,
        status="failed",
        error="ModelCallFailed: Structured response stalled in whitespace before schema completion",
    )
    app = create_app(store.root)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        with store.connect() as conn:
            row = conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()
            value = json.loads(row[0]) | {"active_job": job_id}
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(value), cid))
        state = client.get(f"/conversations/{cid}").json()
        assert state["active_job"] == job_id and state["active_job_status"] == "failed"
        assert state["active_job_failure"]["section_labels"] == ["5.3 估值与定价环境"]
        assert state["active_job_failure"]["code"] == "stalled_output"
        assert "secret-" not in json.dumps(state)


def test_retry_diagnostics_use_only_grants_before_the_failed_attempt(store, diagnostic_job):
    job_id = diagnostic_job
    attempted_failure(store, job_id)
    attempted_failure(store, job_id)
    old_failure = [e for e in store.events(job_id) if e["kind"] == "task_failed"][-1]
    store.update(job_id, status="failed", error="ModelCallFailed: Model HTTP 502")
    store.resume_job(job_id)
    # Authorizing a later run must not rewrite what happened in the earlier run.
    assert retry_details(store, store.get(job_id), old_failure)[1]["retry_status"] == "stopped"
    attempted_failure(store, job_id)
    new_failure = [e for e in store.events(job_id) if e["kind"] == "task_failed"][-1]
    assert retry_details(store, store.get(job_id), new_failure)[1]["retry_status"] == "unknown"
    attempted_failure(store, job_id)
    final_failure = [e for e in store.events(job_id) if e["kind"] == "task_failed"][-1]
    assert retry_details(store, store.get(job_id), final_failure)[1]["retry_status"] == "stopped"


def test_resumed_failure_never_reuses_previous_runs_explicit_root(store, diagnostic_job):
    job_id = diagnostic_job
    key = attempted_failure(store, job_id)
    store.audit_event(
        job_id,
        "job_failure",
        {"task": "extract", "task_key": key, "attempt": 1, "code": "stalled_output"},
    )
    store.update(
        job_id,
        status="failed",
        error="ModelCallFailed: Structured response stalled in whitespace before schema completion",
    )
    assert job_failure(store, store.get(job_id))["action"] == "extract"
    store.update(job_id, status="running")
    store.update(job_id, status="failed", error="RuntimeError: new unrelated failure")
    current = job_failure(store, store.get(job_id))
    assert current["action"] == "" and current["section_labels"] == []
    assert current["code"] == "model_error"


def test_generic_explicit_error_does_not_hide_timeout_cause(store, diagnostic_job):
    job_id = diagnostic_job
    key = attempted_failure(store, job_id, error="TimeoutError: ")
    store.audit_event(
        job_id,
        "job_failure",
        {"task": "extract", "task_key": key, "attempt": 1, "code": "model_error"},
    )
    store.update(job_id, status="failed", error="TimeoutError: ")
    failure = job_failure(store, store.get(job_id))
    assert failure["action"] == "extract" and failure["code"] == "timeout"
    assert "等待时限" in failure["reason"]


@pytest.mark.parametrize("known", [True, False])
def test_job_failure_protocol_details_keep_only_safe_enum_values(store, diagnostic_job, known):
    job_id = diagnostic_job
    store.audit_event(
        job_id,
        "job_failure",
        {
            "code": "protocol",
            "diagnostics": {
                "protocol_issue": "tool_arguments_json" if known else "private-provider-error",
                "finish_reason": "missing" if known else "private-finish-reason",
                "raw_tool_calls": "private-tool-arguments",
            },
        },
    )
    store.update(job_id, status="failed", error="ModelCallFailed [protocol]: private-message")
    failure = job_failure(store, store.get(job_id))
    assert failure["code"] == "protocol"
    assert failure["details"] == (
        {"protocol_issue": "tool_arguments_json", "finish_reason": "missing"} if known else {}
    )
    assert "private" not in json.dumps(failure)


@pytest.mark.parametrize("attributed", [False, True])
def test_validation_failure_requires_model_attribution(store, diagnostic_job, attributed):
    job_id = diagnostic_job
    error = "ValidationError: description exceeds limit; secret-local-data"
    if attributed:
        key = attempted_failure(store, job_id, error=error)
        store.audit_event(job_id, "job_failure", {
            "task": "extract", "task_key": key, "attempt": 1, "code": "validation_error",
        })
    store.update(job_id, status="failed", error=error)
    failure = job_failure(store, store.get(job_id))
    assert failure["code"] == ("invalid_output" if attributed else "internal_validation")
    assert "secret-local-data" not in json.dumps(failure)
    if not attributed:
        assert "模型" not in failure["reason"]
