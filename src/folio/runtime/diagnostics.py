"""Separate safe failure metrics from bounded, local-only model output audits."""

import math
import re
from dataclasses import dataclass
from datetime import datetime

MAX_CAUSE_DEPTH = 8
MAX_PARTIAL_OUTPUT_CHARS = 32_000

# Stable application-owned reasons; never derive public text from provider bodies.
PROTOCOL_MESSAGES = {
    "tool_delta_invalid": "Malformed tool-call delta",
    "tool_index_missing": "Missing tool-call index",
    "tool_index_invalid": "Invalid tool-call index",
    "tool_index_sparse": "Tool-call indices are not contiguous",
    "tool_batch_limit": "Tool-call batch exceeds the supported limit",
    "tool_id_duplicate": "Duplicate tool-call ID in one response",
    "multiple_tool_calls": "Expected exactly one tool call",
    "tool_type_unsupported": "Unsupported tool-call type",
    "tool_id_invalid": "Invalid tool-call ID",
    "tool_function_invalid": "Malformed tool-call function",
    "tool_fragment_invalid": "Invalid tool-call fragment",
    "tool_finish_invalid": "Expected a completed native tool call",
    "tool_id_missing": "Missing tool-call ID",
    "tool_unknown": "Unknown tool call",
    "tool_arguments_json": "Invalid tool-call arguments JSON",
    "tool_arguments_type": "Tool-call arguments must be an object",
    "response_not_sse": "Expected event stream",
    "stream_json_invalid": "Malformed stream",
    "stream_shape_invalid": "Invalid model response",
    "upstream_stream_error": "Model service reported a stream error",
    "legacy_function_call": "Unexpected legacy function call",
    "unexpected_tool_call": "Unexpected protocol tool call",
    "tool_after_finish": "Tool call after completion",
    "content_invalid": "Invalid content",
    "text_after_finish": "Text after completion",
    "incomplete_response": "Incomplete response",
    "malformed_response": "Malformed response",
}
FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "unknown", "missing"})

SAFE_SCHEMA_KEYWORDS = frozenset(
    {
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "oneOf",
        "allOf",
        "anyOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "prefixItems",
        "additionalItems",
        "additionalProperties",
        "pattern",
        "format",
        "minProperties",
        "maxProperties",
        "$dynamicRef",
        "$dynamicAnchor",
    }
)
_ENUMS = {
    "exception": {
        "CancelledError",
        "TimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ModelCallFailed",
        "ModelContextLimit",
        "ModelOutputRepetition",
    },
    "phase": {"connect_or_response_headers", "first_content", "streaming_content", "streaming_tool_call"},
    "finish_reason": FINISH_REASONS,
    "protocol_issue": set(PROTOCOL_MESSAGES),
    "backend_error": {"grammar", "invalid_json_schema"},
    "schema_constraint": {"required_all_properties"},
}
_COUNTS = {
    "received_chars",
    "tool_call_chars",
    "reasoning_chars",
    "trailing_whitespace_chars",
    "stream_event_chars",
    "stream_character_limit",
    "requested_output_tokens",
    "estimated_input_tokens",
    "max_input_tokens",
    "context_window",
    "margin_tokens",
    "completion_tokens",
    "prompt_tokens",
    "total_tokens",
    "status_code",
    "tool_calls_received",
    "stream_event_index",
    "failed_tool_index",
    "tool_argument_error_position",
}
_TIMES = {
    "call_timeout_seconds",
    "elapsed_seconds",
    "response_headers_seconds",
    "first_event_seconds",
    "last_event_seconds",
    "first_content_seconds",
    "last_content_seconds",
}
_DECISION_OUTCOMES = {
    "success",
    "cancelled",
    "configuration",
    "authentication",
    "endpoint_not_found",
    "tool_choice_unsupported",
    "billing",
    "rate_limit",
    "upstream",
    "protocol",
    "timeout",
    "connection",
}


