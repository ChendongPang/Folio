"""Small, allowlisted diagnostics for the conversation entrance; no model/user bodies."""

import math
from contextvars import ContextVar

from pydantic import ValidationError

from folio.runtime.diagnostics import safe_failure_diagnostics

conversation_log_id = ContextVar("conversation_log_id", default=None)

_NUMERIC_FIELDS = {
    "elapsed_seconds",
    "received_chars",
    "reasoning_chars",
    "first_content_seconds",
    "output_chars",
    "attempt",
    "attempts",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "window",
    "status_code",
    "timeout_seconds",
}
_TRANSPORT_PHASES = {
    "connect_or_response_headers",
    "first_content",
    "streaming_content",
    "streaming_tool_call",
}
_ERROR_CODES = {
    "protocol",
    "timeout",
    "context_limit",
    "directory_context_limit",
    "configuration",
    "authentication",
    "rate_limit",
    "length_limit",
    "repetition",
    "schema_unsupported",
    "billing",
    "model_error",
}
_VALIDATION_TYPES = {
    "missing",
    "string_type",
    "string_too_short",
    "string_too_long",
    "literal_error",
    "extra_forbidden",
    "model_type",
    "dict_type",
}


def intent_format_errors(exc):
    """Pydantic's normal errors include input values; keep only known field/type labels."""
    if isinstance(exc, ValidationError):
        return [
            {
                "field": item["loc"][0]
                if item["loc"] and item["loc"][0] in {"intent", "summary"}
                else "response",
                "type": item["type"] if item["type"] in _VALIDATION_TYPES else "invalid",
            }
            for item in exc.errors(include_url=False, include_context=False, include_input=False)[
                :8
            ]
        ]
    return [{"field": "response", "type": "invalid_json_or_object"}]


def error_diagnostics(exc, *, conversation_id, stage, code, elapsed_ms=0):
    """Never stringify an exception: providers may include API keys or request bodies."""
    result = {
        "conversation_id": conversation_id,
        "stage": stage,
        "exception_type": type(exc).__name__,
        "code": code if code in _ERROR_CODES else "model_error",
        "elapsed_ms": elapsed_ms,
    }
    cause = exc.__cause__
    if cause is not None:
        result["cause_type"] = type(cause).__name__
    details = getattr(exc, "diagnostics", {})
    if isinstance(details, dict):
        for name in _NUMERIC_FIELDS:
            value = details.get(name, getattr(exc, name, None))
            if type(value) in {int, float} and math.isfinite(value) and abs(value) <= 1e12:
                result[name] = value
        if isinstance(details.get("phase"), str) and details["phase"] in _TRANSPORT_PHASES:
            result["transport_phase"] = details["phase"]
        if details.get("deadline_source") == "directory_call":
            result["deadline_source"] = "directory_call"
    safe_details = safe_failure_diagnostics(exc)
    for name in (
        "backend_error",
        "schema_constraint",
        "unsupported_schema_keywords",
        "protocol_issue",
        "finish_reason",
        "tool_call_chars",
        "tool_calls_received",
        "done_received",
        "stream_event_index",
        "failed_tool_index",
        "tool_argument_error_position",
        "research_request_id",
    ):
        if name in safe_details:
            result[name] = safe_details[name]
    if safe_details.get("phase") in _TRANSPORT_PHASES:
        result["transport_phase"] = safe_details["phase"]
    validation = exc if isinstance(exc, ValidationError) else cause
    if stage == "intent" and isinstance(validation, (ValidationError, ValueError)):
        result["format_errors"] = intent_format_errors(validation)
    return result
