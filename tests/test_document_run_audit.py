"""Audit numbers are measured units, not inferred HTTP calls or summed parallel time."""

import importlib
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def audit(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("audit_document_run")


def database(tmp_path, events, *, sections=(), retrievals=()):
    root = tmp_path / "data"
    root.mkdir()
    with sqlite3.connect(root / "folio.sqlite3") as conn:
        conn.executescript("""
            CREATE TABLE jobs(id TEXT, status TEXT, stage TEXT, input TEXT, plan TEXT,
                snapshot TEXT, calls INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, job_id TEXT,kind TEXT,data TEXT,created_at TEXT);
            CREATE TABLE tasks(job_id TEXT,task_key TEXT,attempts INTEGER,result TEXT,usage TEXT,error TEXT);
            CREATE TABLE sections(job_id TEXT,section_id TEXT,state TEXT);
            CREATE TABLE retrievals(job_id TEXT,retrieval_key TEXT,evidence TEXT);
        """)
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "job",
                "failed",
                "generation",
                json.dumps({"api_key": "do-not-export"}),
                json.dumps(
                    {"sections": [{"section_id": "s1", "title": "业务", "display_number": "1"}]}
                ),
                "{}",
                1,
                "2026-10-12 00:00:00",
                "2026-10-12 00:00:10",
            ),
        )
        for i, (kind, second, data) in enumerate(events, 1):
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?)",
                (
                    i,
                    "job",
                    kind,
                    json.dumps(data),
                    f"2026-10-12 00:00:{second:02d}",
                ),
            )
        for state in sections:
            conn.execute(
                "INSERT INTO sections VALUES (?,?,?)",
                ("job", state["section_id"], json.dumps(state)),
            )
        for key, evidence in retrievals:
            conn.execute(
                "INSERT INTO retrievals VALUES (?,?,?)", ("job", key, json.dumps(evidence))
            )
    return root


def test_parallel_service_time_does_not_replace_wall_time_and_legacy_http_is_unknown(
    audit, tmp_path
):
    events = [
        ("progress", 0, {"stage": "generation"}),
        ("task_started", 1, {"key": "generate:a", "attempt": 1}),
        ("task_started", 1, {"key": "generate:b", "attempt": 1}),
        ("task_completed", 9, {"key": "generate:a", "usage": {"queue_ms": 0, "service_ms": 8000}}),
        ("task_completed", 9, {"key": "generate:b", "usage": {"queue_ms": 0, "service_ms": 8000}}),
        ("progress", 10, {"status": "failed"}),
    ]
    root = database(tmp_path, events)
    before = (root / "folio.sqlite3").read_bytes()
    report = audit.build_report(root, "job")
    assert report["summary"]["wall_seconds"] == 10
    assert sum(s["wall_seconds"] for s in report["stage_totals"]) == 10
    assert report["action_summary"][0]["aggregate_service_seconds"] == 16
    assert report["summary"]["recorded_provider_operations"] is None
    assert report["action_summary"][0]["recorded_cache_hits"] is None
    assert (root / "folio.sqlite3").read_bytes() == before  # Read-only, no Store migration.


def test_provider_internal_retry_and_incomplete_operation_are_counted_separately(audit, tmp_path):
    provider = {
        "provider": "decision",
        "model": "jev",
        "operation": "evaluate",
        "task": "select_context",
        "task_key": "select_context:a",
        "attempt": 1,
    }
    first = provider | {"interaction_id": "first", "started_at": "2026-10-12T00:00:01.250+00:00"}
    second = provider | {"interaction_id": "second", "started_at": "2026-10-12T00:00:03.500+00:00"}
    events = [
        ("progress", 0, {"stage": "generation"}),
        ("task_started", 1, {"key": "select_context:a", "attempt": 1}),
        ("provider_started", 1, first),
        (
            "provider_completed",
            3,
            first
            | {
                "finished_at": "2026-10-12T00:00:03.250+00:00",
                "duration_ms": 2000,
                "outcome": "failed",
                "status_code": 429,
                "error_code": "DecisionUnavailable",
            },
        ),
        ("provider_started", 3, second),
    ]
    report = audit.build_report(database(tmp_path, events), "job")
    assert report["summary"]["recorded_engine_attempts"] == 1
    assert report["summary"]["recorded_provider_operations"] == 2
    summary = report["provider_summary"][0]
    assert summary["failed"] == summary["unfinished"] == 1
    assert summary["additional_io_within_same_engine_attempt"] == 1
    assert summary["io_on_engine_retry_attempt"] == 0
    assert report["provider_interactions"][1]["duration_ms"] is None
    assert report["engine_attempts"][0]["outcome"] == "unfinished"


