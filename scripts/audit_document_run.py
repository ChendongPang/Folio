#!/usr/bin/env python3
"""Read-only, offline audit of one document job; never calls a provider.

Reports contain business material and must remain under an ignored .folio
directory. Recorded provider operations, engine attempts, and query batches are
different units; old logs never acquire invented HTTP counts or timings.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "headers",
    "request_headers",
    "response_headers",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "llm_api_key",
    "tavily_api_key",
    "jev_api_key",
    "endpoint",
    "model_config",
    "configuration",
    "ragflow",
    "input",
}
REVIEW_ACTIONS = {
    "validate",
    "review",
    "review_work",
    "drill_down",
    "repair",
    "validate_visual",
    "check_supplement_conflicts",
    "repair_supplement_conflicts",
    "integrate_supplement",
    "format_document",
    "read_review_materials",
    "dependency_audit",
}


def parse_json(value, default=None):
    if not isinstance(value, str):
        return default if value is None else value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def error_code(value):
    """Do not copy exception messages, upstream response bodies, or URL secrets."""
    if not value:
        return None
    matched = re.match(r"([A-Za-z_][A-Za-z0-9_.]*)(?:\s+\[([a-zA-Z0-9_]+)\])?", str(value))
    return " ".join(part for part in matched.groups() if part) if matched else "error"


def safe_text(value):
    value = re.sub(
        r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{16,}|\bapikey_[A-Za-z0-9_]{20,}", "[REDACTED]", value
    )
    if value.startswith(("http://", "https://")) and "\n" not in value:
        try:
            url = urlsplit(value)
            host = url.hostname or ""
            if url.port:
                host += ":" + str(url.port)
            query = [
                (k, "[REDACTED]" if k.lower() in SECRET_KEYS | {"key", "token"} else v)
                for k, v in parse_qsl(url.query, keep_blank_values=True)
            ]
            value = urlunsplit((url.scheme, host, url.path, urlencode(query), url.fragment))
        except ValueError:
            return "[INVALID URL]"
    return value


def clean(value):
    if isinstance(value, dict):
        return {
            str(k): error_code(v)
            if str(k).lower() in {"error", "raw_error", "exception"}
            else clean(v)
            for k, v in value.items()
            if str(k).lower() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    return safe_text(value) if isinstance(value, str) else value


def read_snapshot(root, job_id):
    root = Path(root).resolve()
    db = root / "folio.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # Consistent read snapshot, no schema migration or Store constructor.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        raw_job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw_job is None:
            raise ValueError("Job does not exist")
        job = dict(raw_job)
        job.pop("input", None)
        job["error"] = error_code(job.get("error"))
        job["plan"] = parse_json(job.get("plan"), {})
        job["snapshot"] = parse_json(job.get("snapshot"), {})
        snapshot = {"job": clean(job), "root": str(root)}
        for table in (
            "events",
            "tasks",
            "work_plans",
            "work_states",
            "sections",
            "retrievals",
            "decisions",
        ):
            rows = (
                conn.execute(f"SELECT * FROM {table} WHERE job_id=?", (job_id,))
                if table in tables
                else []
            )
            snapshot[table] = [dict(r) for r in rows]
    for row in snapshot["events"]:
        row["data"] = clean(parse_json(row["data"], {}))
    snapshot["events"].sort(key=lambda r: r["id"])
    for row in snapshot["tasks"]:
        for key in ("result", "usage"):
            row[key] = clean(parse_json(row.get(key), None if key == "result" else {}))
        row["error"] = error_code(row.get("error"))
    for table, field in (
        ("work_plans", "plan"),
        ("work_states", "state"),
        ("sections", "state"),
        ("retrievals", "evidence"),
    ):
        for row in snapshot[table]:
            row[field] = clean(parse_json(row[field], {} if field != "evidence" else []))
    for row in snapshot["decisions"]:
        for key in ("context", "result", "metadata"):
            row[key] = clean(parse_json(row.get(key), {}))
    return snapshot


def stage_timing(job, events):
    start = timestamp(job["created_at"])
    points = [timestamp(job.get("updated_at")) or start, start]
    for event in events:
        points.append(timestamp(event["created_at"]))
        points.extend(
            timestamp(event["data"][k])
            for k in ("started_at", "finished_at")
            if event["data"].get(k)
        )
    end = max(points)
    intervals, stage, since = [], "queued", start
    for event in events:
        value = event["data"]
        if event["kind"] != "progress" or not value.get("stage") or value["stage"] == stage:
            continue
        now = max(since, timestamp(event["created_at"]))
        intervals.append(
            {
                "stage": stage,
                "started_at": iso(since),
                "finished_at": iso(now),
                "wall_seconds": round(now - since, 3),
            }
        )
        stage, since = value["stage"], now
    intervals.append(
        {
            "stage": stage,
            "started_at": iso(since),
            "finished_at": iso(end),
            "wall_seconds": round(end - since, 3),
        }
    )
    totals = defaultdict(float)
    for row in intervals:
        totals[row["stage"]] += row["wall_seconds"]
    return {
        "started_at": iso(start),
        "observed_through": iso(end),
        "wall_seconds": round(end - start, 3),
        "intervals": intervals,
        "totals": [{"stage": k, "wall_seconds": round(v, 3)} for k, v in totals.items()],
        "precision": "阶段边界来自 SQLite 事件，通常精确到秒；provider duration_ms 为单次 I/O 计时。",
    }


def engine_attempts(events):
    rows, active = [], {}
    for event in events:
        data, kind = event["data"], event["kind"]
        key = data.get("task_key", data.get("key"))
        if kind == "task_started":
            row = {
                "task_key": key,
                "task": key.split(":", 1)[0],
                "attempt": data.get("attempt", 1),
                "started_at": event["created_at"],
                "finished_at": None,
                "duration_ms": None,
                "outcome": "unfinished",
                "queue_ms": None,
                "service_ms": None,
                "context_ref": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
            }
            rows.append(row)
            active[key] = row
        elif kind == "request_prepared" and key in active:
            active[key]["context_ref"] = data.get("context_ref")
        elif kind in {"task_completed", "task_failed"}:
            row = active.pop(key, None)
            if row is None:
                row = {
                    "task_key": key,
                    "task": key.split(":", 1)[0],
                    "attempt": None,
                    "started_at": None,
                }
                rows.append(row)
            usage = data.get("usage") or {}
            row.update(
                finished_at=event["created_at"],
                outcome="success" if kind == "task_completed" else "failed",
                error_code=error_code(data.get("error")),
                duration_ms=round(
                    (timestamp(event["created_at"]) - timestamp(row["started_at"])) * 1000, 3
                )
                if row["started_at"]
                else None,
            )
            for name in (
                "queue_ms",
                "service_ms",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ):
                row[name] = usage.get(name)
            row["measured_queue_and_service_ms"] = (
                usage["queue_ms"] + usage["service_ms"]
                if usage.get("queue_ms") is not None and usage.get("service_ms") is not None
                else None
            )
    return rows


def paired_events(events, prefix, identity):
    rows = {}
    for event in events:
        if event["kind"] not in {prefix + "_started", prefix + "_completed"}:
            continue
        data = event["data"]
        key = data[identity]
        row = rows.setdefault(
            key, {identity: key, "outcome": "unfinished", "finished_at": None, "duration_ms": None}
        )
        row.update(data)
        row.setdefault("started_at", event["created_at"])
        row["start_event_seen"] = row.get("start_event_seen", False) or event["kind"].endswith(
            "_started"
        )
        row["completion_event_seen"] = row.get("completion_event_seen", False) or event[
            "kind"
        ].endswith("_completed")
    return list(rows.values())


def provider_summary(interactions):
    groups = defaultdict(list)
    for row in interactions:
        row["interaction_class"] = {
            "llm_auxiliary": "auxiliary_http_not_llm_generation",
            "decision": "decision_model",
            "tavily": "public_web_search",
            "ragflow": "knowledge_tool",
        }.get(
            row.get("provider"),
            "llm_chat"
            if row.get("provider") == "llm" and row.get("operation") == "chat.completions"
            else "other",
        )
        row["count_unit"] = (
            "MCP tool invocation" if row.get("provider") == "ragflow" else "HTTP operation"
        )
        groups[(row.get("provider"), row.get("model"), row.get("operation"))].append(row)
    summaries = []
    for (provider, model, operation), rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        internal_groups = Counter((r.get("task_key"), r.get("attempt", 1)) for r in rows)
        summaries.append(
            {
                "provider": provider,
                "model": model,
                "operation": operation,
                "count_unit": rows[0]["count_unit"],
                "interaction_class": rows[0]["interaction_class"],
                "recorded_interactions": len(rows),
                "success": sum(r["outcome"] == "success" for r in rows),
                "failed": sum(r["outcome"] == "failed" for r in rows),
                "cancelled": sum(r["outcome"] == "cancelled" for r in rows),
                "unfinished": sum(not r["completion_event_seen"] for r in rows),
                "additional_io_within_same_engine_attempt": sum(
                    max(0, n - 1) for n in internal_groups.values()
                ),
                "io_on_engine_retry_attempt": sum((r.get("attempt") or 1) > 1 for r in rows),
                "aggregate_io_seconds": round(
                    sum(r.get("duration_ms") or 0 for r in rows) / 1000, 3
                ),
                "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
                "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
                "tokens_measured_interactions": sum(
                    r.get("total_tokens") is not None for r in rows
                ),
            }
        )
    return summaries


def request_details(root, job_id, events, tasks):
    attempts = defaultdict(list)
    for event in events:
        if event["kind"] == "request_prepared":
            attempts[event["data"].get("context_ref")].append(event["data"].get("attempt"))
    rows = []
    directory = Path(root) / "jobs" / job_id / "contexts" / "requests"
    for path in sorted(directory.glob("*.json")):
        if not path.resolve().is_relative_to(directory.resolve()):
            continue
        value = parse_json(path.read_text(encoding="utf-8"), {})
        context = value.get("context", {})
        task_key = value.get("task_key")
        inputs = {
            key: clean(context[key])
            for key in (
                "work_task",
                "section",
                "finding",
                "findings",
                "gaps",
                "missing_information",
                "previous_queries",
                "topic",
                "objective",
                "purpose",
                "question",
                "questions",
                "issue",
                "instructions",
                "repair_instruction",
                "research_instruction",
                "semantic_cases",
                "review_contract",
                "figure_policy",
                "required_visual_kinds",
                "selected_evidence_ids",
                "material_selection",
                "source_applicability_contract",
            )
            if key in context
        }
        section_ids = set()
        for key in ("sections", "definitions", "task_sections", "peer_sections"):
            for section in context.get(key, []):
                if isinstance(section, dict) and section.get("section_id"):
                    section_ids.add(section["section_id"])
        if context.get("section", {}).get("section_id"):
            section_ids.add(context["section"]["section_id"])
        refs = set()

        def evidence_refs(item, refs=refs):
            if isinstance(item, dict):
                if isinstance(item.get("evidence_id"), str):
                    refs.add(item["evidence_id"])
                refs.update(x for x in item.get("evidence_ids", []) if isinstance(x, str))
                for child in item.values():
                    evidence_refs(child)
            elif isinstance(item, list):
                for child in item:
                    evidence_refs(child)

        evidence_refs(context)
        rows.append(
            {
                "context_ref": path.stem,
                "context_path": str(path.resolve()),
                "task": value.get("task"),
                "task_key": task_key,
                "attempts": attempts.get(path.stem, []),
                "work_task_id": context.get("work_task", {}).get("task_id"),
                "section_ids": sorted(section_ids),
                "evidence_ids_in_request": sorted(refs),
                "request_character_count": len(json.dumps(context, ensure_ascii=False)),
                "capacity": clean(value.get("capacity", {})),
                "input_details": inputs,
                "stored_result": tasks.get(task_key, {}).get("result"),
                "result_scope": "task checkpoint final accepted result; earlier failed replies are not reconstructed",
            }
        )
    return rows


def material_catalog(snapshot):
    materials = {}
    for table, field in (("work_states", "state"), ("sections", "state")):
        for row in snapshot[table]:
            for evidence in row[field].get("evidence", []):
                materials.setdefault(evidence["evidence_id"], evidence)
    for retrieval in snapshot["retrievals"]:
        for evidence in retrieval["evidence"]:
            materials.setdefault(evidence["evidence_id"], evidence)
    result = {}
    for eid, evidence in materials.items():
        metadata = evidence.get("metadata", {})
        source = evidence.get("source_id", "")
        kind = (
            "public_web"
            if metadata.get("role") == "public_web"
            else "knowledge_base"
            if source.startswith("ragflow:")
            else "local_material"
        )
        result[eid] = {
            "evidence_id": eid,
            "source_kind": kind,
            "source_id": source,
            "document_name": metadata.get("document_name")
            or metadata.get("title")
            or metadata.get("document_metadata", {}).get("name")
            or source,
            "url": metadata.get("url")
            or (source if source.startswith(("http://", "https://")) else None),
            "locator": evidence.get("locator"),
            "source_version": evidence.get("source_version"),
            "retrieved_at": evidence.get("retrieved_at"),
            "text": evidence.get("text", ""),
            "project_fact": metadata.get("project_fact"),
            "referenced_by": [],
        }
    return result


def body_blocks(snapshot, materials):
    definitions = {s["section_id"]: s for s in snapshot["job"].get("plan", {}).get("sections", [])}
    order = {sid: i for i, sid in enumerate(definitions)}
    result = []
    for row in sorted(snapshot["sections"], key=lambda r: order.get(r["section_id"], 10**9)):
        state, sid = row["state"], row["section_id"]
        definition = definitions.get(sid, {})
        for block in state.get("content", {}).get("blocks", []):
            refs = block.get("evidence_ids", [])
            found = [materials[eid] for eid in refs if eid in materials]
            kinds = sorted({e["source_kind"] for e in found})
            origin = block.get("origin", "evidence")
            basis = (
                "模型补写；参考引用不构成业务已核实证明"
                if origin == "model_proposal"
                else "引用知识库或用户资料"
            )
            if origin != "model_proposal":
                if not refs:
                    basis = "标题" if block.get("kind") == "heading" else "标记有据但没有引用"
                elif kinds == ["public_web"]:
                    basis = "仅引用公开网页；不能据此确认项目已采用"
                elif "public_web" in kinds:
                    basis = "同时引用公开网页与内部资料"
            item = {
                "section_id": sid,
                "section_number": definition.get("display_number"),
                "section_title": definition.get("title"),
                "revision": state.get("revision"),
                **block,
                "basis_description": basis,
                "source_kinds": kinds,
                "missing_evidence_ids": [eid for eid in refs if eid not in materials],
                "source_documents": [
                    {
                        k: e[k]
                        for k in ("evidence_id", "source_kind", "document_name", "url", "locator")
                    }
                    for e in found
                ],
            }
            result.append(item)
            for evidence in found:
                evidence["referenced_by"].append(
                    {
                        "section_id": sid,
                        "section_title": definition.get("title"),
                        "block_id": block.get("block_id"),
                        "origin": origin,
                    }
                )
    return result


def retrieval_queries(snapshot, materials):
    batches = paired_events(snapshot["events"], "retrieval", "retrieval_id")
    for batch in batches:
        batch["measurement"] = "recorded_retrieval_batch"
    cache_events = [e for e in snapshot["events"] if e["kind"] == "retrieval_cache_hit"]
    for event in cache_events:
        batches.append(
            event["data"]
            | {
                "retrieval_id": "cache:" + str(event["id"]),
                "outcome": "cache_hit",
                "started_at": event["created_at"],
                "duration_ms": 0,
                "measurement": "cache_hit_no_provider_request",
            }
        )
    if not batches:
        # Reconstruct the historical cache key, not the historical request timing.
        cached = {r["retrieval_key"]: r["evidence"] for r in snapshot["retrievals"]}
        for item in snapshot["work_states"]:
            work, tid = item["state"], item["task_id"]
            for index, queries in enumerate(work.get("searches", []), 1):
                wire = json.dumps({"queries": queries}, ensure_ascii=False, separators=(",", ":"))
                key = hashlib.sha256((tid + ":" + wire).encode()).hexdigest()[:24]
                batches.append(
                    {
                        "retrieval_id": f"legacy:{tid}:kb:{index}",
                        "work_task_id": tid,
                        "queries": queries,
                        "source_kind": "knowledge_base",
                        "outcome": "checkpoint_recorded",
                        "duration_ms": None,
                        "started_at": None,
                        "finished_at": None,
                        "evidence_ids": [e["evidence_id"] for e in cached.get(key, [])],
                        "measurement": "legacy_checkpoint; timing, retries and cache reuse unknown",
                        "evidence_mapping_known": key in cached,
                    }
                )
            for index, search in enumerate(work.get("web_searches", []), 1):
                batches.append(
                    {
                        "retrieval_id": f"legacy:{tid}:web:{index}",
                        "work_task_id": tid,
                        "queries": [search.get("query", "")],
                        "source_kind": "public_web",
                        "outcome": search.get("status"),
                        "duration_ms": None,
                        "started_at": None,
                        "finished_at": None,
                        "evidence_ids": search.get("evidence_ids", []),
                        "error_code": search.get("error_code"),
                        "measurement": "legacy_checkpoint; timing and HTTP count unknown",
                    }
                )
    rows = []
    for batch in batches:
        query_list = batch.get("queries") or [""]
        for index, query in enumerate(query_list, 1):
            found = [materials[eid] for eid in batch.get("evidence_ids", []) if eid in materials]
            rows.append(
                {
                    **batch,
                    "query_index": index,
                    "query": query,
                    "questions_in_batch": len(query_list),
                    "timing_scope": "整个查询批次，不能把同批各问题重复累加",
                    "source_documents": [
                        {
                            k: e[k]
                            for k in (
                                "evidence_id",
                                "document_name",
                                "url",
                                "source_kind",
                                "referenced_by",
                            )
                        }
                        for e in found
                    ],
                }
            )
    return rows


def review_rows(events):
    rows = []
    for event in events:
        review = event["data"].get("review")
        if review:
            rows.append(
                {
                    "event_id": event["id"],
                    "at": event["created_at"],
                    "text": event["data"].get("text"),
                    **review,
                }
            )
    return rows


def review_call_details(requests, task_map):
    rows = [dict(r) for r in requests if r["task"] in REVIEW_ACTIONS]
    known = {row["task_key"] for row in rows}
    for key, task in task_map.items():
        action = key.split(":", 1)[0]
        if action in REVIEW_ACTIONS and key not in known:
            rows.append(
                {
                    "task": action,
                    "task_key": key,
                    "context_ref": None,
                    "attempts": [],
                    "section_ids": [],
                    "input_details": None,
                    "stored_result": task.get("result"),
                    "result_scope": "task checkpoint; request archive unavailable",
                }
            )
    for row in rows:
        result = row.get("stored_result")
        result = result if isinstance(result, dict) else {}
        # These are full model findings, often absent from abbreviated UI events.
        row["applicability_checks"] = result.get("applicability_checks")
        row["repair_targets"] = result.get("repair_targets")
    return rows


def detailed_archives(root, job_id):
    directory = Path(root) / "jobs" / job_id
    rows = []
    for group in (
        "review_plans",
        "review_results",
        "review_deduplication",
        "issue_read_results",
        "supplement_integrations",
        "rejected",
        "failed_outputs",
        "deferred_writing",
    ):
        for path in sorted((directory / "contexts" / group).glob("*.json")):
            if path.resolve().is_relative_to(directory.resolve()):
                rows.append(
                    {
                        "kind": group,
                        "archive_ref": path.stem,
                        "path": str(path.resolve()),
                        "data": clean(parse_json(path.read_text(encoding="utf-8"), {})),
                    }
                )
    for path in sorted((directory / "visuals").glob("*/*.json")):
        if path.stem.startswith(("review-", "semantic-review-")) or path.stem == "asset":
            if path.resolve().is_relative_to(directory.resolve()):
                rows.append(
                    {
                        "kind": "visual_" + path.stem,
                        "archive_ref": path.parent.name,
                        "path": str(path.resolve()),
                        "data": clean(parse_json(path.read_text(encoding="utf-8"), {})),
                    }
                )
    return rows


def build_report(root, job_id):
    snapshot = read_snapshot(root, job_id)
    events, job = snapshot["events"], snapshot["job"]
    timings = stage_timing(job, events)
    attempts = engine_attempts(events)
    interactions = paired_events(events, "provider", "interaction_id")
    attempt_lookup = {(row["task_key"], row.get("attempt")): row for row in attempts}
    for row in interactions:
        parent = attempt_lookup.get((row.get("task_key"), row.get("attempt")), {})
        row["engine_queue_ms"] = parent.get("queue_ms")
        row["engine_service_ms"] = parent.get("service_ms")
        row["parent_timing_scope"] = "共享的父引擎尝试计时；同尝试内重试不能重复累加"
    summaries = provider_summary(interactions)
    task_map = {row["task_key"]: row for row in snapshot["tasks"]}
    requests = request_details(root, job_id, events, task_map)
    materials = material_catalog(snapshot)
    blocks = body_blocks(snapshot, materials)
    queries = retrieval_queries(snapshot, materials)
    works = {row["task_id"]: row["state"] for row in snapshot["work_states"]}
    groups = []
    for saved in snapshot["work_plans"]:
        for task in saved["plan"].get("tasks", []):
            work = works.get(task["task_id"], {})
            starts = [
                e
                for e in events
                if e["kind"] == "work_update"
                and e["data"].get("text", "").startswith(f"开始业务任务“{task['title']}”")
            ]
            saved_states = [
                e
                for e in events
                if e["kind"] == "work_state_saved" and e["data"].get("task_id") == task["task_id"]
            ]
            reviewed = [e for e in saved_states if e["data"].get("reviewed")]
            groups.append(
                task
                | {
                    k: work.get(k)
                    for k in (
                        "researched",
                        "writable",
                        "research_instruction",
                        "record",
                        "missing",
                        "findings",
                    )
                }
                | {
                    "started_at": starts[0]["created_at"] if starts else None,
                    "first_reviewed_at": reviewed[0]["created_at"] if reviewed else None,
                    "last_state_saved_at": saved_states[-1]["created_at"] if saved_states else None,
                    "first_execution_wall_seconds": round(
                        timestamp(reviewed[0]["created_at"]) - timestamp(starts[0]["created_at"]), 3
                    )
                    if starts and reviewed
                    else None,
                }
            )
    actions = []
    cache_events = [
        e
        for e in events
        if e["kind"] in {"task_cache_hit", "model_cache_hit", "retrieval_cache_hit"}
    ]
    for task in sorted({r["task"] for r in attempts}):
        rows = [r for r in attempts if r["task"] == task]
        ios = [r for r in interactions if r.get("task") == task]
        actions.append(
            {
                "task": task,
                "engine_attempts": len(rows),
                "success": sum(r["outcome"] == "success" for r in rows),
                "failed": sum(r["outcome"] == "failed" for r in rows),
                "unfinished": sum(r["outcome"] == "unfinished" for r in rows),
                "engine_retry_attempts": sum((r.get("attempt") or 1) > 1 for r in rows),
                "recorded_provider_operations": len(ios) if interactions else None,
                "recorded_cache_hits": sum(e["data"].get("task") == task for e in cache_events)
                if interactions
                or any(e["kind"] == "request_prepared" for e in events)
                or cache_events
                else None,
                "aggregate_queue_seconds": round(
                    sum(r.get("queue_ms") or 0 for r in rows) / 1000, 3
                ),
                "aggregate_service_seconds": round(
                    sum(r.get("service_ms") or 0 for r in rows) / 1000, 3
                ),
                "timed_engine_attempts": sum(r.get("service_ms") is not None for r in rows),
                "token_measured_attempts": sum(r.get("total_tokens") is not None for r in rows),
                "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
                "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
            }
        )
    legacy_tasks = Counter()
    decision_metadata = {r["decision_key"]: r["metadata"] for r in snapshot["decisions"]}
    for row in snapshot["tasks"]:
        if row["result"] is None:
            continue
        usage = row.get("usage", {})
        provider = usage.get("decision_provider") or decision_metadata.get(row["task_key"], {}).get(
            "provider"
        )
        model = usage.get("decision_model") or decision_metadata.get(row["task_key"], {}).get(
            "model"
        )
        legacy_tasks[(provider or "unspecified", model or "unspecified")] += 1
    observations = Counter(e["kind"] for e in events)
    review = review_rows(events)
    proposals = [
        {"work_task_id": tid, **p} for tid, work in works.items() for p in work.get("proposals", [])
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "job": {
            k: job.get(k)
            for k in (
                "id",
                "status",
                "stage",
                "artifact",
                "calls",
                "created_at",
                "updated_at",
                "error",
            )
        },
        "summary": {
            "wall_seconds": timings["wall_seconds"],
            "work_groups": len(groups),
            "saved_sections": len(snapshot["sections"]),
            "confirmed_plan_sections": len(job.get("plan", {}).get("sections", [])),
            "body_blocks": len(blocks),
            "origin_counts": dict(Counter(b.get("origin") for b in blocks)),
            "recorded_engine_attempts": len(attempts),
            "recorded_provider_operations": len(interactions) if interactions else None,
            "recorded_llm_chat_requests": sum(
                r.get("interaction_class") == "llm_chat" for r in interactions
            )
            if interactions
            else None,
            "recorded_auxiliary_http_requests": sum(
                r.get("provider") == "llm_auxiliary" for r in interactions
            )
            if interactions
            else None,
            "retrieval_batches_in_report": len({q["retrieval_id"] for q in queries}),
            "retrieval_questions_in_report": len(queries),
            "unique_materials": len(materials),
            "public_web_materials": sum(
                e["source_kind"] == "public_web" for e in materials.values()
            ),
            "provider_logging_present": bool(interactions),
            "request_event_logging_present": bool(observations["request_prepared"]),
            "retrieval_logging_present": bool(observations["retrieval_started"]),
            "event_counts": dict(observations),
        },
        "limitations": [
            "正文均由 LLM 编排或生成。origin=evidence 表示记录了引用，不保证材料足以支持具体业务结论；origin=model_proposal 为模型补写。",
            "公开网页是外部参考资料，不自动等于本项目的业务要求、已采纳产品能力或已承诺性能指标。",
            "阶段墙钟按时间轴计算；并行请求的 service/queue 汇总不能相加作为任务总耗时。",
            "provider 记录统计实际已埋点操作；llm_auxiliary 的 tokenize/models 为辅助 HTTP，单独统计，不能算 LLM 生成次数；RAGFlow 是 MCP 工具调用数，不是连接、握手、心跳的 HTTP 总数；其他未埋点操作不计入。",
            "同引擎尝试内的额外 I/O 单列，避免把内部重试漏计。失败或未完成尝试仍保留；引擎 task_started 不等于 HTTP 请求。",
            "批次包含多个问题时，共享批次耗时和返回材料，不能声称每个问题具有独立耗时或独立检索结果。",
            "tasks 表只保存当前任务键的最终结果；请求已单独归档，但此前失败尝试的模型回复如未归档则未知。",
            "已有旧任务缺少 provider/retrieval/request 事件时，HTTP 次数、查询时长和缓存次数无法追溯；表内 null 表示未知，0 仅表示明确记录为零。",
        ],
        "timing": timings,
        "stage_intervals": timings["intervals"],
        "stage_totals": timings["totals"],
        "action_summary": actions,
        "provider_summary": summaries,
        "legacy_successful_task_provider_labels": [
            {
                "provider": k[0],
                "model": k[1],
                "successful_task_checkpoints": n,
                "count_unit": "task checkpoint, NOT HTTP",
            }
            for k, n in sorted(legacy_tasks.items())
        ],
        "engine_attempts": attempts,
        "provider_interactions": interactions,
        "requests": requests,
        "work_groups": groups,
        "retrieval_queries": queries,
        "materials": list(materials.values()),
        "body_blocks": blocks,
        "proposals": proposals,
        "reviews": review,
        "review_calls": review_call_details(requests, task_map),
        "detailed_archives": detailed_archives(root, job_id),
        "task_checkpoints": snapshot["tasks"],
        "cache_events": cache_events,
        "progress_events": [
            e
            for e in events
            if e["kind"] in {"progress", "work_update", "section_saved", "work_state_saved"}
        ],
        "final_snapshot": job.get("snapshot", {}),
    }
    return clean(report)


def csv_value(value):
    if value is None:
        return ""
    text = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, (dict, list))
        else str(value)
    )
    # CSV must be safe to open in spreadsheets; JSON retains the exact string.
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def html_cell(value, *, language="zh-CN"):
    text = (
        json.dumps(value, ensure_ascii=False, indent=2)
        if isinstance(value, (dict, list))
        else (("Unknown" if language == "en" else "未知") if value is None else str(value))
    )
    escaped = html.escape(text, quote=True)
    return (
        f"<details><summary>{html.escape(text[:110])}…</summary><pre>{escaped}</pre></details>"
        if len(text) > 160
        else escaped
    )


def table(title, rows, columns, *, language="zh-CN"):
    from folio.runtime.language import localize_text

    title = localize_text(title, language)
    head = "".join(f"<th>{html.escape(key)}</th>" for key in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html_cell(row.get(key), language=language)}</td>" for key in columns) + "</tr>"
        for row in rows
    )
    count = localize_text(f"{len(rows)} 条", language)
    return f"<details><summary>{html.escape(title)} · {count}</summary><div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></details>"


def write_report(report, output, *, language="zh-CN"):
    from functools import partial

    from folio.runtime.language import localize_text

    def tr(text):
        return localize_text(text, language)

    def metadata(value, key=""):
        # Source quotations, user titles and model content retain their original language.
        if isinstance(value, dict):
            return {name: metadata(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [metadata(item, key) for item in value]
        if isinstance(value, str) and key in {"warnings", "limitations", "precision", "basis", "basis_description", "timing_scope", "parent_timing_scope"}:
            return tr(value)
        return value

    report = metadata(report)
    render_table = partial(table, language=language)
    output = Path(output).resolve()
    if ".folio" not in output.parts:
        raise ValueError("Business audit reports must be written beneath a .folio directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, rows in report.items():
        if not isinstance(rows, list) or (rows and not isinstance(rows[0], dict)):
            continue
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with (output / (name + ".csv")).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, columns)
            writer.writeheader()
            writer.writerows({key: csv_value(value) for key, value in row.items()} for row in rows)
    components = [
        render_table("阶段墙钟", report["stage_totals"], ["stage", "wall_seconds"]),
        render_table(
            "实际服务交互汇总（HTTP / MCP 分开）",
            report["provider_summary"],
            [
                "provider",
                "model",
                "operation",
                "count_unit",
                "interaction_class",
                "recorded_interactions",
                "success",
                "failed",
                "unfinished",
                "additional_io_within_same_engine_attempt",
                "aggregate_io_seconds",
            ],
        ),
        render_table(
            "引擎步骤汇总（不是 HTTP 次数）",
            report["action_summary"],
            [
                "task",
                "engine_attempts",
                "success",
                "failed",
                "unfinished",
                "engine_retry_attempts",
                "recorded_cache_hits",
                "aggregate_queue_seconds",
                "aggregate_service_seconds",
                "prompt_tokens",
                "completion_tokens",
            ],
        ),
        render_table(
            "任务如何划分",
            report["work_groups"],
            [
                "task_id",
                "title",
                "objective",
                "section_ids",
                "depends_on",
                "questions",
                "completion_criteria",
                "rationale",
                "missing",
            ],
        ),
        render_table(
            "每个检索问题、材料及引用去向",
            report["retrieval_queries"],
            [
                "work_task_id",
                "source_kind",
                "query",
                "retrieval_id",
                "started_at",
                "finished_at",
                "duration_ms",
                "outcome",
                "measurement",
                "evidence_ids",
                "source_documents",
            ],
        ),
        render_table(
            "逐节正文来源",
            report["body_blocks"],
            [
                "section_number",
                "section_title",
                "block_id",
                "kind",
                "origin",
                "basis_description",
                "text",
                "rows",
                "evidence_ids",
                "source_documents",
            ],
        ),
        render_table(
            "原始材料与最终正文引用",
            report["materials"],
            [
                "evidence_id",
                "source_kind",
                "document_name",
                "url",
                "locator",
                "text",
                "referenced_by",
            ],
        ),
        render_table(
            "每轮审阅、精查和修订",
            report["reviews"],
            [
                "at",
                "phase",
                "round_id",
                "unit_id",
                "status",
                "outcome",
                "text",
                "findings",
                "reason",
                "model_verdict",
                "calls",
                "elapsed_ms",
            ],
        ),
        render_table(
            "审阅具体输入与模型最终判定",
            report["review_calls"],
            [
                "task",
                "task_key",
                "context_ref",
                "attempts",
                "section_ids",
                "input_details",
                "applicability_checks",
                "repair_targets",
                "stored_result",
            ],
        ),
        render_table(
            "每次引擎尝试",
            report["engine_attempts"],
            [
                "task",
                "task_key",
                "attempt",
                "started_at",
                "finished_at",
                "duration_ms",
                "queue_ms",
                "service_ms",
                "outcome",
                "error_code",
                "context_ref",
                "prompt_tokens",
                "completion_tokens",
            ],
        ),
        render_table(
            "每次实际服务交互",
            report["provider_interactions"],
            [
                "interaction_id",
                "provider",
                "model",
                "operation",
                "task",
                "task_key",
                "attempt",
                "started_at",
                "finished_at",
                "duration_ms",
                "outcome",
                "status_code",
                "error_code",
                "prompt_tokens",
                "completion_tokens",
            ],
        ),
        render_table(
            "所有请求目录及任务结果",
            report["requests"],
            [
                "task",
                "task_key",
                "context_ref",
                "attempts",
                "work_task_id",
                "section_ids",
                "request_character_count",
                "evidence_ids_in_request",
                "input_details",
                "stored_result",
                "context_path",
            ],
        ),
        render_table(
            "模型补写及适用章节",
            report["proposals"],
            ["work_task_id", "gap_id", "section_ids", "disposition", "content"],
        ),
        render_table("缓存复用事件", report["cache_events"], ["created_at", "kind", "data"]),
    ]
    warnings = "".join("<li>" + html.escape(item) + "</li>" for item in report["limitations"])
    summary = html.escape(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    content = f"""<!doctype html><html lang="{language}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{tr('文档生成任务详细审计')}</title><style>
