"""One authorized, fresh replay including optional intent/directory preparation.

Uses the same service functions as the GUI, with the user's supplied confirmations
automated. This is not a browser interaction or a claim of browser UI coverage.
"""

import argparse
import asyncio
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from uuid import uuid4

from benchmark_job import code_digest
from benchmark_optimization import (
    finish_ledger,
    inspect_artifact,
    read_source,
    reserve_run,
    source_signature,
    summarize_events,
    utc_now,
    write_json,
)

from folio.api.workspace import detect_intent
from folio.directory_drafts import DirectoryDrafts
from folio.models import DocumentPlan, JobInput
from folio.previews import PreviewLibrary
from folio.runtime.interaction_audit import interaction_scope
from folio.service import execute, resolve_model_config
from folio.source_documents import source_catalog
from folio.store import Store


@contextmanager
def phase(store, job_id, name):
    started = monotonic()
    entry = {"phase": name, "started_at": utc_now()}
    store.audit_event(job_id, "preparation_started", entry)
    outcome, primary_error = "success", None
    try:
        with interaction_scope(store, job_id, name, "preparation:" + name):
            yield
    except BaseException as exc:
        outcome, primary_error = "failed", exc
        raise
    finally:
        try:
            store.audit_event(
                job_id,
                "preparation_completed",
                entry
                | {
                    "finished_at": utc_now(),
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                    "outcome": outcome,
                },
            )
        except BaseException:
            if primary_error is None:
                raise
            # A failed log write must never hide the actual preparation failure.


