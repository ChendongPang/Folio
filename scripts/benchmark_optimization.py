"""One fresh, real replay per invocation; a durable ledger caps the series at ten.

The source job and its results are read-only. Only the explicitly requested task
parallelism and run deadline may differ from its input. Credentials are neither
printed nor copied into reports; configuration identity is recorded as hashes.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import posixpath
import re
import sqlite3
import statistics
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from benchmark_job import code_digest
from filelock import FileLock

from folio.legacy_upgrade import upgrade_job_input, upgrade_plan
from folio.models import DocumentPlan, JobInput
from folio.service import execute, resolve_model_config
from folio.store import Store

TARGET_SECONDS = 420
MAX_RUNS = 10
EXPECTED_SECTIONS = 53
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def utc_now():
    return datetime.now(UTC).isoformat()


def checksum(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def file_checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_source(root, job_id):
    with sqlite3.connect(f"file:{(root / 'folio.sqlite3').resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("Source job does not exist")
    result = dict(row)
    for key in ("input", "plan", "snapshot"):
        result[key] = json.loads(result[key] or "{}")
    result["input"] = upgrade_job_input(result["input"])
    if result["plan"]:
        result["plan"] = upgrade_plan(result["plan"])
    return result


def source_signature(source, config):
    request = source["input"]
    files = request.get("reference_paths", []) + request.get("sample_paths", [])
    knowledge = Path(request["knowledge_path"]) if request.get("knowledge_path") else None
    local_knowledge = (
        [
            (str(path.relative_to(knowledge)), file_checksum(path))
            for path in sorted(knowledge.rglob("*"))
            if path.is_file()
        ]
        if knowledge
        else []
    )
    return {
        "input_sha256": checksum(request),
        "confirmed_plan_sha256": checksum(source["plan"]),
        "source_files_sha256": [file_checksum(path) for path in files],
        "model_configuration_sha256": file_checksum(config),
        "knowledge_configuration_sha256": checksum(request.get("ragflow") or {}),
        "local_knowledge_sha256": checksum(local_knowledge),
    }


def reserve_run(root, source_job, round_number, signature, overrides):
    """Reserve before any real request. Failed/interrupted reservations still count."""
    path = root / "ledger.json"
    with FileLock(root / "ledger.lock"):
        ledger = json.loads(path.read_text())
        if ledger["source_job"] != source_job:
            raise ValueError("Ledger belongs to another source job")
        if not 1 <= round_number <= 10 or not any(
            item["round"] == round_number for item in ledger["optimization_rounds"]
        ):
            raise ValueError("Optimization round must already exist in the ledger")
        if len(ledger["optimization_rounds"]) > min(10, ledger.get("max_optimization_rounds", 10)):
            raise ValueError("Optimization round limit exceeded")
        runs = ledger["full_runs"]
        if len(runs) >= min(MAX_RUNS, ledger.get("max_full_runs", MAX_RUNS)):
            raise ValueError("Configured full-run limit reached; refusing another real replay")
        if any(item.get("status") == "running" for item in runs):
            raise ValueError(
                "A run is already reserved as running; reconcile it before another replay"
            )
        baseline = ledger.get("source_signature")
        if baseline is not None and baseline != signature:
            raise ValueError(
                "Source files, confirmed plan, model or knowledge configuration changed"
            )
        ledger["source_signature"] = signature
        index = len(runs) + 1
        destination = root / f"run-{index:02d}"
        destination.mkdir(parents=False, exist_ok=False)
        entry = {
            "run": index,
            "round": round_number,
            "status": "running",
            "started_at": utc_now(),
            "pid": os.getpid(),
            "output": str(destination.resolve()),
            "result": str((destination / "result.json").resolve()),
            "overrides": overrides,
        }
        runs.append(entry)
        write_json(path, ledger)
    return index, destination, entry


def finish_ledger(root, index, report):
    with FileLock(root / "ledger.lock"):
        path = root / "ledger.json"
        ledger = json.loads(path.read_text())
        entry = next(item for item in ledger["full_runs"] if item["run"] == index)
        entry.update(
            status="passed" if report.get("passed") else "failed",
            finished_at=report["finished_at"],
            job_id=report.get("job_id"),
            elapsed_seconds=report.get("elapsed_seconds"),
            calls=report.get("calls"),
            job_status=report.get("status"),
            target_met=report.get("passed", False),
        )
        write_json(path, ledger)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC).timestamp() if parsed.tzinfo is None else parsed.timestamp()


def distribution(values):
    values = sorted(values)
    return {
        "count": len(values),
        "total": round(sum(values), 3),
        "median": round(statistics.median(values), 3) if values else None,
        "p95": round(values[max(0, math.ceil(len(values) * 0.95) - 1)], 3) if values else None,
        "maximum": max(values) if values else None,
    }


def summarize_events(events, *, created_at, updated_at, stopped_at=None):
    """Old event intervals include slot waiting. Only explicit usage separates service."""
    start, end = timestamp(created_at), timestamp(updated_at)
    end = max(
        start,
        end,
        timestamp(stopped_at) if stopped_at else start,
        max((timestamp(row["created_at"]) for row in events), default=start),
    )
    active_stage, stage_start = "queued", start
    stage_seconds, starts, intervals, actions = defaultdict(float), {}, [], defaultdict(list)
    business_starts, business, timeline = {}, [], []
    failures = Counter()
    for row in events:
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        kind, now = row["kind"], timestamp(row["created_at"])
        if kind == "progress":
            if data.get("stage") and data["stage"] != active_stage:
                stage_seconds[active_stage] += now - stage_start
                active_stage, stage_start = data["stage"], now
            timeline.append(
                {
                    "at": row["created_at"],
                    "stage": data.get("stage", active_stage),
                    "status": data.get("status"),
                }
            )
        if kind == "task_started":
            starts[data["key"]] = {
                "key": data["key"],
                "stage": active_stage,
                "start": now,
                "attempt": data.get("attempt", 1),
            }
        elif kind in {"task_completed", "task_failed"}:
            pending = starts.pop(data["key"], None)
            if pending is not None:
                pending.update(
                    end=now,
                    elapsed_seconds=now - pending["start"],
                    failed=kind == "task_failed",
                    usage=data.get("usage") or {},
                )
                intervals.append(pending)
                actions[data["key"].split(":")[0]].append(pending)
                if pending["failed"]:
                    # Error strings can contain provider URLs, so retain only their type.
                    failures[(data.get("error") or "Unknown").split(":", 1)[0][:100]] += 1
        if kind == "work_update":
            text = data.get("text", "")
            opening = re.match(r"开始业务任务“(.+?)”", text)
            closing = re.match(r"业务任务“(.+?)”整体检查完成", text)
            if opening:
                business_starts[opening[1]] = now
            if closing and closing[1] in business_starts:
                begun = business_starts.pop(closing[1])
                business.append(
                    {
                        "title": closing[1],
                        "start_offset_seconds": begun - start,
                        "elapsed_seconds": now - begun,
                        "completed": True,
                    }
                )
    stage_seconds[active_stage] += max(0, end - stage_start)
    for title, begun in business_starts.items():
        business.append(
            {
                "title": title,
                "start_offset_seconds": begun - start,
                "elapsed_seconds": end - begun,
                "completed": False,
            }
        )
    by_action = {}
    for action, records in actions.items():
        queue = [
            r["usage"]["queue_ms"]
            for r in records
            if isinstance(r["usage"].get("queue_ms"), (int, float))
        ]
        service = [
            r["usage"]["service_ms"]
            for r in records
            if isinstance(r["usage"].get("service_ms"), (int, float))
        ]
        by_action[action] = {
            "task_elapsed_seconds": distribution([r["elapsed_seconds"] for r in records]),
            "queue_ms": distribution(queue),
            "service_ms": distribution(service),
            "failed_attempts": sum(r["failed"] for r in records),
            "prompt_tokens": sum(r["usage"].get("prompt_tokens", 0) for r in records),
            "completion_tokens": sum(r["usage"].get("completion_tokens", 0) for r in records),
        }
    points = sorted(
        (at, delta)
        for r in intervals
        for at, delta in [(r["start"], 1), (r["end"], -1)]
        if r["end"] > r["start"]
    )
    running = peak = 0
    for _, delta in points:
        running += delta
        peak = max(peak, running)
    return {
        "observed_duration_seconds": round(end - start, 3),
        "event_precision": "seconds; task_elapsed includes queueing, service and validation; sums overlap",
        "peak_started_unfinished_tasks": peak,
        "http_concurrency_inferred": False,
        "stage_seconds": {k: round(v, 3) for k, v in stage_seconds.items()},
        "stage_timeline": timeline,
        "actions": by_action,
        "started_attempts": sum(row["kind"] == "task_started" for row in events),
        "unfinished_attempts": dict(Counter(key.split(":")[0] for key in starts)),
        "failure_types": dict(failures),
        "business_tasks": sorted(business, key=lambda item: -item["elapsed_seconds"]),
    }


def summarize_providers(events):
    """Separate engine tasks from successful provider requests reported in usage."""
    logical, requests = Counter(), Counter()
    missing_decision_usage = 0
    for event in events:
        if event["kind"] != "task_completed":
            continue
        usage = json.loads(event["data"]).get("usage") or {}
        provider = usage.get("selection_provider") or (
            "decision" if usage.get("decision_provider") else "llm"
        )
        logical[provider] += 1
        if provider == "decision":
            recorded = usage.get("decision_calls")
            if isinstance(recorded, int) and recorded >= 0:
                requests[provider] += recorded
            else:
                missing_decision_usage += 1
        else:
            pages = usage.get("selection_page_usage")
            requests[provider] += len(pages) if isinstance(pages, list) else 1
    return {
        "completed_logical_calls_by_provider": dict(logical),
        "recorded_successful_provider_requests": dict(requests),
        "decision_tasks_missing_request_usage": missing_decision_usage,
        "provider_request_count_scope": (
            "Successful requests recorded by completed tasks; failed requests and unreported "
            "internal retries are not included. This is not a complete HTTP request count."
        ),
    }


def mark_interrupted_job(store, job_id, exception_type):
    """Only the isolated replay job changes; keep completed output and source jobs intact."""
    if store is not None and job_id is not None:
        job = store.get(job_id)
        if job["status"] in {"queued", "running"}:
            store.update(job_id, status="failed", error=f"Benchmark interrupted ({exception_type})")


def content_quality(snapshot):
    issues = snapshot.get("blocking_issues", [])
    allowed_cover = {"cover:business_name", "cover:requirement_name"}
    blockers = [f["key"] for f in issues if f["kind"] != "pending"]
    manual = [f["key"] for f in issues if f["kind"] == "pending" and f["key"] not in allowed_cover]
    return {
        "unresolved_non_pending_keys": blockers,
        "allowed_cover_pending_keys": [
            f["key"] for f in issues if f["kind"] == "pending" and f["key"] in allowed_cover
        ],
        "manual_quality_review_keys": manual,
        "requires_manual_quality_review": bool(manual),
        "automatic_content_quality_pass": bool(
            not blockers and not manual and (snapshot.get("review_passed") or issues)
        ),
    }


def embedded_png_hashes(archive, document):
    """Count actual body image references, not merely unused ZIP media entries."""
    rels_path = "word/_rels/document.xml.rels"
    if rels_path not in archive.namelist():
        return Counter()
    rels = ElementTree.fromstring(archive.read(rels_path))
    by_id = {}
    for relation in rels:
        if relation.get("TargetMode") == "External" or not relation.get("Type", "").endswith(
            "/image"
        ):
            continue
        target = relation.get("Target", "")
        path = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else "word/" + target
        )
        if path.startswith("word/media/") and path in archive.namelist():
            data = archive.read(path)
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                by_id[relation.get("Id")] = hashlib.sha256(data).hexdigest()
    drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    reference = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    return Counter(
        by_id[node.get(reference)]
        for node in document.iter(drawing)
        if node.get(reference) in by_id
    )


def formatting_summary(folder, work_plan):
    try:
        report = json.loads((folder / "formatting.json").read_text())
    except (OSError, ValueError):
        return {"status": "missing_or_invalid", "acceptable": False}
    groups = report.get("groups", [])
    expected = {task.task_id for task in work_plan.tasks} if work_plan else set()
    actual = [group.get("task_id") for group in groups]
    started = bool(expected) and len(actual) == len(expected) and set(actual) == expected
    started = started and all(group.get("status") != "not_started" for group in groups)
    acceptable = report.get("status") in {"complete", "partial", "replayed"} and started
    for group in groups:
        status, calls = group.get("status"), group.get("calls", 0)
        acceptable = acceptable and 0 <= calls <= 3 and not group.get("oversized_targets")
        acceptable = acceptable and (
            status in {"complete", "unchanged"}
            or (status in {"iteration_limit", "partial"} and calls == 3)
        )
    return {
        "status": report.get("status"),
        "groups": len(groups),
        "group_statuses": dict(Counter(group.get("status", "unknown") for group in groups)),
        "all_business_groups_started": started,
        "remaining_targets": sum(group.get("remaining_targets", 0) for group in groups),
        "calls": sum(group.get("calls", 0) for group in groups),
        "budget_limited": any(
            group.get("status") in {"iteration_limit", "partial"} for group in groups
        ),
        "fully_formatted": bool(
            acceptable
            and all(
                group.get("status") in {"complete", "unchanged"}
                and not group.get("remaining_targets")
                for group in groups
            )
        ),
        "acceptable": bool(acceptable),
    }


def inspect_artifact(store, job_id, source_plan, *, expected_sections=EXPECTED_SECTIONS):
    from folio.visuals.workflow import load_visuals

    job, states = store.get(job_id), store.states(job_id)
    snapshot = job["snapshot"] or {}
    expected = {s["section_id"] for s in source_plan["sections"]}
    artifact = Path(job["artifact"]) if job["artifact"] else None
    issues = snapshot.get("blocking_issues", [])
    technical = [
        f["key"]
        for f in issues
        if f["key"].startswith(
            ("review-incomplete:", "adjudication:", "repair-no-progress:", "visual:")
        )
    ]
    from folio.orchestration.visual_obligations import missing_visual_obligations

    missing_visuals = missing_visual_obligations(
        source_plan["sections"],
        [s.model_dump() for s in states],
        user_request=(job.get("input") or {}).get("user_request", ""),
    )
    check = {
        "explicit_visual_requirements_satisfied": not missing_visuals,
        "missing_required_visuals": [
            {"section_id": v.section_id, "kind": v.kind} for v in missing_visuals
        ],
        "terminal_export": job["status"] in {"published", "draft", "needs_confirmation"}
        and job["stage"] == "complete",
        "all_53_sections_saved": len(expected) == EXPECTED_SECTIONS
        and {s.section_id for s in states} == expected,
        "all_expected_sections_saved": len(expected) == expected_sections
        and {s.section_id for s in states} == expected,
        "valid_docx": False,
        "heading_bookmarks_complete": False,
        "trace_matches_saved_states": False,
        "internal_marker_count": None,
        "technical_blocker_keys": technical,
        "call_budget_exhausted": bool(snapshot.get("call_budget_exhausted")),
        "unresolved_visuals": bool(snapshot.get("visual_issues")),
        "requested_figures": sum(len(s.content.visuals) for s in states),
        "visual_assets_complete": False,
        "visual_pngs_embedded": False,
        "formatting": {"status": "not_exported", "acceptable": False},
        "review_passed": snapshot.get("review_passed", False),
        "blocking_kinds": dict(Counter(f["kind"] for f in issues)),
        "table_contract_errors": [
            node["section_id"]
            for node in source_plan["sections"]
            if node.get("expected_table_headers")
            and not any(
                s.section_id == node["section_id"]
                and any(
                    b.kind == "table" and b.rows[0] == node["expected_table_headers"]
                    for b in s.content.blocks
                )
                for s in states
            )
        ],
    }
    check.update(content_quality(snapshot))
    if artifact and artifact.is_file():
        with zipfile.ZipFile(artifact) as archive:
            check["valid_docx"] = (
                archive.testzip() is None and "word/document.xml" in archive.namelist()
            )
            xml = ElementTree.fromstring(archive.read("word/document.xml"))
            text = "\n".join(t.text or "" for t in xml.iter(WORD + "t"))
            check["internal_marker_count"] = len(
                re.findall(
                    r"\bev_[a-f0-9]{8,}\b|\bsfref\d+\b|\bmodel_proposal\b|【模型建议】", text
                )
            )
            marks = Counter(node.get(WORD + "name") for node in xml.iter(WORD + "bookmarkStart"))
            check["heading_bookmarks_complete"] = all(marks[sid] == 1 for sid in expected)
            embedded = embedded_png_hashes(archive, xml)
        trace = artifact.with_name("trace.json")
        if trace.exists():
            check["trace_matches_saved_states"] = {
                s.section_id: s.model_dump() for s in states
            } == {s["section_id"]: s for s in json.loads(trace.read_text())}
        try:
            visuals = load_visuals(artifact.parent, states)
            check["visual_assets_complete"] = not visuals.issues
            requested = Counter(
                file_checksum(item["image_path"])
                for items in visuals.values()
                for item in items
                if item.get("status") != "unresolved"
            )
            check["visual_pngs_embedded"] = not visuals.issues and all(
                embedded[signature] >= count for signature, count in requested.items()
            )
        except (OSError, ValueError, KeyError):
            check["visual_assets_complete"] = False
        check["formatting"] = formatting_summary(artifact.parent, store.work_plan(job_id))
    check["artifact"] = str(artifact) if artifact else None
    check["complete_delivery"] = bool(
        check["terminal_export"]
        and check["all_expected_sections_saved"]
        and check["valid_docx"]
        and check["heading_bookmarks_complete"]
        and check["trace_matches_saved_states"]
        and check["internal_marker_count"] == 0
        and not check["table_contract_errors"]
        and not technical
        and not check["call_budget_exhausted"]
        and not check["unresolved_visuals"]
        and check["visual_assets_complete"]
        and check["visual_pngs_embedded"]
        and check["formatting"]["acceptable"]
        and check["explicit_visual_requirements_satisfied"]
        and check["automatic_content_quality_pass"]
    )
    return check


async def run(args):
    source = read_source(args.root, args.source_job)
    request = JobInput.model_validate(source["input"])
    plan = DocumentPlan.model_validate(source["plan"])
    if request.runtime != "llm" or len(plan.sections) != EXPECTED_SECTIONS:
        raise ValueError("This benchmark requires the real LLM and the confirmed 53-section plan")
    config = resolve_model_config(args.root)
    signature = source_signature(source, config)
    overrides = {
        "run_timeout_seconds": {"from": request.limits.run_timeout_seconds, "to": args.timeout}
    }
    request.limits.run_timeout_seconds = args.timeout
    if args.max_parallel_tasks is not None:
        overrides["max_parallel_tasks"] = {
            "from": request.limits.max_parallel_tasks,
            "to": args.max_parallel_tasks,
        }
        request.limits.max_parallel_tasks = args.max_parallel_tasks
    request.document_id = "optimization_" + uuid.uuid4().hex
    index, output, reservation = reserve_run(
        args.output_root, args.source_job, args.round, signature, overrides
    )
    report = {
        **reservation,
        "source_job": args.source_job,
        "source_signature": signature,
        "code_sha256": code_digest(),
        "target_seconds": TARGET_SECONDS,
        "deadline_seconds": args.timeout,
        "cached_results_imported": False,
        "passed": False,
    }
    result_path = output / "result.json"
    write_json(result_path, report)
    store = job_id = None
    started = time.monotonic()
    try:
        store = Store(output / "data")
        job_id = store.create(request, plan=plan)
        report["job_id"] = job_id
        report["independent_store"] = str(store.root)
        write_json(result_path, report)
        print(
            json.dumps(
                {
                    "event": "run_started",
                    "run": index,
                    "round": args.round,
                    "job_id": job_id,
                    "result": str(result_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        # Engine uses the exact deadline in the recorded override. The outer
        # watchdog leaves cancellation/cleanup a short grace period, not more work.
        await asyncio.wait_for(execute(store, job_id, config), timeout=args.timeout + 15)
    except BaseException as exc:
        report["exception_type"] = type(exc).__name__
        report["exception_code"] = getattr(exc, "code", None)
        report["exception_sha256"] = hashlib.sha256(str(exc).encode()).hexdigest()
        report["interrupted"] = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        try:
            mark_interrupted_job(store, job_id, type(exc).__name__)
        except Exception as update_error:
            report["interruption_status_error_type"] = type(update_error).__name__
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at"] = utc_now()
        report["within_target"] = report["elapsed_seconds"] <= TARGET_SECONDS
        try:
            report["code_unchanged"] = report["code_sha256"] == code_digest()
            report["input_configuration_unchanged"] = signature == source_signature(
                read_source(args.root, args.source_job), config
            )
            if store is not None and job_id is not None:
                job = store.get(job_id)
                report.update(
                    status=job["status"],
                    stage=job["stage"],
                    calls=job["calls"],
                    saved_nodes=len(store.states(job_id)),
                )
                report.update(inspect_artifact(store, job_id, source["plan"]))
                with store.connect() as conn:
                    events = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT kind,data,created_at FROM events WHERE job_id=? ORDER BY id",
                            (job_id,),
                        )
                    ]
                    tasks = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT task_key,attempts,usage FROM tasks WHERE job_id=?", (job_id,)
                        )
                    ]
                timing = summarize_events(
                    events,
                    created_at=job["created_at"],
                    updated_at=job["updated_at"],
                    stopped_at=report["finished_at"],
                )
                write_json(output / "timing.json", timing)
                report["timing"] = str(output / "timing.json")
                report["requests_by_action"] = dict(
                    Counter(row["task_key"].split(":")[0] for row in tasks)
                )
                report["attempts_by_action"] = dict(
                    Counter(
                        json.loads(row["data"])["key"].split(":")[0]
                        for row in events
                        if row["kind"] == "task_started"
                    )
                )
                report.update(summarize_providers(events))
                report["retry_count"] = sum(max(0, row["attempts"] - 1) for row in tasks)
                report["stage_seconds"] = timing["stage_seconds"]
                report["unfinished_attempts"] = timing["unfinished_attempts"]
                # Concise task/stage audit without material text, raw errors or configuration.
                with (output / "events.jsonl").open("w", encoding="utf-8") as log:
                    for event in events:
                        data = json.loads(event["data"])
                        if event["kind"] in {
                            "progress",
                            "task_started",
                            "task_completed",
                            "task_failed",
                        }:
                            safe = {
                                k: data[k]
                                for k in ("stage", "status", "key", "attempt")
                                if k in data
                            }
                            usage = data.get("usage") or {}
                            safe.update(
                                {
                                    k: usage[k]
                                    for k in (
                                        "queue_ms",
                                        "service_ms",
                                        "prompt_tokens",
                                        "completion_tokens",
                                        "decision_calls",
                                        "selection_provider",
                                        "selection_batches",
                                        "selection_attempts",
                                        "decision_provider",
                                    )
                                    if k in usage
                                }
                            )
                            log.write(
                                json.dumps(
                                    {"at": event["created_at"], "event": event["kind"], **safe},
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                report["operational_pass"] = bool(
                    report["complete_delivery"]
                    and report["code_unchanged"]
                    and report["input_configuration_unchanged"]
                    and not report["unfinished_attempts"]
                    and "exception_type" not in report
                )
                report["passed"] = report["operational_pass"] and report["within_target"]
        except BaseException as exc:
            report["verification_exception_type"] = type(exc).__name__
            report["passed"] = False
        write_json(result_path, report)
        finish_ledger(args.output_root, index, report)
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in (
                        "run",
                        "job_id",
                        "elapsed_seconds",
                        "status",
                        "stage",
                        "calls",
                        "saved_nodes",
                        "complete_delivery",
                        "passed",
                        "exception_type",
                        "verification_exception_type",
                        "result",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_job")
    parser.add_argument("--root", type=Path, default=Path(".folio"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-parallel-tasks", type=int, choices=range(1, 17))
    args = parser.parse_args()
    if args.timeout < TARGET_SECONDS:
        parser.error("The full-run deadline must allow the complete 420-second target")
    try:
        passed = asyncio.run(run(args))
    except Exception as exc:
        # Configuration validation may include secrets in its exception text.
        # Reports contain hashes/types; never emit a raw configuration traceback.
        print(
            json.dumps({"event": "benchmark_harness_failed", "error_type": type(exc).__name__}),
            flush=True,
        )
        raise SystemExit(2) from None
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
