"""Record every research question and its returned sources independently of model calls."""

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from folio.runtime.interaction_audit import interaction_scope


async def search_with_audit(
    store, job_id, provider, queries, *, task_id, source_kind, limit, max_chars
):
    key = uuid4().hex
    start = monotonic()
    entry = {
        "retrieval_id": key,
        "work_task_id": task_id,
        "source_kind": source_kind,
        "queries": [queries] if isinstance(queries, str) else queries,
        "started_at": datetime.now(UTC).isoformat(),
    }
    store.audit_event(job_id, "retrieval_started", entry)
    result, outcome, error_code = [], "success", None
    try:
        with interaction_scope(store, job_id, "retrieval", key):
            result = await provider.search(queries, limit=limit, max_chars=max_chars)
        return result
    except BaseException as exc:
        outcome, error_code = "failed", type(exc).__name__
        raise
    finally:
        store.audit_event(
            job_id,
            "retrieval_completed",
            entry
            | {
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((monotonic() - start) * 1000, 3),
                "outcome": outcome,
                "error_code": error_code,
                "evidence_ids": [e.evidence_id for e in result],
                "returned_characters": sum(len(e.text) for e in result),
            },
        )
