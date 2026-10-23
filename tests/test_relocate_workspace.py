import json
import runpy
import sqlite3
from pathlib import Path

import pytest

module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/relocate_workspace.py"))
relocate = module["relocate"]


@pytest.fixture
def workspace(tmp_path):
    old = tmp_path / "old-app"
    new = tmp_path / "new-app"
    old_data, data = old / ".old-data", new / ".folio"
    data.mkdir(parents=True)
    task = "jobs/j1/research/tasks/t1/r1"
    research = {
        "task_root": str(old_data / task),
        "research_notes_path": str(old_data / task / "research.md"),
        "round_id": "immutable-round", "model_calls": 19,
    }
    state = {"task_id": "t1", "context_key": "immutable-context", "research": research}
    original = {
        "knowledge_path": str(old_data / "jobs/j1/inputs/knowledge"),
        "sample_paths": [str(old_data / "uploads/u1/source.pdf")],
        "reference_paths": [str(old / "external/reference.docx")],
        "user_request": "Keep this literal historical path: " + str(old_data),
    }
    conn = sqlite3.connect(data / "old-app.sqlite3")
    conn.executescript("""
      CREATE TABLE jobs(id TEXT, input TEXT, plan TEXT, snapshot TEXT, artifact TEXT);
      CREATE TABLE work_states(job_id TEXT, task_id TEXT, state TEXT);
      CREATE TABLE web_uploads(id TEXT, path TEXT);
      CREATE TABLE events(data TEXT);
    """)
    conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (
        "j1", json.dumps(original),
        json.dumps({"template": {"path": str(old_data / "jobs/j1/template.docx")}}),
        json.dumps({"work_states": {"t1": state}, "state_digest": "immutable-digest"}),
        str(old_data / "jobs/j1/draft.docx"),
    ))
    conn.execute("INSERT INTO work_states VALUES(?,?,?)", ("j1", "t1", json.dumps(state)))
    conn.execute("INSERT INTO web_uploads VALUES(?,?)", ("u1", str(old_data / "uploads/u1/source.pdf")))
    conn.execute("INSERT INTO events VALUES(?)", (json.dumps({"path": str(old_data / task)}),))
    conn.commit()
    conn.close()
    ledger = data / "jobs/j1/research/ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE research_sessions(task_id TEXT, round_id TEXT, data TEXT)")
        conn.execute("INSERT INTO research_sessions VALUES(?,?,?)", (
            "t1", "r1", json.dumps({"workspace_root": str(old_data / task), "model_calls": 19}),
        ))
    ledger_connection = conn
    ledger_connection.close()
    files = {
        "jobs/j1/work-records.json": json.dumps({"t1": state}),
        "directory-drafts/d1/inspection.json": json.dumps({"artifact": str(old_data / "previews/p1/source.docx")}),
        "logs/history.json": json.dumps({"path": str(old_data / task)}),
        "jobs/j1/contexts/requests/request.json": json.dumps(original),
        "uploads/u1/source.pdf": "These source bytes include " + str(old_data),
        "source-cache/object.json": json.dumps({"source_text": str(old_data), "hash": "immutable"}),
    }
    for relative, value in files.items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return {
        "data_dir": data, "from_root": old, "to_root": new,
        "from_data": old_data, "to_data": data, "database_name": "old-app.sqlite3",
    }