def test_mixed_sources_retrieval_mapping_and_export_escaping(audit, tmp_path):
    evidence = [
        {
            "evidence_id": "e1",
            "source_id": "ragflow:data:doc",
            "source_version": "v1",
            "locator": "c1",
            "text": "内部规则 <script>alert(1)</script>",
            "metadata": {"document_name": "规则.docx"},
        },
        {
            "evidence_id": "w1",
            "source_id": "https://public.example/article",
            "source_version": "v1",
            "locator": "web",
            "text": "公开能力资料",
            "metadata": {
                "role": "public_web",
                "url": "https://public.example/article",
                "title": "产品说明",
                "project_fact": False,
            },
        },
    ]
    state = {
        "section_id": "s1",
        "revision": 1,
        "evidence": evidence,
        "content": {
            "blocks": [
                {
                    "block_id": "b1",
                    "origin": "evidence",
                    "kind": "paragraph",
                    "text": "<script>unsafe()</script>",
                    "evidence_ids": ["e1", "w1"],
                },
                {
                    "block_id": "b2",
                    "origin": "model_proposal",
                    "kind": "paragraph",
                    "text": "=MALICIOUS()",
                    "evidence_ids": ["w1"],
                },
            ]
        },
    }
    retrieval = {
        "retrieval_id": "r1",
        "work_task_id": "t1",
        "source_kind": "public_web",
        "queries": ["第一个问题", "第二个问题"],
        "started_at": "2026-10-12T00:00:01+00:00",
    }
    root = database(
        tmp_path,
        [
            ("retrieval_started", 1, retrieval),
            (
                "retrieval_completed",
                3,
                retrieval
                | {
                    "finished_at": "2026-10-12T00:00:03+00:00",
                    "duration_ms": 2000,
                    "outcome": "success",
                    "evidence_ids": ["w1"],
                },
            ),
        ],
        sections=[state],
        retrievals=[("r1", evidence)],
    )
    report = audit.build_report(root, "job")
    assert report["summary"]["retrieval_batches_in_report"] == 1
    assert report["summary"]["retrieval_questions_in_report"] == 2
    assert report["body_blocks"][0]["source_kinds"] == ["knowledge_base", "public_web"]
    assert report["body_blocks"][1]["origin"] == "model_proposal"
    assert len(report["retrieval_queries"][0]["source_documents"][0]["referenced_by"]) == 2
    output = tmp_path / ".folio" / "report"
    page = audit.write_report(report, output).read_text()
    assert "<script>unsafe()" not in page and "&lt;script&gt;unsafe()" in page
    assert "do-not-export" not in (output / "report.json").read_text()
    assert "'=MALICIOUS()" in (output / "body_blocks.csv").read_text(encoding="utf-8-sig")
    assert (
        json.loads((output / "report.json").read_text())["body_blocks"][1]["text"] == "=MALICIOUS()"
    )
    with pytest.raises(ValueError, match=".folio"):
        audit.write_report(report, tmp_path / "public")


def test_auxiliary_http_is_not_llm_generation_and_checkpoint_review_details_are_visible(
    audit, tmp_path
):
    events = []
    for index, (provider, operation) in enumerate(
        [
            ("llm_auxiliary", "models"),
            ("llm_auxiliary", "tokenize"),
            ("llm", "chat.completions"),
        ]
    ):
        data = {
            "interaction_id": str(index),
            "provider": provider,
            "operation": operation,
            "task": "drill_down",
            "task_key": "drill_down:a",
            "attempt": 1,
            "started_at": "2026-10-12T00:00:01+00:00",
        }
        events.extend(
            [
                ("provider_started", 1, data),
                (
                    "provider_completed",
                    2,
                    data
                    | {
                        "finished_at": "2026-10-12T00:00:02+00:00",
                        "outcome": "success",
                        "duration_ms": 1000,
                    },
                ),
            ]
        )
    root = database(tmp_path, events)
    verdict = {
        "confirmed": True,
        "applicability_checks": [{"source_id": "e1", "reason": "具体主体必须一致"}],
        "repair_targets": [{"section_id": "s1", "block_id": "b2", "instruction": "保持补写属性"}],
    }
    with sqlite3.connect(root / "folio.sqlite3") as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
            (
                "job",
                "drill_down:a",
                1,
                json.dumps(verdict),
                "{}",
                None,
            ),
        )
    report = audit.build_report(root, "job")
    assert report["summary"]["recorded_provider_operations"] == 3
    assert report["summary"]["recorded_llm_chat_requests"] == 1
    assert report["summary"]["recorded_auxiliary_http_requests"] == 2
    assert report["review_calls"][0]["applicability_checks"] == verdict["applicability_checks"]
    assert report["review_calls"][0]["repair_targets"] == verdict["repair_targets"]
    page = audit.write_report(report, tmp_path / ".folio" / "audit").read_text()
    assert "applicability_checks" in page and "具体主体必须一致" in page
    assert "repair_targets" in page and "保持补写属性" in page
    assert "auxiliary_http_not_llm_generation" in page
