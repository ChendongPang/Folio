"""SQLite checkpoints and compare-and-swap publication, backed by job file locks."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from folio.document.reader import read_docx
from folio.legacy_upgrade import upgrade_job_input, upgrade_plan
from folio.models import DocumentPlan, JobInput, Limits, SectionState, digest
from folio.orchestration.work import WorkPlan, WorkState

review_logger = logging.getLogger("folio.review")
# Uvicorn configures its own loggers; the CLI captures stderr into gui.log.
# Keep this local to review diagnostics without changing application-wide logging.
if review_logger.level == logging.NOTSET:
    review_logger.setLevel(logging.INFO)
if not review_logger.handlers:
    review_handler = logging.StreamHandler()
    review_handler.setFormatter(logging.Formatter("%(message)s"))
    review_logger.addHandler(review_handler)


class PublicationConflict(RuntimeError):
    pass


class SectionRevisionConflict(RuntimeError):
    """A proposal was based on section revisions that are no longer current."""


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "folio.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, published_version TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, baseline TEXT,
                    status TEXT NOT NULL, stage TEXT NOT NULL, input TEXT NOT NULL,
                    plan TEXT, snapshot TEXT NOT NULL DEFAULT '{}', error TEXT,
                    artifact TEXT, calls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sections (
                    job_id TEXT NOT NULL, section_id TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(job_id, section_id)
                );
                CREATE TABLE IF NOT EXISTS work_plans (
                    job_id TEXT PRIMARY KEY, plan TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_states (
                    job_id TEXT NOT NULL, task_id TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(job_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    job_id TEXT NOT NULL, task_key TEXT NOT NULL, attempts INTEGER NOT NULL,
                    result TEXT, usage TEXT, error TEXT,
                    PRIMARY KEY(job_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS retrievals (
                    job_id TEXT NOT NULL, retrieval_key TEXT NOT NULL, evidence TEXT NOT NULL,
                    PRIMARY KEY(job_id, retrieval_key)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    job_id TEXT, decision_key TEXT, task TEXT, context TEXT, result TEXT, metadata TEXT,
                    PRIMARY KEY(job_id, decision_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS events_job_kind ON events(job_id, kind);
                CREATE TABLE IF NOT EXISTS confirmation_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(job_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS call_budgets (
                    job_id TEXT NOT NULL, bucket TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, bucket)
                );
            """)
            from folio.migrations import migrate_jobs

            migrate_jobs(conn)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(
        self, request: JobInput, *, plan: DocumentPlan | None = None,
        directory_research=None, writing_profile=None,
    ) -> str:
        knowledge = (
            Path(request.knowledge_path).expanduser().resolve() if request.knowledge_path else None
        )
        if knowledge is not None and not knowledge.is_dir():
            raise ValueError("knowledge_path must be a directory")
        if knowledge and self.root.is_relative_to(knowledge):
            raise ValueError("Knowledge directory cannot contain the job storage directory")
        job_id = uuid4().hex
        document_id = request.document_id or "doc_" + uuid4().hex
        folder = self.root / "jobs" / job_id
        inputs = folder / "inputs"
        inputs.mkdir(parents=True)
        try:
            sample_sources = [Path(path).expanduser().resolve() for path in request.sample_paths]
            ordered_sample_hashes = [sha256(path.read_bytes()).hexdigest() for path in sample_sources]
            sample_hashes = set(ordered_sample_hashes)
            template_hash = None
            template_source_path = None
            if plan is not None and plan.template is not None:
                from folio.document.template_contract import validate_template_plan

                validate_template_plan(plan)
                source_path = Path(plan.template.path).expanduser().resolve()
                template_source_path = source_path
                template_bytes = source_path.read_bytes()
                template_hash = sha256(template_bytes).hexdigest()
                if template_hash != plan.template.source_hash:
                    raise ValueError("模板文件已改变，请重新生成并确认目录")
                destination = inputs / "template.docx"
                destination.write_bytes(template_bytes)
                # Never change the caller's reviewed directory or its input path.
                plan = plan.model_copy(deep=True)
                plan.template.path = str(destination)

            def is_template_source(path):
                # Duplicate uploads must not turn the same template's examples
                # into project evidence merely by acquiring another filename.
                return bool(
                    template_hash
                    and (
                        path.resolve() == template_source_path
                        or path.suffix.lower() == ".docx"
                        and sha256(path.read_bytes()).hexdigest() == template_hash
                    )
                )

            def is_nonfactual_source(path):
                return is_template_source(path) or bool(
                    sample_hashes and sha256(path.read_bytes()).hexdigest() in sample_hashes
                )

            if request.requirements is not None:
                (inputs / "requirements.json").write_text(
                    request.requirements.model_dump_json(indent=2), encoding="utf-8"
                )
            if request.writing_guide is not None:
                (inputs / "writing-guide.json").write_text(
                    request.writing_guide.model_dump_json(indent=2), encoding="utf-8"
                )
            frozen_references = []
            for index, reference in enumerate(request.reference_paths):
                source_path = Path(reference).expanduser().resolve()
                if is_nonfactual_source(source_path):
                    continue
                read_docx(source_path, request.reference_revision_view)
                destination = inputs / "references" / str(index) / source_path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
                frozen_references.append(str(destination))
            frozen_samples = []
            for index, sample_path in enumerate(sample_sources):
                from folio.research.source_index import cached_source

                cached_source(sample_path, self.root / "source-cache")
                destination = inputs / "samples" / str(index) / sample_path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sample_path, destination)
                if sha256(destination.read_bytes()).hexdigest() != ordered_sample_hashes[index]:
                    raise ValueError("风格示例文件在快照期间发生变化，请重新生成目录")
                frozen_samples.append(str(destination))
            if writing_profile is not None:
                from folio.document.style_profile import validate_profile_sources

                if not isinstance(writing_profile, dict):
                    raise ValueError("已确认的风格说明与示例文件不一致，请重新生成目录")
                validate_profile_sources(writing_profile, frozen_samples)
                style_folder = inputs / "style"
                style_folder.mkdir()
                (style_folder / "profile.json").write_text(
                    json.dumps(writing_profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                (style_folder / "style.md").write_text(
                    writing_profile.get("markdown", writing_profile.get("style_markdown", "")), encoding="utf-8"
                )
            # Snapshot supported sources only. Do not follow symlinks outside the source directory.
            target = inputs / "knowledge"
            target.mkdir()
            for source in sorted(knowledge.rglob("*")) if knowledge else []:
                from folio.research.formats import SUPPORTED_SUFFIXES

                if not source.is_file() or source.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                if not source.resolve().is_relative_to(knowledge):
                    raise ValueError("Knowledge symlink points outside the source directory")
                if is_nonfactual_source(source):
                    continue
                dest = target / source.relative_to(knowledge)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
            if directory_research is not None:
                if plan is None:
                    raise ValueError("目录研究交接必须对应已确认目录")
                from folio.research.planning_handoff import freeze_directory_research

                freeze_directory_research(
                    directory_research, inputs, target, plan, cache_root=self.root / "source-cache"
                )
            frozen = request.model_copy(
                update={
                    "document_id": document_id,
                    "reference_paths": frozen_references,
                    "sample_paths": frozen_samples,
                    "knowledge_path": str(target) if knowledge else None,
                }
            )
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT OR IGNORE INTO documents(id) VALUES (?)", (document_id,))
                baseline = conn.execute(
                    "SELECT published_version FROM documents WHERE id=?", (document_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO jobs(id,document_id,baseline,status,stage,input,plan) VALUES (?,?,?,?,?,?,?)",
                    (
                        job_id,
                        document_id,
                        baseline,
                        "queued",
                        "queued",
                        frozen.model_dump_json(),
                        plan.model_dump_json() if plan is not None else None,
                    ),
                )
                self._event(conn, job_id, "created", {"document_id": document_id})
        except Exception:
            shutil.rmtree(folder)
            raise
        return job_id

    def get(self, job_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        data = dict(row)
        for key in ("input", "plan", "snapshot"):
            data[key] = json.loads(data[key]) if data[key] else None
        data["input"] = upgrade_job_input(data["input"])
        if data["plan"] is not None:
            data["plan"] = upgrade_plan(data["plan"])
        data["completed_sections"] = len(self.states(job_id))
        data["total_sections"] = len(data["plan"]["sections"]) if data["plan"] else None
        work_plan = self.work_plan(job_id)
        data["work_plan"] = work_plan.model_dump() if work_plan else None
        return data

    def candidates(self) -> list[str]:
        with self.connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','running','pausing') ORDER BY created_at,id"
                )
            ]

    def job_status(self, job_id: str) -> str:
        """Read lifecycle state without decoding a potentially large document snapshot."""
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return row[0]

    def _control_status(self, job_id: str, action: str) -> dict:
        """Serialize pause/start/resume with workers; never clear saved work or budgets."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,stage FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            status, stage = row["status"], row["stage"]
            next_status = status
            if action == "pause":
                if status not in {"queued", "running", "pausing", "paused"}:
                    raise ValueError("任务已结束，无法暂停。")
                next_status = {"queued": "paused", "running": "pausing"}.get(status, status)
            elif action == "resume":
                if status not in {"paused", "failed", "queued", "running"}:
                    raise ValueError("仅暂停或失败的任务可以继续；正在暂停时请等待暂停完成。")
                if status in {"paused", "failed"}:
                    # An explicit resume authorizes a fresh, bounded retry window.
                    # Parallel tasks may have already failed before the user
                    # pauses their siblings; those failures need the same grant.
                    # Keep attempts, spend and checkpoints; automatic workers must
                    # never silently reopen an exhausted failed request.
                    failed_tasks = conn.execute(
                        "SELECT task_key,attempts FROM tasks "
                        "WHERE job_id=? AND result IS NULL AND error IS NOT NULL",
                        (job_id,),
                    ).fetchall()
                    for task in failed_tasks:
                        previous = conn.execute(
                            "SELECT data FROM events WHERE job_id=? AND kind='task_retry_authorized' "
                            "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                            (job_id, task["task_key"]),
                        ).fetchone()
                        if previous and json.loads(previous["data"])["attempts"] >= task["attempts"]:
                            continue
                        self._event(conn, job_id, "task_retry_authorized", {
                            "key": task["task_key"],
                            "attempts": task["attempts"],
                        })
                if status in {"paused", "failed"}:
                    next_status = "queued"
            elif action == "acknowledge":
                if status == "pausing":
                    next_status = "paused"
            elif action == "start":
                # Only the owner of the job file lock may start or acknowledge.
                if status == "pausing":
                    next_status = "paused"
                elif status in {"queued", "running"}:
                    next_status = "running"
                    if stage == "queued":
                        stage = "planning"
            else:
                raise ValueError("Unknown job control action")
            if next_status != status:
                conn.execute(
                    "UPDATE jobs SET status=?,stage=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (next_status, stage, job_id),
                )
                self._event(conn, job_id, "job_control", {
                    "status": next_status, "action": action, "previous_status": status,
                })
        return self.get(job_id)

    def request_pause(self, job_id: str) -> dict:
        return self._control_status(job_id, "pause")

    def resume_job(self, job_id: str) -> dict:
        """Explicitly resume saved work, granting failed requests one retry window."""
        return self._control_status(job_id, "resume")

    def acknowledge_pause(self, job_id: str) -> dict:
        """Call only after the owner has drained requests and local export work."""
        return self._control_status(job_id, "acknowledge")

    def start_execution(self, job_id: str) -> dict:
        """Claim a runnable state while holding the job file lock."""
        return self._control_status(job_id, "start")

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        error: str | None = None,
        artifact: Path | None = None,
        snapshot: dict | None = None,
    ) -> None:
        values = {"error": error}
        if status is not None:
            values["status"] = status
        if stage is not None:
            values["stage"] = stage
        if artifact is not None:
            values["artifact"] = str(artifact)
        if snapshot is not None:
            values["snapshot"] = json.dumps(snapshot, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {','.join(k + '=?' for k in values)},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (*values.values(), job_id),
            )
            self._event(
                conn, job_id, "progress", {k: v for k, v in values.items() if k != "snapshot"}
            )

    def save_plan(self, job_id: str, plan: DocumentPlan) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET plan=? WHERE id=?", (plan.model_dump_json(), job_id))

    def states(self, job_id: str) -> list[SectionState]:
        with self.connect() as conn:
            return [
                SectionState.model_validate_json(r[0])
                for r in conn.execute(
                    "SELECT state FROM sections WHERE job_id=? ORDER BY section_id", (job_id,)
                )
            ]

    def work_plan(self, job_id: str) -> WorkPlan | None:
        with self.connect() as conn:
            row = conn.execute("SELECT plan FROM work_plans WHERE job_id=?", (job_id,)).fetchone()
            return WorkPlan.model_validate_json(row[0]) if row else None

    def save_work_plan(self, job_id: str, plan: WorkPlan) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] == "published":
                raise ValueError("Published versions are immutable")
            conn.execute(
                "INSERT INTO work_plans VALUES (?,?)",
                (job_id, plan.model_dump_json()),
            )
            self._event(conn, job_id, "work_plan_saved", {"tasks": len(plan.tasks)})

    def completed_dependency_audit(self, job_id: str, plan_hash: str, policy: int) -> bool:
        """A persisted effective graph needs no repeat audit on recovery or model changes."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT result FROM tasks WHERE job_id=? AND task_key LIKE 'audit_work_dependencies:%' AND result IS NOT NULL",
                (job_id,),
            ).fetchall()
        for row in rows:
            try:
                result = json.loads(row[0])
                if (
                    result.get("policy") == policy
                    and result.get("target_hash") == plan_hash
                    and result.get("status") in {"applied", "unchanged"}
                ):
                    return True
            except (ValueError, AttributeError):
                continue
        return False

    def apply_dependency_audit(
        self,
        job_id: str,
        key: str,
        source_hash: str,
        removals: set[tuple[str, str]],
        audit: dict,
        usage: dict,
    ) -> tuple[WorkPlan, bool]:
        """Atomically cache the audit and remove only still-unstarted task input edges."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT jobs.status, work_plans.plan FROM jobs JOIN work_plans ON jobs.id=work_plans.job_id WHERE jobs.id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Missing work plan for dependency audit")
            current = WorkPlan.model_validate_json(row["plan"])
            call = conn.execute(
                "SELECT result FROM tasks WHERE job_id=? AND task_key=?", (job_id, key)
            ).fetchone()
            if call is None:
                raise ValueError("Dependency audit was not counted as a model call")
            if call["result"] is not None:
                return current, False
            started = {
                r[0]
                for r in conn.execute("SELECT task_id FROM work_states WHERE job_id=?", (job_id,))
            }
            written = {
                r[0]
                for r in conn.execute("SELECT section_id FROM sections WHERE job_id=?", (job_id,))
            }
            started.update(t.task_id for t in current.tasks if written.intersection(t.section_ids))
            edges = {(t.task_id, d) for t in current.tasks for d in t.depends_on}
            eligible = (
                row["status"] != "published"
                and digest(current.model_dump_json()) == source_hash
                and removals <= edges
                and not any(task in started for task, _ in removals)
            )
            updated = current
            if eligible:
                updated = WorkPlan.model_validate(
                    current.model_copy(
                        update={
                            "tasks": [
                                t.model_copy(
                                    update={
                                        "depends_on": [
                                            d
                                            for d in t.depends_on
                                            if (t.task_id, d) not in removals
                                        ]
                                    }
                                )
                                for t in current.tasks
                            ]
                        }
                    ).model_dump()
                )
                conn.execute(
                    "UPDATE work_plans SET plan=? WHERE job_id=?",
                    (updated.model_dump_json(), job_id),
                )
            target_hash = digest(updated.model_dump_json())
            result = audit | {
                "source_hash": source_hash,
                "target_hash": target_hash,
                "status": "applied"
                if eligible and removals
                else "unchanged"
                if eligible
                else "stale",
            }
            conn.execute(
                "UPDATE tasks SET result=?,usage=?,error=NULL WHERE job_id=? AND task_key=?",
                (json.dumps(result, ensure_ascii=False), json.dumps(usage), job_id, key),
            )
            self._event(conn, job_id, "task_completed", {"key": key})
            self._event(
                conn,
                job_id,
                "dependency_audit_completed",
                {
                    "status": result["status"],
                    "source_hash": source_hash,
                    "target_hash": target_hash,
                    "removed_edges": sorted(removals) if eligible else [],
                },
            )
            return updated, eligible

    def work_states(self, job_id: str) -> dict[str, WorkState]:
        with self.connect() as conn:
            return {
                r["task_id"]: WorkState.model_validate_json(r["state"])
                for r in conn.execute("SELECT * FROM work_states WHERE job_id=?", (job_id,))
            }

    def save_work_state(self, job_id: str, state: WorkState) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job[0] == "published":
                raise ValueError("Published versions are immutable")
            conn.execute(
                "INSERT INTO work_states VALUES (?,?,?) ON CONFLICT(job_id,task_id) DO UPDATE SET state=excluded.state",
                (job_id, state.task_id, state.model_dump_json()),
            )
            conn.execute("UPDATE jobs SET snapshot='{}',artifact=NULL WHERE id=?", (job_id,))
            self._event(
                conn,
                job_id,
                "work_state_saved",
                {
                    "task_id": state.task_id,
                    "researched": state.researched,
                    "reviewed": bool(state.review_key),
                    "issues": len(state.findings),
                },
            )

    def save_section(self, job_id: str, state: SectionState) -> None:
        self.save_sections_batch(job_id, [state])

    def save_sections_batch(
        self,
        job_id: str,
        states: list[SectionState],
        *,
        expected_revisions: dict[str, int | None] | None = None,
        confirmation_ids: list[int] | None = None,
    ) -> None:
        """Commit a proposal and its invalidations atomically against its read set.

        The expected revisions may include unchanged chapters read by the agent.
        None means that a chapter must not yet exist. Legacy single-section
        callers remain supported; agent submissions should provide a baseline.
        """
        section_ids = {state.section_id for state in states}
        if len(section_ids) != len(states):
            raise ValueError("Duplicate section in a submission batch")
        if expected_revisions is not None and not section_ids.issubset(expected_revisions):
            raise ValueError("Every submitted section must have an expected revision")
        if expected_revisions is not None:
            for state in states:
                baseline = expected_revisions[state.section_id]
                if state.revision < 1 or (baseline is not None and state.revision <= baseline):
                    raise ValueError("A submitted section must advance its revision")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] == "published":
                raise ValueError("Published versions are immutable")
            for section_id, expected in (expected_revisions or {}).items():
                row = conn.execute(
                    "SELECT state FROM sections WHERE job_id=? AND section_id=?",
                    (job_id, section_id),
                ).fetchone()
                actual = SectionState.model_validate_json(row[0]).revision if row else None
                if actual != expected:
                    raise SectionRevisionConflict(
                        f"Section {section_id} changed: expected revision {expected}, found {actual}"
                    )
            for confirmation_id in confirmation_ids or []:
                changed = conn.execute(
                    "UPDATE confirmation_rounds SET status='applied' WHERE id=? AND job_id=? AND status='pending'",
                    (confirmation_id, job_id),
                ).rowcount
                if changed != 1:
                    raise SectionRevisionConflict("Confirmation round is no longer pending")
                self._event(conn, job_id, "confirmation_applied", {"round_id": confirmation_id})
            if not states:
                return
            for state in states:
                conn.execute(
                    "INSERT INTO sections(job_id,section_id,state) VALUES (?,?,?) ON CONFLICT(job_id,section_id) DO UPDATE SET state=excluded.state",
                    (job_id, state.section_id, state.model_dump_json()),
                )
            conn.execute("UPDATE jobs SET snapshot='{}',artifact=NULL WHERE id=?", (job_id,))
            plan_row = conn.execute(
                "SELECT plan FROM work_plans WHERE job_id=?", (job_id,)
            ).fetchone()
            if plan_row:
                plan = WorkPlan.model_validate_json(plan_row[0])
                affected = {
                    t.task_id for t in plan.tasks if section_ids.intersection(t.section_ids)
                }
                while True:
                    expanded = affected | {
                        t.task_id for t in plan.tasks if set(t.depends_on) & affected
                    }
                    if expanded == affected:
                        break
                    affected = expanded
                for row in conn.execute(
                    "SELECT task_id,state FROM work_states WHERE job_id=?", (job_id,)
                ).fetchall():
                    if row["task_id"] in affected:
                        work = WorkState.model_validate_json(row["state"])
                        work.review_key = ""
                        conn.execute(
                            "UPDATE work_states SET state=? WHERE job_id=? AND task_id=?",
                            (work.model_dump_json(), job_id, work.task_id),
                        )
            for state in states:
                self._event(
                    conn,
                    job_id,
                    "section_saved",
                    {
                        "section_id": state.section_id,
                        "revision": state.revision,
                        "issues": len(state.local_findings),
                    },
                )

    def task(self, job_id: str, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE job_id=? AND task_key=?", (job_id, key)
            ).fetchone()
            return dict(row) if row else None

    def task_retry_baseline(self, job_id: str, key: str) -> int:
        """Attempts consumed before the latest explicit paused/failed-job resume."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM events WHERE job_id=? AND kind='task_retry_authorized' "
                "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                (job_id, key),
            ).fetchone()
            return int(json.loads(row["data"])["attempts"]) if row else 0

    def recover_interrupted_task(self, job_id: str, key: str) -> dict | None:
        """Reclaim one unfinished attempt under the caller's job/request lock.

        The provider request still counts towards jobs.calls. A durable event makes
        recovery idempotent until a new task_started event records another call.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE job_id=? AND task_key=?", (job_id, key)
            ).fetchone()
            if row is None:
                return None
            task = dict(row)
            if task["result"] is not None or task["error"] is not None or task["attempts"] < 1:
                return task
            last = conn.execute(
                "SELECT kind FROM events WHERE job_id=? "
                "AND kind IN ('task_started','task_interruption_recovered') "
                "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                (job_id, key),
            ).fetchone()
            if last is not None and last["kind"] == "task_interruption_recovered":
                return task
            task["attempts"] -= 1
            conn.execute(
                "UPDATE tasks SET attempts=? WHERE job_id=? AND task_key=?",
                (task["attempts"], job_id, key),
            )
            self._event(
                conn,
                job_id,
                "task_interruption_recovered",
                {"key": key, "attempts": task["attempts"]},
            )
            return task

    def call_budget_usage(self, job_id: str, bucket: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT calls FROM call_budgets WHERE job_id=? AND bucket=?", (job_id, bucket),
            ).fetchone()
            return row[0] if row else 0

    def begin_task(self, job_id: str, key: str, max_calls: int, *, budget_policy=None) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            calls = conn.execute("SELECT calls FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if budget_policy is not None:
                from folio.runtime.call_budget import (
                    ResearchModelDeferred,
                    VisualPrefetchDeferred,
                )

                row = conn.execute(
                    "SELECT calls FROM call_budgets WHERE job_id=? AND bucket=?",
                    (job_id, budget_policy.bucket),
                ).fetchone()
                used = row[0] if row else 0
                if (used >= budget_policy.max_calls
                        or calls >= max_calls - budget_policy.reserve_calls):
                    error_type = (
                        ResearchModelDeferred if budget_policy.purpose == "research"
                        else VisualPrefetchDeferred
                    )
                    raise error_type(
                        bucket=budget_policy.bucket, used=used, limit=budget_policy.max_calls,
                        calls=calls, reserve_calls=budget_policy.reserve_calls,
                    )
                conn.execute(
                    "INSERT INTO call_budgets(job_id,bucket,calls) VALUES (?,?,1) "
                    "ON CONFLICT(job_id,bucket) DO UPDATE SET calls=calls+1",
                    (job_id, budget_policy.bucket),
                )
            if calls >= max_calls:
                from folio.runtime.errors import ModelCallBudgetExceeded

                raise ModelCallBudgetExceeded(calls, max_calls)
            conn.execute("UPDATE jobs SET calls=calls+1 WHERE id=?", (job_id,))
            conn.execute(
                "INSERT INTO tasks(job_id,task_key,attempts) VALUES (?,?,1) ON CONFLICT(job_id,task_key) DO UPDATE SET attempts=attempts+1,error=NULL",
                (job_id, key),
            )
            attempts = conn.execute(
                "SELECT attempts FROM tasks WHERE job_id=? AND task_key=?", (job_id, key)
            ).fetchone()[0]
            self._event(conn, job_id, "task_started", {"key": key, "attempt": attempts})
            return attempts

    def finish_task(
        self,
        job_id: str,
        key: str,
        *,
        result: str | None = None,
        usage: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET result=?,usage=?,error=? WHERE job_id=? AND task_key=?",
                (result, json.dumps(usage or {}), error, job_id, key),
            )
            self._event(
                conn,
                job_id,
                "task_failed" if error else "task_completed",
                {"key": key, "error": error, "usage": usage or {}},
            )

    def retrieval(self, job_id: str, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT evidence FROM retrievals WHERE job_id=? AND retrieval_key=?", (job_id, key)
            ).fetchone()
            return row[0] if row else None

    def save_retrieval(self, job_id: str, key: str, evidence: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO retrievals(job_id,retrieval_key,evidence) VALUES (?,?,?)",
                (job_id, key, evidence),
            )
            self._event(conn, job_id, "retrieval_saved", {"key": key})

    def record_decision(self, job_id, key, task, context, result, metadata):
        with self.connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?)",
                (
                    job_id,
                    key,
                    task,
                    json.dumps(context, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            if inserted.rowcount:
                self._event(
                    conn,
                    job_id,
                    "decision_completed",
                    {"task": task, "key": key, **metadata, "summary": result.get("summary")},
                )

    def audit_event(self, job_id: str, kind: str, data: dict) -> None:
        """Persist structured local diagnostics without exposing them as UI prose."""
        with self.connect() as conn:
            self._event(conn, job_id, kind, data)

    def begin_provider_attempt(self, job_id: str, data: dict) -> None:
        """Reserve one actual model HTTP operation and its audit atomically.

        The logical task allowance remains separate. Retries and decision pages
        each consume this durable provider allowance before any request is sent.
        Audit-only scopes without a document job retain their existing behavior.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT input FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None and data.get("provider") in {"llm", "decision"}:
                limit = json.loads(row["input"]).get("limits", {}).get(
                    "max_calls", Limits.model_fields["max_calls"].default,
                )
                used = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='provider_started' "
                    "AND json_extract(data,'$.provider') IN ('llm','decision')",
                    (job_id,),
                ).fetchone()[0]
                if used >= limit:
                    from folio.runtime.errors import ModelCallBudgetExceeded

                    raise ModelCallBudgetExceeded(used, limit)
            self._event(conn, job_id, "provider_started", data)

    def work_event(
        self,
        job_id: str,
        text: str,
        *,
        review: dict | None = None,
        task_id: str | None = None,
        task_usage: dict | None = None,
    ) -> None:
        # The caller supplies operational metadata only, never model input or
        # source text. Round-trip once so the event and log share an immutable
        # JSON snapshot even when the caller later changes its counters.
        data = {"text": text}
        if review is not None:
            data["review"] = json.loads(json.dumps(review, ensure_ascii=False))
        if task_id is not None:
            data["task_id"] = task_id
        if task_usage is not None:
            data["task_usage"] = json.loads(json.dumps(task_usage, ensure_ascii=False))
        with self.connect() as conn:
            self._event(conn, job_id, "work_update", data)
            row = (
                conn.execute(
                    "SELECT id, created_at FROM events WHERE id=last_insert_rowid()"
                ).fetchone()
                if review is not None or task_usage is not None
                else None
            )
        if row is not None:
            record = {
                "event_id": row["id"],
                "created_at": row["created_at"],
                "job_id": job_id,
            } | data
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            review_logger.info("%s", line)
            try:
                directory = self.root / "jobs" / job_id
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "review.jsonl").open("ab", buffering=0) as log:
                    log.write((line + "\n").encode("utf-8"))
            except OSError as exc:
                # SQLite is authoritative. A diagnostic-file problem must not
                # turn a completed review into a failed generation job.
                review_logger.warning(
                    "%s",
                    json.dumps(
                        {
                            "job_id": job_id,
                            "event_id": row["id"],
                            "diagnostic": "review_log_write_failed",
                            "error_type": type(exc).__name__,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(r) | {"data": json.loads(r["data"])}
                for r in conn.execute(
                    "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 500",
                    (job_id, after),
                )
            ]

    def publish(self, job_id: str, artifact: Path) -> None:
        if not artifact.is_file():
            raise ValueError("Cannot publish a missing artifact")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            snapshot = json.loads(job["snapshot"])
            if snapshot.get("blocking_issues") or not snapshot.get("review_passed"):
                raise ValueError("Review has not passed")
            states = [
                SectionState.model_validate_json(row[0])
                for row in conn.execute(
                    "SELECT state FROM sections WHERE job_id=? ORDER BY section_id", (job_id,)
                )
            ]
            plan = DocumentPlan.model_validate(upgrade_plan(json.loads(job["plan"])))
            if plan.template is not None:
                from folio.document.template_contract import validate_template_plan

                validate_template_plan(plan)
            if {s.section_id for s in states} != {s.section_id for s in plan.sections}:
                raise ValueError("Cannot publish incomplete sections")
            state_digest = digest("".join(s.model_dump_json() for s in states))
            if snapshot.get("state_digest") != state_digest:
                raise ValueError("Review is stale or sections contain unresolved issues")
            work_plan = conn.execute(
                "SELECT plan FROM work_plans WHERE job_id=?", (job_id,)
            ).fetchone()
            work = {}
            typed_work_plan = None
            if work_plan:
                work = {
                    r["task_id"]: WorkState.model_validate_json(r["state"])
                    for r in conn.execute("SELECT * FROM work_states WHERE job_id=?", (job_id,))
                }
                typed_work_plan = WorkPlan.model_validate_json(work_plan[0])
                expected = {t.task_id for t in typed_work_plan.tasks}
                if (
                    set(work) != expected
                    or any(not w.review_key for w in work.values())
                    or snapshot.get("work_states") != {k: w.model_dump() for k, w in work.items()}
                ):
                    raise ValueError("Business task review is missing or stale")
                from folio.research.workflow import research_findings

                for task in typed_work_plan.tasks:
                    if research_findings(
                        None, task, work[task.task_id],
                        [s for s in states if s.section_id in task.section_ids],
                        update_coverage=False,
                    ):
                        raise ValueError("Research or complete source coverage has not passed")
            from folio.orchestration.local_checks import finding_version, hard_findings

            request = JobInput.model_validate(upgrade_job_input(json.loads(job["input"])))
            from folio.confirmations import ConfirmedReferences, rounds

            references = ConfirmedReferences(
                request.reference_paths, request.reference_revision_view, rounds(self, job_id)
            )
            definitions = {section.section_id: section for section in plan.sections}
            for state in states:
                section = definitions[state.section_id]
                if section.content_mode != "container" and hard_findings(
                    section,
                    state.content,
                    state.evidence,
                    state.extraction,
                    (e.evidence_id for e in references.evidence(section, None)),
                ):
                    raise ValueError("Sections contain deterministic validation failures")
            try:
                adjudications = json.loads(
                    (self.root / "jobs" / job_id / "adjudications.json").read_text()
                )
            except (OSError, ValueError):
                adjudications = {}
            if not isinstance(adjudications, dict):
                adjudications = {}
            for state in states:
                for finding in state.local_findings:
                    reason = adjudications.get(
                        finding_version(finding, states, typed_work_plan, work)
                    )
                    if (
                        finding.kind == "pending"
                        or not isinstance(reason, str)
                        or not reason.strip()
                    ):
                        raise ValueError(
                            "Sections contain unresolved issues without current adjudication"
                        )
            if artifact.resolve().parent != self.root / "jobs" / job_id:
                raise ValueError("Artifact must belong to the working version")
            if plan.template is not None:
                from folio.document.template_layout import validate_template_output

                validate_template_output(artifact, plan)
            if json.loads(job["input"])["runtime"] == "demo":
                raise ValueError("Demo artifacts cannot be published")
            head = conn.execute(
                "SELECT published_version FROM documents WHERE id=?", (job["document_id"],)
            ).fetchone()[0]
            if head == job_id:
                return  # Idempotent retry after a successful commit.
            if head != job["baseline"]:
                raise PublicationConflict(
                    "Published baseline changed; create a new job against the latest version"
                )
            conn.execute(
                "UPDATE documents SET published_version=? WHERE id=?", (job_id, job["document_id"])
            )
            conn.execute(
                "UPDATE jobs SET status='published',stage='complete',artifact=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(artifact), job_id),
            )
            self._event(conn, job_id, "published", {"artifact": str(artifact)})

    @staticmethod
    def _event(conn, job_id: str, kind: str, data: dict) -> None:
        conn.execute(
            "INSERT INTO events(job_id,kind,data) VALUES (?,?,?)",
            (job_id, kind, json.dumps(data, ensure_ascii=False)),
        )
