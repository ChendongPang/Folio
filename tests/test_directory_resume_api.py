"""Resume controls use the saved research without changing the accepted request."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_directory_review import assert_no_jobs, begin, state

from folio.source_documents import SourceDirectoryDrafts

pytest_plugins = ("test_directory_review",)


@pytest.mark.parametrize("reason", ["model_timeout", "time_limit"])
async def test_directory_preparation_exposes_interruption_reason_for_resume_control(tmp_path, monkeypatch, reason):
    from test_directory_research_handoff import PlanningRuntime, partial_research, prepare

    report = partial_research()
    report["termination_reason"] = reason
    report["failure"] = {"code": reason, "message": "Research request timed out"}
    result = await prepare(monkeypatch, tmp_path, report, PlanningRuntime())
    assert result["confirmable"] is False
    assert result["research"]["status"] == "partial"
    assert result["research"]["termination_reason"] == reason


def interrupted_review(app, client, cid, reason="model_timeout"):
    """Keep a complete frozen draft, but mark its saved research as interrupted."""
    selection = state(client, cid)
    folder = app.state.store.root / "directory-drafts" / selection["directory"]["revision"]
    for path in (folder / "metadata.json", folder / "research/report.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        research = record["research"] if path.name == "metadata.json" else record
        research.update(status="partial", termination_reason=reason)
        if path.name == "metadata.json":
            record["confirmable"] = False
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    with app.state.store.connect() as conn:
        record = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
        directory = record["directory_review"]["directory"]
        directory["confirmable"] = False
        directory["research"].update(status="partial", termination_reason=reason)
        conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(record), cid))
    return folder, client.get(f"/conversations/{cid}").json()


def resume(client, cid, selection, **extra):
    return client.post(f"/conversations/{cid}/messages", json={
        "text": "从中断处继续资料研究", "action": "resume_directory_research",
        "selection_revision": selection["revision"], **extra,
    })


@pytest.mark.parametrize("reason", ["model_timeout", "time_limit"])
def test_resume_passes_frozen_draft_and_preserves_requirements(setup, inputs, monkeypatch, reason):
    app, _ = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        folder, before = interrupted_review(app, client, cid, reason)
        selection = before["directory_review"]
        assert selection["directory"]["research"]["termination_reason"] == reason
        captured = []

        async def continued(self, config, conversation_id, attachments, request, **kwargs):
            captured.append((conversation_id, request, kwargs))
            directory = deepcopy(selection["directory"])
            directory["research"].update(status="ready", termination_reason=None)
            directory["confirmable"] = True
            directory["confirmation_issues"] = []
            directory["title"] = selection["title"]
            return directory

        monkeypatch.setattr(SourceDirectoryDrafts, "create_from_sources", continued)
        response = resume(client, cid, selection)
        assert response.status_code == 200 and "directory.review" in response.text
        assert "message.failed" not in response.text
        assert len(captured) == 1
        resumed_cid, request, kwargs = captured[0]
        assert resumed_cid == cid and kwargs["resume_from"]["folder"] == folder
        assert kwargs["previous"] is None
        assert kwargs["requirements"].model_dump(mode="json") == before["requirements"]
        assert "从中断处继续" not in request
        after = client.get(f"/conversations/{cid}").json()
        assert after["requirements"] == before["requirements"]
        assert after["directory_review"]["revision"] != selection["revision"]
        assert after["directory_review"]["directory"]["confirmable"] is True
        assert_no_jobs(app)


@pytest.mark.parametrize("invalid", ["missing_report", "broken_input", "non_object_report"])
def test_unusable_resume_record_is_rejected_before_stream_and_keeps_draft(setup, inputs, monkeypatch, invalid):
    app, _ = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        folder, before = interrupted_review(app, client, cid)
        if invalid == "missing_report":
            (folder / "research/report.json").unlink()
        elif invalid == "broken_input":
            (folder / "research/input.json").write_text("{broken", encoding="utf-8")
        else:
            (folder / "research/report.json").write_text("[]", encoding="utf-8")

        async def unexpected(*args, **kwargs):
            pytest.fail("An unavailable replay must not start another directory run")

        monkeypatch.setattr(SourceDirectoryDrafts, "create_from_sources", unexpected)
        response = resume(client, cid, before["directory_review"])
        assert response.status_code == 409
        assert "研究记录不可用" in response.json()["detail"]
        after = client.get(f"/conversations/{cid}").json()
        assert after["directory_review"] == before["directory_review"]
        assert after["messages"] == before["messages"]
        assert after["requirements"] == before["requirements"]
        assert_no_jobs(app)


@pytest.mark.parametrize("invalid", ["not_timeout", "stale_revision", "changed_requirements", "report_mismatch", "advice"])
def test_resume_does_not_bypass_snapshot_guards(setup, inputs, monkeypatch, invalid):
    app, _ = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        folder, before = interrupted_review(app, client, cid, "tool_error" if invalid == "not_timeout" else "model_timeout")
        selection = deepcopy(before["directory_review"])
        if invalid == "stale_revision":
            selection["revision"] = "f" * 32
        elif invalid == "changed_requirements":
            with app.state.store.connect() as conn:
                record = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
                record["messages"].append({"role": "user", "content": "增加成本比较章节", "action": "generate",
                                           "requirement": {"status": "active", "kind": "request", "source": "explicit_action"}})
                conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(record), cid))
            before = client.get(f"/conversations/{cid}").json()
        elif invalid == "report_mismatch":
            path = folder / "research/report.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["termination_reason"] = "tool_error"
            path.write_text(json.dumps(record), encoding="utf-8")

        async def unexpected(*args, **kwargs):
            pytest.fail("A conflicting request must not start another directory run")

        monkeypatch.setattr(SourceDirectoryDrafts, "create_from_sources", unexpected)
        response = resume(client, cid, selection, **({"advice": "另外补上成本分析"} if invalid == "advice" else {}))
        assert response.status_code == (422 if invalid == "advice" else 409)
        after = client.get(f"/conversations/{cid}").json()
        assert after["directory_review"] == before["directory_review"]
        assert after["messages"] == before["messages"]
        assert_no_jobs(app)
