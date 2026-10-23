"""Explicit native call envelopes for deterministic model fixtures."""

import json
from uuid import uuid4

from folio.runtime.base import RuntimeResult


def native_call(name, arguments, *, call_id=None):
    return RuntimeResult("", tool_calls=[{
        "id": call_id or "call_" + uuid4().hex,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }])


def research_call(tool, arguments=None, **metadata):
    arguments = dict(arguments or {})
    if tool == "sandbox.exec":
        arguments.setdefault("operation", "python")
    if tool == "artifact.save":
        arguments.setdefault("operation", "register")
    return native_call(tool.replace(".", "_"), arguments | {
        "summary": "受控测试研究步骤", **metadata,
    })


def action_call(action, **arguments):
    return native_call(action, {"summary": "受控测试下一步", **arguments})