async def run(args):
    source = read_source(args.root, args.source_job)
    request = JobInput.model_validate(source["input"])
    if request.runtime != "llm":
        raise ValueError("Real model configuration is required")
    config = resolve_model_config(args.root)
    signature = source_signature(source, config)
    request.document_id = "web_" + uuid4().hex
    request.limits.run_timeout_seconds = args.generation_timeout
    request.limits.max_parallel_tasks = args.parallel
    initial_request = request.user_request
    if args.from_uploads:
        request.title = args.title or request.title
    overrides = {
        "run_timeout_seconds": {
            "from": source["input"]["limits"]["run_timeout_seconds"],
            "to": args.generation_timeout,
        },
        "max_parallel_tasks": {
            "from": source["input"]["limits"]["max_parallel_tasks"],
            "to": args.parallel,
        },
        "preparation": "fresh_intent_directory"
        if args.from_uploads
        else "confirmed_directory",
        "cover": request.cover,
    }
    index, output, reservation = reserve_run(
        args.output_root, args.source_job, 1, signature, overrides
    )
    report = reservation | {
        "source_job": args.source_job,
        "source_signature": signature,
        "code_sha256": None,
        "cached_results_imported": False,
        "execution_mode": "same_GUI_service_functions_with_automated_confirmations",
        "target_seconds": 420,
        "passed": False,
        "from_uploads": args.from_uploads,
    }
    result_path = output / "result.json"
    started = monotonic()
    store, job_id, plan, body_started = None, None, None, None

    def record_failure(exc, stage, *, primary=False):
        detail = {"stage": stage, "exception_type": type(exc).__name__}
        code = getattr(exc, "code", None)
        # Internal errors already use fixed codes; never echo arbitrary exception
        # text, a provider response, a URL, or credentials into the audit report.
        from folio.runtime.interaction_audit import _ERROR_CODES

        if isinstance(code, str) and code in _ERROR_CODES:
            detail["exception_code"] = code
        if primary and "exception_type" not in report:
            report.update(
                exception_type=detail["exception_type"],
                exception_code=detail.get("exception_code"),
            )
        else:
            report.setdefault("audit_errors", []).append(detail)
        report.update(status="failed", passed=False, audit_complete=False)

    def inspect_safely(stage, operation, default=None):
        try:
            return operation()
        except BaseException as exc:
            record_failure(exc, stage)
            return default

    try:
        report["code_sha256"] = code_digest()
        write_json(result_path, report)
        store = Store(output / "data")
        job_id = store.create(request)
        report.update(job_id=job_id, independent_store=str(store.root))
        write_json(result_path, report)
        print(
            json.dumps({"event": "run_started", "job_id": job_id, "output": str(output)}),
            flush=True,
        )
        async with asyncio.timeout(args.timeout):
            if args.from_uploads:
                with phase(store, job_id, "source_preparation"):
                    catalog = await asyncio.to_thread(
                        source_catalog,
                        Path(request.knowledge_path) if request.knowledge_path else None,
                        cache_root=store.root / "source-cache",
                        extra_paths=request.reference_paths,
                    )
                with phase(store, job_id, "intent"):
                    intent = await detect_intent(
                        config,
                        [{"role": "user", "content": initial_request}],
                        {"attachments": [
                            {"role": "source", "name": item["name"]} for item in catalog
                        ]},
                    )
                    write_json(output / "intent.json", intent.model_dump())
                    if intent.intent != "generate":
                        raise ValueError("The real intent classifier did not choose generation")
                with phase(store, job_id, "directory_planning_and_preview"):
                    library = PreviewLibrary(store.root)
                    drafts = DirectoryDrafts(store.root, library)
                    preview = await drafts.create(
                        config,
                        request.document_id.removeprefix("web_"),
                        initial_request,
                        source_catalog=catalog,
                        title=request.title,
                        writing_guide=request.writing_guide.model_dump(mode="json")
                        if request.writing_guide
                        else None,
                        on_progress=lambda text: store.work_event(job_id, text),
                    )
                    saved = drafts.load(
                        preview["revision"], request.document_id.removeprefix("web_")
                    )
                    plan = saved["plan"]
                    write_json(
                        output / "directory-confirmation.json",
                        {
                            "automated": True,
                            "revision": preview["revision"],
                            "sections": preview["sections"],
                            "directory_folder": str(saved["folder"]),
                        },
                    )
            else:
                plan = DocumentPlan.model_validate(source["plan"])
            store.save_plan(job_id, plan)
            report["actual_plan_sections"] = len(plan.sections)
            report["actual_plan_sha256"] = hashlib.sha256(
                plan.model_dump_json().encode()
            ).hexdigest()
            report["preparation_seconds"] = round(monotonic() - started, 3)
            write_json(result_path, report)
            body_started = monotonic()
            await execute(store, job_id, config)
    except BaseException as exc:
        record_failure(exc, "execution", primary=True)
        if store is not None and job_id is not None:
            inspect_safely(
                "record_failed_job",
                lambda: store.update(
                    job_id,
                    status="failed",
                    error=report["exception_type"]
                    + ": "
                    + (report["exception_code"] or "unclassified"),
                ),
            )
    finally:
        report.update(
            elapsed_seconds=round(monotonic() - started, 3),
            finished_at=utc_now(),
            generation_to_export_seconds=round(monotonic() - body_started, 3)
            if body_started is not None
            else None,
        )
        report["code_unchanged"] = inspect_safely(
            "verify_code",
            lambda: report["code_sha256"] is not None and report["code_sha256"] == code_digest(),
            False,
        )
        report["source_configuration_unchanged"] = inspect_safely(
            "verify_source",
            lambda: signature == source_signature(read_source(args.root, args.source_job), config),
            False,
        )
        job = (
            inspect_safely("read_final_job", lambda: store.get(job_id))
            if store is not None and job_id
            else None
        )
        report.update(
            stage="unknown" if job_id else "initialization",
            calls=None if job_id else 0,
            saved_nodes=None if job_id else 0,
        )
        if job is not None:
            report.update(
                job_status=job["status"],
                status=job["status"],
                stage=job["stage"],
                calls=job["calls"],
                saved_nodes=inspect_safely("read_saved_nodes", lambda: len(store.states(job_id))),
                artifact=job.get("artifact"),
            )
            if plan is not None:
                report.update(
                    inspect_safely(
                        "inspect_artifact",
                        lambda: inspect_artifact(
                            store, job_id, plan.model_dump(), expected_sections=len(plan.sections)
                        ),
                        {},
                    )
                )
            timing = inspect_safely(
                "summarize_events",
                lambda: summarize_events(
                    store.events(job_id),
                    created_at=job["created_at"],
                    updated_at=job["updated_at"],
                    stopped_at=report["finished_at"],
                ),
            )
            if timing is not None:
                inspect_safely("write_timing", lambda: write_json(output / "timing.json", timing))
        report["within_target"] = report["elapsed_seconds"] <= 420
        report["body_within_target"] = (
            report["generation_to_export_seconds"] is not None
            and report["generation_to_export_seconds"] <= 420
        )
        report["audit_complete"] = not report.get("audit_errors")
        if report.get("exception_type") or not report["audit_complete"]:
            report["status"] = "failed"
        report["passed"] = bool(
            not report.get("exception_type")
            and report["audit_complete"]
            and report.get("complete_delivery")
            and report["within_target"]
            and report["code_unchanged"]
            and report["source_configuration_unchanged"]
        )
        # Both finalization operations are attempted independently. A transient
        # disk/audit error gets one bounded persistence retry; the reservation is
        # never removed, refunded, or replaced with an uncounted run.
        for _ in range(2):
            ledger_written = inspect_safely(
                "finish_ledger",
                lambda: (finish_ledger(args.output_root, index, report), True)[1],
                False,
            )
            report_written = inspect_safely(
                "write_result",
                lambda: (write_json(result_path, report), True)[1],
                False,
            )
            if report_written and ledger_written:
                break
            report["audit_complete"] = False
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in (
                        "job_id",
                        "elapsed_seconds",
                        "generation_to_export_seconds",
                        "status",
                        "calls",
                        "saved_nodes",
                        "artifact",
                        "passed",
                        "exception_code",
                        "audit_errors",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_job")
    parser.add_argument("--root", type=Path, default=Path(".folio"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--from-uploads", action="store_true")
    parser.add_argument("--title", help="Override the source document title")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--generation-timeout", type=int, default=900)
    parser.add_argument("--parallel", type=int, default=16)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