def test_dry_run_does_not_change_database_json_or_create_backup(workspace, tmp_path):
    data = workspace["data_dir"]
    before = {str(p.relative_to(data)): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    report = relocate(**workspace, backup_dir=tmp_path / "backup")
    assert report["mode"] == "dry_run" and report["changed"]
    assert not (tmp_path / "backup").exists()
    assert before == {str(p.relative_to(data)): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def test_migration_keeps_work_snapshot_memory_and_audits_consistent(workspace, tmp_path):
    data = workspace["data_dir"]
    preserved = ["logs/history.json", "jobs/j1/contexts/requests/request.json", "uploads/u1/source.pdf", "source-cache/object.json"]
    before = {name: (data / name).read_bytes() for name in preserved}
    report = relocate(**workspace, backup_dir=tmp_path / "backup", apply=True)
    assert report["changed"] and not report["historical_audits_and_sources_changed"]
    assert not (data / "old-app.sqlite3").exists()
    with sqlite3.connect(data / "folio.sqlite3") as conn:
        raw_input, raw_snapshot, artifact = conn.execute("SELECT input,snapshot,artifact FROM jobs").fetchone()
        job_input, snapshot = json.loads(raw_input), json.loads(raw_snapshot)
        state = json.loads(conn.execute("SELECT state FROM work_states").fetchone()[0])
        assert snapshot["work_states"]["t1"] == state
        assert snapshot["state_digest"] == "immutable-digest"
        assert state["context_key"] == "immutable-context"
        assert state["research"]["model_calls"] == 19
        assert state["research"]["task_root"].startswith(str(data))
        assert artifact == str(data / "jobs/j1/draft.docx")
        assert job_input["knowledge_path"] == str(data / "jobs/j1/inputs/knowledge")
        assert job_input["sample_paths"] == [str(data / "uploads/u1/source.pdf")]
        assert job_input["reference_paths"] == [str(workspace["to_root"] / "external/reference.docx")]
        assert str(workspace["from_data"]) in job_input["user_request"]
        assert str(workspace["from_data"]) in conn.execute("SELECT data FROM events").fetchone()[0]
    with sqlite3.connect(data / "jobs/j1/research/ledger.sqlite") as conn:
        session = json.loads(conn.execute("SELECT data FROM research_sessions").fetchone()[0])
        assert session == {"workspace_root": str(data / "jobs/j1/research/tasks/t1/r1"), "model_calls": 19}
    assert before == {name: (data / name).read_bytes() for name in preserved}
    assert (tmp_path / "backup/old-app.sqlite3").is_file()
    assert (tmp_path / "backup/jobs/j1/research/ledger.sqlite").is_file()
    assert (tmp_path / "backup/jobs/j1/work-records.json").is_file()
    second = relocate(**workspace, backup_dir=tmp_path / "second-backup", apply=True)
    assert not second["changed"]
    assert not (tmp_path / "second-backup").exists()


def test_signed_records_require_special_migration(workspace):
    path = workspace["data_dir"] / "jobs/j1/research/task/.research/artifact_records/signed.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"signature":"preserve"}')
    with pytest.raises(ValueError, match="signature-aware"):
        relocate(**workspace)


def test_apply_requires_external_new_backup_directory(workspace, tmp_path):
    with pytest.raises(ValueError, match="requires --backup-dir"):
        relocate(**workspace, apply=True)
    with pytest.raises(ValueError, match="outside"):
        relocate(**workspace, apply=True, backup_dir=workspace["data_dir"] / "backup")
    existing = tmp_path / "already-used"
    existing.mkdir()
    with pytest.raises(ValueError, match="new directory"):
        relocate(**workspace, apply=True, backup_dir=existing)


def test_sqlite_backup_includes_committed_wal_data(tmp_path):
    source = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE values_saved(value TEXT)")
        connection.execute("INSERT INTO values_saved VALUES('latest committed job')")
        connection.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        target = tmp_path / "backup.sqlite3"
        module["_backup_database"](source, target)
        with sqlite3.connect(target) as backup:
            assert backup.execute("SELECT value FROM values_saved").fetchone()[0] == "latest committed job"
    finally:
        connection.close()


def test_json_write_failure_restores_database_and_original_state(workspace, tmp_path, monkeypatch):
    data = workspace["data_dir"]
    source_json = data / "jobs/j1/work-records.json"
    before = source_json.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(module["os"], "replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        relocate(**workspace, backup_dir=tmp_path / "backup", apply=True)
    assert source_json.read_bytes() == before
    assert (data / "old-app.sqlite3").is_file()
    assert not (data / "folio.sqlite3").exists()
    with sqlite3.connect(data / "old-app.sqlite3") as connection:
        state = json.loads(connection.execute("SELECT state FROM work_states").fetchone()[0])
        assert state["research"]["task_root"].startswith(str(workspace["from_data"]))
