"""Remove exclusively owned session assets with compensating filesystem rollback."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from fastapi import HTTPException
from filelock import FileLock

_ID = re.compile(r"^[a-f0-9]{32}$")
_ACTIVE = {"queued", "running", "pausing"}
_JOB_TABLES = (
    "sections", "work_plans", "work_states", "tasks", "retrievals", "decisions",
    "events", "confirmation_rounds", "call_budgets",
)


def _safe_tree(root, path):
    """Check the complete tree before touching any original file."""
    if not path.is_relative_to(root) or path == root:
        raise ValueError("文件路径超出工作区")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("待删除文件不能使用符号链接")
    if not path.exists():
        return []
    children = sorted(path.rglob("*")) if path.is_dir() else []
    if any(item.is_symlink() for item in children):
        raise ValueError("待删除目录包含符号链接")
    return [path, *children]


def _owned_folders(root, conversation_id):
    owned, foreign = [], []
    for category in ("directory-drafts", "conversation-styles"):
        base = root / category
        if base.is_symlink():
            raise ValueError("资料目录不能使用符号链接")
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not _ID.fullmatch(folder.name):
                continue
            metadata = folder / "metadata.json"
            if folder.is_symlink() or metadata.is_symlink():
                raise ValueError("资料目录不能使用符号链接")
            if not metadata.is_file():
                continue  # No proven owner; do not guess from an incomplete directory.
            value = json.loads(metadata.read_text(encoding="utf-8"))
            encoded = json.dumps(value, ensure_ascii=False)
            if value.get("conversation_id") == conversation_id:
                owned.append((folder, encoded))
            else:
                foreign.append(encoded)
    return owned, foreign


def _job_bindings(state):
    # User message text is never evidence of ownership, even if it contains a
    # valid job ID or download URL belonging to another task.
    return {value for value in (
        state.get("active_job"), (state.get("generation_context") or {}).get("job_id"),
    ) if isinstance(value, str) and _ID.fullmatch(value)}


def _upload_bindings(root, state, owned_folders, jobs):
    values = {item.get("id") for item in state.get("files", [])}
    values.update(item.get("id") for item in (state.get("generation_context") or {}).get("files", []))
    for _, encoded in owned_folders:
        metadata = json.loads(encoded)
        values.update(metadata.get("source_ids", []))
        values.update(item.get("id") for item in metadata.get("sources", []))
    # Older jobs can outlive the current directory binding. Uploaded business
    # sources have an explicit <upload-id>__<filename> convention in frozen inputs.
    for job_id in jobs:
        inputs = root / "jobs" / job_id / "inputs"
        for path in _safe_tree(root, inputs):
            candidate, separator, _ = path.name.partition("__")
            if separator and _ID.fullmatch(candidate):
                values.add(candidate)
    return {value for value in values if isinstance(value, str) and _ID.fullmatch(value)}


def _restore(archive, root):
    for member in archive.infolist():
        destination = root / member.filename
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod((member.external_attr >> 16) & 0o777)


def delete_conversation_files(store, conversation_id, state, *, tasks, locks):
    """Called with the session and current job locks already held.

    Ownership is recalculated while SQLite blocks new references. The caller's
    save path validates newly bound upload IDs in that same database transaction.
    An anonymous archive remains available until both file cleanup and commit
    succeed, so a partial rmtree or failed database commit restores originals.
    Content-addressed global previews and caches have no exclusive owner and stay.
    """
    root = store.root
    backup = None
    archive = None
    changed = False
    committed = False
    try:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            other_conversations = [r[0] for r in conn.execute(
                "SELECT data FROM web_conversations WHERE id<>?", (conversation_id,)
            )]
            other_text = "\n".join(other_conversations)
            jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs")]
            bound_jobs = _job_bindings(state)
            owned_jobs = {
                row["id"] for row in jobs
                if row["document_id"] == "web_" + conversation_id or row["id"] in bound_jobs
            }
            if any(not _ID.fullmatch(job_id) for job_id in owned_jobs):
                raise ValueError("任务编号无效，无法确认文件归属")
            retained_jobs = {job_id for job_id in owned_jobs if job_id in other_text}
            # A surviving revision can depend on an older published baseline.
            while True:
                surviving_text = "\n".join(json.dumps(row, ensure_ascii=False) for row in jobs if row["id"] not in owned_jobs - retained_jobs)
                protected = retained_jobs | {job_id for job_id in owned_jobs if job_id in surviving_text}
                if protected == retained_jobs:
                    break
                retained_jobs = protected
            removed_jobs = owned_jobs - retained_jobs
            for row in jobs:
                if row["id"] not in owned_jobs:
                    continue
                job_id = row["id"]
                if job_id != state.get("active_job"):
                    locks.enter_context(FileLock(root / f"{job_id}.lock").acquire(timeout=0))
                task = tasks.get(job_id)
                if row["status"] in _ACTIVE or (task and not task.done()):
                    raise HTTPException(409, "该会话还有正在运行的文档任务，请先停止处理再删除文件。")

            owned_folders, foreign_metadata = _owned_folders(root, conversation_id)
            surviving_jobs = [json.dumps(row, ensure_ascii=False) for row in jobs if row["id"] not in removed_jobs]
            protected_text = "\n".join([other_text, *surviving_jobs, *foreign_metadata])
            retained_folders = set()
            while True:
                referenced = {folder for folder, _ in owned_folders if folder.name in protected_text}
                if referenced <= retained_folders:
                    break
                retained_folders.update(referenced)
                protected_text += "\n" + "\n".join(metadata for folder, metadata in owned_folders if folder in referenced)
            owned_uploads = _upload_bindings(root, state, owned_folders, owned_jobs)
            delete_paths = [root / "jobs" / job_id for job_id in removed_jobs]
            retained_paths = [root / "jobs" / job_id for job_id in retained_jobs]
            for folder, _ in owned_folders:
                (retained_paths if folder in retained_folders else delete_paths).append(folder)

            upload_ids = []
            for row in conn.execute("SELECT id,path FROM web_uploads"):
                if row["id"] not in owned_uploads:
                    continue
                folder = root / "uploads" / row["id"]
                if not _ID.fullmatch(row["id"]) or not Path(row["path"]).is_relative_to(folder):
                    raise ValueError("上传文件路径不属于当前工作区")
                if row["id"] in protected_text or str(folder) in protected_text:
                    retained_paths.append(folder)
                else:
                    upload_ids.append(row["id"])
                    delete_paths.append(folder)

            entries = [entry for path in delete_paths for entry in _safe_tree(root, path)]
            retained_entries = [entry for path in retained_paths for entry in _safe_tree(root, path)]
            files_deleted = sum(path.is_file() for path in entries)
            files_retained = sum(path.is_file() for path in retained_entries)
            backup = tempfile.TemporaryFile(dir=root)
            archive = ZipFile(backup, "w", compression=ZIP_STORED)
            for path in entries:
                archive.write(path, path.relative_to(root).as_posix())
            archive.close()
            archive = ZipFile(backup, "r")
            changed = True
            for path in delete_paths:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            for job_id in removed_jobs:
                for table in _JOB_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                conn.execute("UPDATE documents SET published_version=NULL WHERE published_version=?", (job_id,))
            conn.execute("DELETE FROM documents WHERE id=? AND NOT EXISTS(SELECT 1 FROM jobs WHERE document_id=documents.id)", ("web_" + conversation_id,))
            conn.executemany("DELETE FROM web_uploads WHERE id=?", [(value,) for value in upload_ids])
            conn.execute("DELETE FROM web_conversations WHERE id=?", (conversation_id,))
        committed = True
        return {
            "deleted": conversation_id,
            "files_deleted": files_deleted,
            "files_retained": files_retained,
            "jobs_deleted": len(removed_jobs),
            "shared_caches_retained": True,
        }
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(409, "文件未能完整清理，会话已保留。请检查文件权限和磁盘后重试。") from exc
    finally:
        # The database context commits before the return. Any exception rolls it
        # back; use the still-open anonymous archive to undo partial file cleanup.
        if changed and not committed and archive is not None:
            _restore(archive, root)
        if archive is not None:
            archive.close()
        if backup is not None:
            backup.close()
