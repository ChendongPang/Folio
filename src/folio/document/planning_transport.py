"""Compact directory planning requests with no synthetic source references."""

import json
from copy import deepcopy

PLANNING_TASKS = {"plan", "repair_plan", "revise_plan"}


def planning_text(payload):
    """Retain the request and prepared source metadata as ordinary JSON data."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def planning_schema(schema, payload):
    """Bound explanatory output without limiting the needed business sections."""
    schema = deepcopy(schema)
    concise = payload.get("concise_plan_retry", False)
    properties = schema.get("$defs", {}).get("PlanItem", {}).get("properties", {})
    if "instruction" in properties:
        properties["instruction"]["maxLength"] = 80 if concise else 160
    if "PlanItem" in schema.get("$defs", {}):
        required = schema["$defs"]["PlanItem"].setdefault("required", [])
        for name in ("instruction", "content_mode"):
            if name not in required:
                required.append(name)
    if "notes" in schema.get("properties", {}):
        schema["properties"]["notes"]["maxItems"] = 2 if concise else 4
        schema["properties"]["notes"]["items"]["maxLength"] = 120
    if "document_title" in schema.get("properties", {}):
        required = schema.setdefault("required", [])
        if "document_title" not in required:
            required.append("document_title")
    return schema


def planning_payload(context):
    """Add exact repair positions while leaving business context and node IDs intact."""
    payload = deepcopy(context)
    rejected = deepcopy(context.get("rejected_proposal"))
    if isinstance(rejected, dict):
        payload["rejected_proposal"] = rejected
        if isinstance(rejected.get("sections"), list):
            rejected["sections"] = [
                item | {"edit_path": f"/sections/{index}"} if isinstance(item, dict) else item
                for index, item in enumerate(rejected["sections"])
            ]
    return payload
