"""Read-only research audit and downloads, scoped to one existing job."""

import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from folio.research.artifacts import ArtifactError, _read_record, _validate_chain
from folio.research.ledger import ResearchLedger


def install_research_routes(app, store):
    def root_for(job_id):
        try:
            store.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return store.root / "jobs" / job_id / "research"

    def manifests(root):
        return sorted(root.glob("tasks/*/*/.research/artifact_records/*.json"))

    @app.get("/jobs/{job_id}/research")
    def research_status(job_id: str):
        root = root_for(job_id)
        works = store.work_states(job_id)
        tasks = []
        ledger = ResearchLedger(root / "ledger.sqlite") if (root / "ledger.sqlite").is_file() else None
        for task_id, work in works.items():
            if not work.research:
                continue
            research = {key: value for key, value in work.research.items() if key != "task_root"}
            runs = ledger.runs(work.research.get("quota_task_id", task_id)) if ledger else []
            tasks.append({"task_id": task_id, **research, "executions": [
                {key: run[key] for key in ("id", "round_id", "step", "tool", "status", "elapsed_seconds")}
                for run in runs
            ]})
        artifacts = []
        for path in manifests(root):
            try:
                record = _read_record(path)
                _validate_chain(record)
            except (ArtifactError, OSError, ValueError, KeyError):
                continue
            aid = record["artifact_id"]
            artifacts.append({"artifact_id": aid, "status": record["validation"]["status"],
                              "description": record["registration"]["description"],
                              "filename": Path(record["registration"]["output"]["path"]).name,
                              "download_url": f"/jobs/{job_id}/research/artifacts/{aid}"})
        return {"job_id": job_id, "tasks": tasks, "artifacts": artifacts}

    @app.get("/jobs/{job_id}/research/artifacts/{artifact_id}")
    def research_artifact(job_id: str, artifact_id: str):
        root = root_for(job_id)
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise HTTPException(404, "研究产物不存在")
        for path in manifests(root):
            if path.stem != artifact_id:
                continue
            try:
                record = _read_record(path)
                _validate_chain(record)
                target = Path(record["task_root"]) / record["registration"]["output"]["path"]
                if not target.resolve().is_relative_to(root.resolve()):
                    raise ArtifactError("Artifact is outside this job")
            except (ArtifactError, OSError, ValueError, KeyError) as exc:
                raise HTTPException(409, "研究产物的来源或文件版本已变化") from exc
            return FileResponse(target, filename=target.name, media_type="application/octet-stream")
        raise HTTPException(404, "研究产物不存在")