body{{font-family:system-ui,'Noto Sans CJK SC',sans-serif;margin:24px;color:#17212b;background:#f8fafc;line-height:1.6}}
main{{max-width:1550px;margin:auto}}details{{background:white;border:1px solid #dce3eb;border-radius:7px;margin:12px 0;padding:12px}}
summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;word-break:break-word;font:13px/1.55 ui-monospace,monospace}}
.scroll{{overflow:auto;max-height:75vh}}table{{border-collapse:collapse;font-size:13px;min-width:100%;background:white}}
td,th{{border:1px solid #e2e8f0;padding:8px;text-align:left;vertical-align:top;min-width:85px;max-width:550px;overflow-wrap:anywhere}}
th{{position:sticky;top:0;background:#edf2f7}}td details{{min-width:230px;margin:0}}li{{margin:4px 0}}
</style><main><h1>{tr('文档生成任务详细审计')}</h1><p>{tr('任务')} {html.escape(str(report["job"]["id"]))} · {tr('状态')} {html.escape(str(report["job"]["status"]))}</p>
<p>{tr('仅本地离线读取。JSON 与 CSV 保留逐条记录；HTML 可展开查看。所有时间均为 UTC。')}</p>
<details open><summary>{tr('统计口径和限制')}</summary><ul>{warnings}</ul></details>
<details open><summary>{tr('整体统计')}</summary><pre>{summary}</pre></details>{"".join(components)}</main></html>"""
    (output / "report.html").write_text(content, encoding="utf-8")
    return output / "report.html"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en", help="Audit report language")
    args = parser.parse_args()
    report = build_report(args.root, args.job)
    path = write_report(report, args.output, language=args.language)
    print(
        json.dumps(
            {
                "report": str(path),
                "job_id": args.job,
                "recorded_provider_operations": report["summary"]["recorded_provider_operations"],
                "wall_seconds": report["summary"]["wall_seconds"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
