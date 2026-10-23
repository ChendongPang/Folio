#!/usr/bin/env python3
"""Relocate active workspace paths after an offline project/data-directory move.

Dry-run is the default. Stop the workspace service before --apply. Uploaded files,
source indexes, historical model requests/events and audit logs are never rewritten.
Signed derived artifacts require a separate signature-aware migration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def _absolute(value):
    return Path(value).expanduser().resolve()


@contextmanager
def _database(path, *, readonly=False):
    connection = (
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        if readonly else sqlite3.connect(path, timeout=5)
    )
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _read_database(path):
    return _database(path, readonly=True)


def _map_path(value, prefixes):
    if not isinstance(value, str):
        return value
    for before, after in prefixes:
        if value == before or value.startswith(before + "/"):
            return after + value[len(before):]
    return value


def _research_paths(value, prefixes):
    research = value.get("research") if isinstance(value, dict) else None
    if isinstance(research, dict):
        for field in ("task_root", "research_notes_path"):
            if field in research:
                research[field] = _map_path(research[field], prefixes)


def _transform(value, kind, prefixes):
    if kind == "path":
        return _map_path(value, prefixes)
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError(f"Expected an object in active {kind} state")
    before = json.dumps(result, ensure_ascii=False)
    if kind == "input":
        if "knowledge_path" in result:
            result["knowledge_path"] = _map_path(result["knowledge_path"], prefixes)
        for field in ("reference_paths", "sample_paths"):
            if isinstance(result.get(field), list):
                result[field] = [_map_path(item, prefixes) for item in result[field]]
    elif kind == "plan":
        template = result.get("template")
        if isinstance(template, dict) and "path" in template:
            template["path"] = _map_path(template["path"], prefixes)
    elif kind == "snapshot":
        for state in (result.get("work_states") or {}).values():
            _research_paths(state, prefixes)
    elif kind == "work":
        _research_paths(result, prefixes)
    elif kind == "session":
        if "workspace_root" in result:
            result["workspace_root"] = _map_path(result["workspace_root"], prefixes)
    elif kind == "records":
        for state in result.values():
            _research_paths(state, prefixes)
    elif kind == "inspection":
        for field in ("artifact", "pdf"):
            if field in result:
                result[field] = _map_path(result[field], prefixes)
    after = json.dumps(result, ensure_ascii=False)
    return after if after != before else value


def _changes(path, specs, prefixes):
    changes = []
    with _read_database(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, kind in specs:
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue
            for row_id, value in connection.execute(f'SELECT rowid,"{column}" FROM "{table}"'):
                if value is None:
                    continue
                replacement = _transform(value, kind, prefixes)
                if replacement != value:
                    changes.append((table, column, row_id, value, replacement))
    return changes


def _backup_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _read_database(source) as original, _database(target) as backup:
        original.backup(backup)
    target.chmod(0o600)


def relocate(*, data_dir, from_root, to_root, from_data, to_data,
             database_name="folio.sqlite3", backup_dir=None, apply=False):
    data = _absolute(data_dir)
    if not data.is_dir():
        raise ValueError("--data-dir must be the existing moved data directory")
    if Path(database_name).name != database_name or database_name in {"", ".", ".."}:
        raise ValueError("--database-name must be a filename, not a path")
    # Do not use a blanket text replacement: model histories are immutable audits
    # and source bytes define source/evidence/cache identities.
    prefixes = sorted(
        [(str(_absolute(from_data)), str(_absolute(to_data))),
         (str(_absolute(from_root)), str(_absolute(to_root)))],
        key=lambda item: len(item[0]), reverse=True,
    )
    signed = next((path for path in data.rglob("artifact_records/*.json") if path.is_file()), None)
    if signed is not None:
        raise ValueError("Signed artifact_records exist; use a dedicated signature-aware migration")
    destination = data / "folio.sqlite3"
    source = data / database_name
    if not source.is_file() and destination.is_file():
        source = destination  # An already completed migration can be rerun safely.
    if not source.is_file():
        raise ValueError("Workspace database does not exist")
    if source != destination and destination.exists():
        raise ValueError("Both source and destination databases exist; refusing to overwrite either")
    specs = [
        ("jobs", "input", "input"), ("jobs", "plan", "plan"),
        ("jobs", "snapshot", "snapshot"), ("jobs", "artifact", "path"),
        ("work_states", "state", "work"), ("web_uploads", "path", "path"),
    ]
    databases = {source: _changes(source, specs, prefixes)}
    for ledger in sorted(data.glob("jobs/*/research/ledger.sqlite")):
        databases[ledger] = _changes(ledger, [("research_sessions", "data", "session")], prefixes)
    files = {}
    for pattern, kind in (("jobs/*/work-records.json", "records"),
                          ("directory-drafts/*/inspection.json", "inspection")):
        for path in sorted(data.glob(pattern)):
            value = path.read_text(encoding="utf-8")
            updated = _transform(value, kind, prefixes)
            if updated != value:
                files[path] = updated + "\n"
    for path in [*databases, *files]:
        if path.is_symlink() or not path.resolve().is_relative_to(data):
            raise ValueError("Active state paths must not be symlinks or escape the data directory")
    report = {
        "mode": "apply" if apply else "dry_run", "database": str(destination),
        "database_rename": source != destination,
        "database_changes": {str(path.relative_to(data)): len(changes) for path, changes in databases.items()},
        "json_files": [str(path.relative_to(data)) for path in files],
        "changed": source != destination or any(databases.values()) or bool(files),
        "historical_audits_and_sources_changed": False,
    }
    if not apply or not report["changed"]:
        return report
    if backup_dir is None:
        raise ValueError("--apply requires --backup-dir outside the data directory")
    backup = _absolute(backup_dir)
    if any(backup.is_relative_to(root) for root in (data, _absolute(from_data), _absolute(to_data))):
        raise ValueError("--backup-dir must be outside the data directory")
    if backup.exists():
        raise ValueError("--backup-dir must be a new directory to preserve previous backups")
    backup.mkdir(parents=True, mode=0o700)
    # SQLite backup includes committed WAL contents; copying just the .sqlite file
    # could silently lose the latest jobs/messages.
    for path in databases:
        _backup_database(path, backup / path.relative_to(data))
    for path in files:
        target = backup / path.relative_to(data)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(path, target)
        target.chmod(0o600)
    (backup / "migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (backup / "migration.json").chmod(0o600)
    renamed = False
    try:
        for path, changes in databases.items():
            if not changes:
                continue
            with _database(path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for table, column, row_id, before, after in changes:
                    updated = connection.execute(
                        f'UPDATE "{table}" SET "{column}"=? WHERE rowid=? AND "{column}"=?',
                        (after, row_id, before),
                    )
                    if updated.rowcount != 1:
                        raise ValueError("Workspace changed during migration; stop the service first")
                connection.commit()
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and checkpoint[0]:
                    raise ValueError("Database is busy; stop the service before migration")
        for path, content in files.items():
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                temporary.chmod(path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if source != destination:
            with _database(source) as connection:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and checkpoint[0]:
                    raise ValueError("Database is busy; stop the service before migration")
            source.rename(destination)
            renamed = True
            for suffix in ("-wal", "-shm"):
                Path(str(source) + suffix).unlink(missing_ok=True)
    except BaseException:
        # Restore active files from the consistent backups if any later write fails.
        if renamed:
            destination.rename(source)
        for path in databases:
            with _read_database(backup / path.relative_to(data)) as original, _database(path) as target:
                original.backup(target)
        for path in files:
            shutil.copyfile(backup / path.relative_to(data), path)
        raise
    report["backup_dir"] = str(backup)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("data-dir", "from-root", "to-root", "from-data", "to-data"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--database-name", default="folio.sqlite3")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write changes; otherwise only describe them")
    args = parser.parse_args(argv)
    try:
        report = relocate(**vars(args))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Migration refused: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