def _safe_decision_http_attempts(value):
    """Keep only complete local transport metrics, never source/header/provider strings."""
    if not isinstance(value, list):
        return None
    result, seen = [], set()
    for entry in value[:10000]:
        if not isinstance(entry, dict):
            continue
        request_id, attempt = entry.get("request_id"), entry.get("attempt")
        started_at, duration = entry.get("started_at"), entry.get("duration_ms")
        outcome, retryable = entry.get("outcome"), entry.get("retryable")
        if (
            not isinstance(request_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
            or request_id in seen
            or type(attempt) is not int
            or not 1 <= attempt <= 1000
            or not isinstance(started_at, str)
            or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00", started_at)
            is None
            or type(duration) not in {int, float}
            or not math.isfinite(duration)
            or not 0 <= duration <= 10**12
            or not isinstance(outcome, str)
            or outcome not in _DECISION_OUTCOMES
            or type(retryable) is not bool
        ):
            continue
        try:
            datetime.fromisoformat(started_at)
        except ValueError:
            continue
        safe = {
            "request_id": request_id,
            "attempt": attempt,
            "started_at": started_at,
            "duration_ms": round(duration, 3),
            "outcome": outcome,
            "retryable": retryable,
        }
        status = entry.get("status_code")
        if type(status) is int and 100 <= status <= 599:
            safe["status_code"] = status
        result.append(safe)
        seen.add(request_id)
    return result


def _causes(exc):
    seen = set()
    for _ in range(MAX_CAUSE_DEPTH):
        if not isinstance(exc, BaseException) or id(exc) in seen:
            return
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__


def safe_failure_diagnostics(exc: BaseException) -> dict:
    """Return allowlisted metrics only; never copy arbitrary exception/provider text."""
    result = {}
    for cause in _causes(exc):
        values = getattr(cause, "diagnostics", None)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key in result:
                continue
            if key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
                result[key] = value
            elif key in _COUNTS and type(value) is int and 0 <= value <= 10**12:
                if key != "status_code" or 100 <= value <= 599:
                    result[key] = value
            elif key in _TIMES and type(value) in {int, float} and 0 <= value <= 10**12:
                if math.isfinite(value):
                    result[key] = round(value, 3)
            elif key == "unsupported_schema_keywords" and isinstance(value, list):
                result[key] = sorted(
                    {
                        item
                        for item in value
                        if isinstance(item, str) and item in SAFE_SCHEMA_KEYWORDS
                    }
                )
            elif key == "decision_http_attempts":
                attempts = _safe_decision_http_attempts(value)
                if attempts is not None:
                    result[key] = attempts
            elif key == "done_received" and type(value) is bool:
                result[key] = value
            elif key == "research_request_id" and isinstance(value, str):
                if re.fullmatch(r"research_[0-9]{3,6}", value):
                    result[key] = value
    return result


def tool_delta_for_audit(deltas):
    """Bounded private projection; never copy an SSE event or reasoning/error fields."""
    def scalar(value):
        if isinstance(value, str):
            if len(value) <= 1024:
                return value
            return {"head": value[:512], "tail": value[-512:],
                    "characters": len(value), "truncated": True}
        if value is None or type(value) in {int, float, bool}:
            return value
        return {"value_type": type(value).__name__}

    if not isinstance(deltas, list):
        return {"value_type": type(deltas).__name__}
    result = []
    # A second tool already violates the one-action contract. Preserve it even
    # though it never enters the assembled calls; avoid retaining unbounded data.
    for delta in deltas[:2]:
        if not isinstance(delta, dict):
            result.append({"value_type": type(delta).__name__})
            continue
        item = {key: scalar(delta[key]) for key in ("index", "id", "type") if key in delta}
        if "function" in delta:
            function = delta["function"]
            item["function"] = (
                {key: scalar(function[key]) for key in ("name", "arguments") if key in function}
                if isinstance(function, dict) else {"value_type": type(function).__name__}
            )
        result.append(item)
    return result


@dataclass(frozen=True, repr=False)
class _PartialModelOutput:
    content_head: str
    content_tail: str
    received_chars: int


def capture_partial_output(exc: BaseException, content: str) -> None:
    """Runtime-only input: decoded assistant content, never HTTP/SSE error bodies or reasoning."""
    if not isinstance(content, str) or not content:
        return
    if len(content) <= MAX_PARTIAL_OUTPUT_CHARS:
        head, tail = content, ""
    else:
        half = MAX_PARTIAL_OUTPUT_CHARS // 2
        head, tail = content[:half], content[-half:]
    exc._partial_model_output = _PartialModelOutput(head, tail, len(content))


def partial_output_for_audit(exc: BaseException) -> dict | None:
    """Only for a private local archive; callers MUST NOT put this in usage, UI or errors."""
    for cause in _causes(exc):
        value = getattr(cause, "_partial_model_output", None)
        if not isinstance(value, _PartialModelOutput):
            continue
        retained = len(value.content_head) + len(value.content_tail)
        return {
            "content_head": value.content_head,
            "content_tail": value.content_tail,
            "received_chars": value.received_chars,
            "retained_chars": retained,
            "truncated": retained < value.received_chars,
        }
    return None
