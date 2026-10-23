"""One-time import of the retired runtime's local settings; never load its package."""

import json
from pathlib import Path

from folio.runtime.token_estimate import DEFAULT_OUTPUT_TOKEN_BUDGET


def legacy_model_path(directory: Path) -> Path:
    return directory / "nanobot.json"


def import_model(path: Path) -> dict:
    old = json.loads(path.read_text(encoding="utf-8"))
    defaults = old["agents"]["defaults"]
    provider = old["providers"][defaults.get("provider", "custom")]
    return {
        "version": 1,
        "base_url": provider["apiBase"],
        "model": defaults["model"],
        "api_key": provider.get("apiKey", ""),
        "no_auth": not bool(provider.get("apiKey")),
        "max_tokens": defaults.get("maxTokens", DEFAULT_OUTPUT_TOKEN_BUDGET),
        "temperature": defaults.get("temperature", 0.1),
        "decision_provider": "llm",
    }


def migrate_jobs(conn):
    # Completed artifacts and history are immutable. Unfinished old executions
    # require a fresh job, rather than silently mixing incompatible checkpoints.
    for row in conn.execute("SELECT id,input,status FROM jobs").fetchall():
        data = json.loads(row["input"])
        if data.get("runtime") == "nanobot" and row["status"] in {"queued", "running", "failed"}:
            conn.execute(
                "UPDATE jobs SET status='failed',error=? WHERE id=?",
                ("旧引擎任务已停止，请使用原材料创建新任务；旧检查点不会混入新引擎。", row["id"]),
            )
