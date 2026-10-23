"""Task-local, safe I/O accounting; never persist headers or provider bodies."""

import asyncio
import math
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

_scope = ContextVar("folio_interaction_audit", default=None)
_ERROR_CODES = {
    "authentication",
    "billing",
    "configuration",
    "rate_limit",
    "upstream",
    "protocol",
    "timeout",
    "connection",
    "model_error",
    "context_limit",
    "length_limit",
    "stalled_output",
    "repetition",
    "schema_unsupported",
    "tool_choice_unsupported",
    "invalid_request",
    "endpoint_not_found",
    "decision_unavailable",
    "selection_context_limit",
}
_ERROR_TYPES = {
    "ValueError",
    "TypeError",
    "TimeoutError",
    "CancelledError",
    "ReadTimeout",
    "ConnectTimeout",
}


@contextmanager
def interaction_scope(store, job_id, task, task_key, attempt=1):
    token = _scope.set((store, job_id, {"task": task, "task_key": task_key, "attempt": attempt}))
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def provider_attempt(provider, operation, model=None):
    """One entry per actual provider operation, including failed internal retries.

    Model/decision/Tavily operations correspond to HTTP requests. RAGFlow's
    retrieval is an MCP tool invocation, not a count of its session HTTP traffic.
    With no scope, this is a no-op for settings tests and legacy callers.
    """
    scope = _scope.get()
    record = {}
    if scope is None:
        yield record
        return
    store, job_id, parent = scope
    entry = parent | {
        "interaction_id": uuid4().hex,
        "provider": provider,
        "operation": operation,
        "model": model,
        "started_at": datetime.now(UTC).isoformat(),
    }
    reserve = getattr(store, "begin_provider_attempt", None)
    if provider in {"llm", "decision"} and callable(reserve):
        # Reservation and provider_started must commit together before the HTTP
        # body runs, including internal retries hidden inside one engine call.
        reserve(job_id, entry)
    else:
        store.audit_event(job_id, "provider_started", entry)
    start = monotonic()
    outcome, error_code = "success", None
    try:
        yield record
    except BaseException as exc:
        outcome = (
            "consumer_stopped"
            if isinstance(exc, GeneratorExit)
            else "cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else "failed"
        )
        code = getattr(exc, "code", None)
        error_code = (
            code
            if isinstance(code, str) and code in _ERROR_CODES
            else type(exc).__name__
            if type(exc).__name__ in _ERROR_TYPES
            else "error"
        )
        raise
    finally:
        safe = {}
        for key in (
            "status_code",
            "first_content_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            value = record.get(key)
            if key == "status_code" and not (type(value) is int and 100 <= value <= 599):
                continue
            if type(value) in {int, float} and math.isfinite(value) and value >= 0:
                safe[key] = value
        # No arbitrary strings from upstream; only callers' explicit outcome tags.
        if record.get("termination") in {"completed", "validated_json_padding", "consumer_stopped"}:
            safe["termination"] = record["termination"]
            if outcome in {"cancelled", "consumer_stopped"} and safe["termination"] in {
                "completed",
                "validated_json_padding",
            }:
                outcome = "success"
                error_code = None
        store.audit_event(
            job_id,
            "provider_completed",
            entry
            | safe
            | {
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((monotonic() - start) * 1000, 3),
                "outcome": outcome,
                "error_code": error_code,
            },
        )
