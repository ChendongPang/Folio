"""Real, isolated replay of a confirmed document job. Never changes the source job."""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

import httpx

from folio.api.app import create_app
from folio.models import DocumentPlan, JobInput
from folio.service import execute, resolve_model_config
from folio.store import Store


def code_digest():
    value = hashlib.sha256()
    for path in sorted([*Path("src/folio").rglob("*.py"), *Path("scripts").glob("*.py")]):
        value.update(str(path).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job")
    parser.add_argument("--root", type=Path, default=Path(".folio"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-calls", type=int, default=2000)
    args = parser.parse_args()
    if (args.output / "result.json").exists():
        raise SystemExit("Refusing to overwrite an earlier benchmark run")
    source = Store(args.root).get(args.source_job)
    request = JobInput.model_validate(source["input"])
    assert request.runtime == "llm", "Stability runs require the real configured model"
    request.document_id = "benchmark_" + uuid.uuid4().hex
    request.limits.run_timeout_seconds = args.timeout
    request.limits.max_calls = args.max_calls
    store = Store(args.output / "data")
    job_id = store.create(request, plan=DocumentPlan.model_validate(source["plan"]))
    report = {
        "job_id": job_id,
        "source_job": args.source_job,
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "working_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "HEAD"])
        ).hexdigest(),
        "input_sha256": hashlib.sha256(
            json.dumps(source["input"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "confirmed_plan_sha256": hashlib.sha256(
            json.dumps(source["plan"], sort_keys=True).encode()
        ).hexdigest(),
        "timeout_seconds": args.timeout,
        "started_at": time.time(),
        "independent_store": str(store.root.resolve()),
        "cached_results_imported": False,
        "code_sha256": code_digest(),
    }
    path = args.output / "result.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    start = time.monotonic()
    try:
        await execute(store, job_id, resolve_model_config(args.root))
    except Exception as exc:
        report["exception"] = f"{type(exc).__name__}: {exc}"
    job = store.get(job_id)
    elapsed = time.monotonic() - start
    issues = (job["snapshot"] or {}).get("blocking_issues", [])
    technical_blockers = [
        f
        for f in issues
        if f["key"].startswith(("review-incomplete:", "adjudication:", "repair-no-progress:"))
    ]
    artifact = Path(job["artifact"]) if job["artifact"] else None
    valid = False
    downloadable = False
    internal_markers = None
    trace_matches = False
    states = store.states(job_id)
    table_errors = [
        node["section_id"]
        for node in source["plan"]["sections"]
        if node.get("expected_table_headers")
        and not any(
            state.section_id == node["section_id"]
            and any(
                b.kind == "table" and b.rows[0] == node["expected_table_headers"]
                for b in state.content.blocks
            )
            for state in states
        )
    ]
    if artifact and artifact.is_file():
        with zipfile.ZipFile(artifact) as archive:
            valid = archive.testzip() is None and "word/document.xml" in archive.namelist()
            xml = ElementTree.fromstring(archive.read("word/document.xml"))
            text = "\n".join(
                t.text or ""
                for t in xml.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
            )
            internal_markers = len(
                re.findall(
                    r"\bev_[a-f0-9]{8,}\b|\bsfref\d+\b|\bmodel_proposal\b|【模型建议】", text
                )
            )
        trace = json.loads(artifact.with_name("trace.json").read_text())
        trace_matches = {s.section_id: s.model_dump() for s in states} == {
            s["section_id"]: s for s in trace
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(store.root)),
            base_url="http://benchmark.test",
        ) as client:
            response = await client.get(f"/jobs/{job_id}/artifact")
            downloadable = response.status_code == 200 and response.content == artifact.read_bytes()
    report.update(
        {
            "elapsed_seconds": round(elapsed, 2),
            "calls": job["calls"],
            "status": job["status"],
            "stage": job["stage"],
            "error": job["error"],
            "saved_nodes": len(store.states(job_id)),
            "valid_docx": valid,
            "download_matches_artifact": downloadable,
            "internal_marker_count": internal_markers,
            "table_contract_errors": table_errors,
            "trace_matches_saved_states": trace_matches,
            "artifact": str(artifact) if artifact else None,
            "review_passed": (job["snapshot"] or {}).get("review_passed", False),
            "blocking_kinds": dict(Counter(f["kind"] for f in issues)),
            "technical_blockers": technical_blockers,
            "within_target": elapsed <= 600,
            "within_deadline": elapsed <= args.timeout,
            "code_unchanged": report["code_sha256"] == code_digest(),
        }
    )
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT task_key,attempts,error,usage FROM tasks WHERE job_id=?", (job_id,)
        ).fetchall()
        report["requests_by_action"] = dict(Counter(row["task_key"].split(":")[0] for row in rows))
        report["request_errors"] = [dict(row) | {"usage": None} for row in rows if row["error"]]
        report["failed_attempts"] = [
            json.loads(row["data"]) | {"created_at": row["created_at"]}
            for row in conn.execute(
                "SELECT data,created_at FROM events WHERE job_id=? AND kind='task_failed' ORDER BY id",
                (job_id,),
            )
        ]
        report["retry_count"] = sum(max(0, row["attempts"] - 1) for row in rows)
        report["prompt_tokens"] = sum(
            json.loads(row["usage"] or "{}").get("prompt_tokens", 0) for row in rows
        )
        report["completion_tokens"] = sum(
            json.loads(row["usage"] or "{}").get("completion_tokens", 0) for row in rows
        )
        report["validated_padding_stops"] = sum(
            json.loads(row["usage"] or "{}").get("completion_termination")
            == "validated_json_padding"
            for row in rows
        )
    report["operational_pass"] = bool(
        valid
        and downloadable
        and trace_matches
        and internal_markers == 0
        and not table_errors
        and not technical_blockers
        and elapsed <= args.timeout
        and report["code_unchanged"]
        and len(store.states(job_id)) == len(source["plan"]["sections"])
        and job["status"] in {"published", "draft", "needs_confirmation"}
    )
    report["passed"] = bool(
        report["operational_pass"]
        and report["review_passed"]
        and elapsed <= args.timeout
        and job["status"] in {"published", "draft"}
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
