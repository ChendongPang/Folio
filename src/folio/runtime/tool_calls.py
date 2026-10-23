"""Decode native tool-call deltas without treating assistant prose as commands."""

import json

from folio.runtime.diagnostics import (
    partial_output_for_audit,
    safe_failure_diagnostics,
    tool_delta_for_audit,
)
from folio.runtime.errors import protocol_failure

MAX_TOOL_CALLS_PER_BATCH = 16


def native_retry_context(task, context, *, result=None, error=None, feedback=""):
    """Prepare one correction within the caller's existing retry allowance.

    Reply text remains an assistant message, never an executable action. Only
    already decoded provider calls receive correlated failure tool messages.
    """
    diagnostics = safe_failure_diagnostics(error) if error is not None else {}
    calls = getattr(result, "tool_calls", []) if result is not None else []
    no_calls = result is not None and not calls or (
        diagnostics.get("protocol_issue") == "tool_finish_invalid"
        and diagnostics.get("tool_calls_received") == 0
        and diagnostics.get("finish_reason") == "stop"
    )
    response = ""
    if no_calls:
        response = getattr(result, "content", "") if result is not None else ""
        if not response and error is not None:
            partial = partial_output_for_audit(error)
            if partial:
                response = partial["content_head"]
                if partial["truncated"]:
                    response += "\n[Middle of previous response omitted from the diagnostic excerpt.]\n"
                    response += partial["content_tail"]
        feedback = (
            "The previous response contained no native tool_calls. No tool was executed and no "
            "conclusion was saved. Your response text is preserved below only as an unexecuted "
            "assistant message. Use a currently offered API function, not prose or action JSON. "
        )
        if task in {"directory_research", "research_step"}:
            feedback += (
                "The current checkpoint requires research_save; save notes from the supplied evidence. "
                if context.get("memory_checkpoint_required")
                else "If research is complete, call finish with the supported findings and remaining "
                "missing information; do not repeat already completed reads solely to produce a tool call. "
            )
    elif diagnostics.get("protocol_issue"):
        feedback = (
            f"The previous native response failed protocol validation: {diagnostics['protocol_issue']}. "
            "No tool in that response was executed. Submit a complete call to a currently offered "
            "API function, with its actual name and valid JSON arguments. "
        )
    observations = list(context.get("observations", []))
    valid_calls = [call for call in calls if isinstance(call, dict) and call.get("id")
                   and isinstance(call.get("function"), dict)]
    reasoning = getattr(result, "reasoning_content", None)
    continuation = {"reasoning_content": reasoning} if isinstance(reasoning, str) else {}
    for call in valid_calls:
        observations.append({
            "request_id": call["id"], "tool_call": call,
            "batch_id": "rejected:" + valid_calls[0]["id"], "batch_tool_calls": valid_calls,
            "assistant_content": getattr(result, "content", ""), **continuation,
            "result": {"status": "failed", "error": feedback},
            "model_text": "Tool arguments failed validation; the tool was not executed: " + feedback,
        })
    revised = {key: value for key, value in context.items()
               if key not in {"native_retry_response", "native_retry_reasoning_content"}}
    revised.update(
        observations=observations,
        format_feedback=feedback if no_calls or diagnostics.get("protocol_issue") else (
            "Correct the tool arguments, then call a currently available tool: " + feedback
        ),
    )
    if isinstance(response, str) and response:
        revised["native_retry_response"] = response
        if isinstance(reasoning, str):
            revised["native_retry_reasoning_content"] = reasoning
    return revised


class ToolCallStream:
    def __init__(self, tools):
        self.offered = {tool["function"]["name"]: tool["function"] for tool in tools}
        self.calls = {}
        self.characters = 0
        self.indices_seen = set()
        self.current_delta = None

    def feed(self, deltas):
        self.current_delta = tool_delta_for_audit(deltas)
        if not isinstance(deltas, list):
            raise protocol_failure("tool_delta_invalid")
        for delta in deltas:
            # The rejected fragment need not be one of the first two in a
            # provider event. Keep the fragment actually being validated.
            self.current_delta = tool_delta_for_audit([delta])
            if not isinstance(delta, dict) or type(delta.get("index")) is not int:
                raise protocol_failure("tool_index_missing")
            index = delta["index"]
            self.indices_seen.add(index)
            if index < 0:
                raise protocol_failure("tool_index_invalid", failed_tool_index=index)
            if index >= MAX_TOOL_CALLS_PER_BATCH:
                raise protocol_failure("tool_batch_limit", failed_tool_index=index)
            call = self.calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if delta.get("type") not in (None, "function"):
                raise protocol_failure("tool_type_unsupported", failed_tool_index=index)
            if delta.get("id") is not None:
                if not isinstance(delta["id"], str):
                    raise protocol_failure("tool_id_invalid", failed_tool_index=index)
                call["id"] += delta["id"]
            function = delta.get("function", {})
            if not isinstance(function, dict):
                raise protocol_failure("tool_function_invalid", failed_tool_index=index)
            for key in ("name", "arguments"):
                fragment = function.get(key)
                if fragment is None:
                    continue
                if not isinstance(fragment, str):
                    raise protocol_failure("tool_fragment_invalid", failed_tool_index=index)
                call["function"][key] += fragment
                self.characters += len(fragment)

    def finish(self, reason):
        if reason != "tool_calls" or not self.calls:
            raise protocol_failure(
                "tool_finish_invalid", code="length_limit" if reason == "length" else "protocol",
            )
        if sorted(self.calls) != list(range(len(self.calls))):
            raise protocol_failure("tool_index_sparse")
        ids = set()
        calls = []
        for index, call in sorted(self.calls.items()):
            function = call["function"]
            if not call["id"].strip():
                raise protocol_failure("tool_id_missing", failed_tool_index=index)
            if call["id"] in ids:
                raise protocol_failure("tool_id_duplicate", failed_tool_index=index)
            ids.add(call["id"])
            if function["name"] not in self.offered:
                raise protocol_failure("tool_unknown", failed_tool_index=index)
            try:
                arguments = json.loads(function["arguments"])
            except (ValueError, TypeError) as exc:
                raise protocol_failure(
                    "tool_arguments_json", failed_tool_index=index,
                    tool_argument_error_position=getattr(exc, "pos", 0),
                ) from exc
            if not isinstance(arguments, dict):
                raise protocol_failure("tool_arguments_type", failed_tool_index=index)
            calls.append(call)
        return calls


def tool_characters(tools):
    return len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) if tools else 0
