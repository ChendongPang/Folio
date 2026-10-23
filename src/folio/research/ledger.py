"""Atomic tool admission and durable results, separate from model call accounting."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class ResearchLimitReached(ValueError):
    pass


class ResearchLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS research_tools (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, round_id TEXT NOT NULL,
                    step INTEGER NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, reserved_seconds REAL NOT NULL DEFAULT 0,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS research_sessions (
                    task_id TEXT NOT NULL, round_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(task_id, round_id)
                );
                CREATE TABLE IF NOT EXISTS research_rounds (
                    task_id TEXT NOT NULL, number INTEGER NOT NULL,
                    session_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
                    PRIMARY KEY(task_id, number)
                );
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def usage(self, task_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(CASE WHEN status='running' "
                "THEN reserved_seconds ELSE elapsed_seconds END),0) AS seconds "
                "FROM research_tools WHERE task_id=?", (task_id,),
            ).fetchone()
            return dict(row)

    def get(self, request_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM research_tools WHERE id=?", (request_id,)).fetchone()
            if not row:
                return None
            return dict(row) | {"result": json.loads(row["result"]) if row["result"] else None}

    def begin(self, request_id, task_id, round_id, step, tool, arguments, *, config, reserve=0):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM research_tools WHERE id=?", (request_id,),
            ).fetchone()
            if existing:
                if (
                    existing["task_id"] != task_id or existing["round_id"] != round_id
                    or existing["step"] != step or existing["tool"] != tool
                    or json.loads(existing["arguments"]) != arguments
                ):
                    raise ValueError("Research execution identity reused for another operation")
                return False
            usage = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(CASE WHEN status='running' "
                "THEN reserved_seconds ELSE elapsed_seconds END),0) AS seconds "
                "FROM research_tools WHERE task_id=?", (task_id,),
            ).fetchone()
            if usage["calls"] >= config.max_tool_calls:
                raise ResearchLimitReached('The research tool-call budget for this business task is exhausted')
            if reserve and usage["seconds"] + reserve > config.max_execution_seconds:
                raise ResearchLimitReached('The cumulative script execution allowance for this business task is insufficient')
            conn.execute(
                "INSERT INTO research_tools(id,task_id,round_id,step,tool,arguments,status,"
                "reserved_seconds) VALUES(?,?,?,?,?,?,'running',?)",
                (request_id, task_id, round_id, step, tool,
                 json.dumps(arguments, ensure_ascii=False, sort_keys=True), reserve),
            )
        return True

    def finish(self, request_id, result, *, elapsed=0, status="completed"):
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Invalid research tool completion state")
        with self.connect() as conn:
            conn.execute(
                "UPDATE research_tools SET status=?,result=?,elapsed_seconds=? "
                "WHERE id=? AND status='running'",
                (status, json.dumps(result, ensure_ascii=False), max(0, elapsed), request_id),
            )

    def load_session(self, task_id, round_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM research_sessions WHERE task_id=? AND round_id=?",
                (task_id, round_id),
            ).fetchone()
            return json.loads(row["data"]) if row else {
                "step": 0, "notes": "", "missing": [], "observations": [], "evidence": [],
                "finished": False, "model_calls": 0,
            }

    def save_session(self, task_id, round_id, data):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO research_sessions(task_id,round_id,data) VALUES(?,?,?) "
                "ON CONFLICT(task_id,round_id) DO UPDATE SET data=excluded.data",
                (task_id, round_id, json.dumps(data, ensure_ascii=False)),
            )

    def runs(self, task_id):
        with self.connect() as conn:
            return [dict(r) | {"arguments": json.loads(r["arguments"]),
                               "result": json.loads(r["result"]) if r["result"] else None}
                    for r in conn.execute(
                        "SELECT * FROM research_tools WHERE task_id=? ORDER BY created_at,rowid",
                        (task_id,),
                    )]

    def local_rounds(self, task_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM research_rounds WHERE task_id=? ORDER BY number", (task_id,),
            )]

    def begin_round(self, task_id, session_id):
        """Reserve before model/tool I/O; repairs and crashes share the three rounds."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                "SELECT * FROM research_rounds WHERE task_id=? ORDER BY number", (task_id,),
            ))
            if rows and rows[-1]["status"] == "running":
                if rows[-1]["session_id"] == session_id:
                    return rows[-1]["number"]
                conn.execute("UPDATE research_rounds SET status='interrupted' WHERE task_id=? AND number=?",
                             (task_id, rows[-1]["number"]))
            if len(rows) >= 3:
                return None
            number = len(rows) + 1
            conn.execute("INSERT INTO research_rounds(task_id,number,session_id) VALUES(?,?,?)",
                         (task_id, number, session_id))
            return number

    def finish_round(self, task_id, number):
        with self.connect() as conn:
            conn.execute("UPDATE research_rounds SET status='completed' WHERE task_id=? AND number=?",
                         (task_id, number))

    def history(self, task_id, *, request_id=None, query="", offset=0, limit=20, max_chars=600000,
                inherited_ids=()):
        """Page archived results without granting access to another business task."""
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= (max_chars if request_id else 100)
                or not isinstance(query, str) or len(query) > 3500):
            raise ValueError("Invalid research history range")
        # inherited_ids is server-owned lineage, never a model/tool argument.
        allowed = list(dict.fromkeys(inherited_ids))
        ownership = "task_id=?"
        ownership_args = [task_id]
        if allowed:
            ownership = "(task_id=? OR id IN (" + ",".join("?" for _ in allowed) + "))"
            ownership_args += allowed
        with self.connect() as conn:
            if request_id:
                row = conn.execute("SELECT * FROM research_tools WHERE " + ownership + " AND id=?",
                                   (*ownership_args, request_id)).fetchone()
                if row is None:
                    raise ValueError("Tool result does not belong to this research task")
                value = row["result"] or "null"
                end = min(len(value), offset + min(limit, max_chars))
                return {"request_id": request_id, "text": value[offset:end], "total_chars": len(value),
                        "offset": offset, "next_offset": end if end < len(value) else None}
            rows = conn.execute(
                "SELECT id,round_id,step,tool,status,arguments,result FROM research_tools "
                "WHERE " + ownership + " AND (instr(arguments,?)>0 OR instr(COALESCE(result,''),?)>0) "
                "ORDER BY created_at,rowid", (*ownership_args, query, query),
            ).fetchall()
        items, end, size = [], offset, 0
        for row in rows[offset:offset + limit]:
            item = {"request_id": row["id"], "round_id": row["round_id"], "step": row["step"],
                    "tool": row["tool"], "status": row["status"],
                    "arguments_preview": row["arguments"][:400], "result_preview": (row["result"] or "")[:400]}
            length = len(json.dumps(item, ensure_ascii=False))
            if items and size + length > max_chars - 300:
                break
            items.append(item)
            size += length
            end += 1
        return {"items": items, "offset": offset, "total": len(rows),
                "next_offset": end if end < len(rows) else None}
