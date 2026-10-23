"""Typed sparse directory repairs; serialize nested JSON and deletion order in code."""

import json
from copy import deepcopy

from folio.models import PlanProposal


def repair_transport(task, context, schema):
    if task != "repair_plan" or not isinstance(context.get("rejected_proposal"), dict):
        return schema
    proposal = context["rejected_proposal"]
    canonical = PlanProposal.model_json_schema()
    fields = canonical["$defs"]["PlanItem"]["properties"]
    paths = context.get("repair_paths")
    if paths is None:
        paths = [f"/sections/{i}/{key}" for i in range(len(proposal["sections"])) for key in fields]
        paths += ["/sections", "/sections/-"]
    replacements, removable = {}, []
    sections = proposal["sections"]
    for path in paths:
        parts = path.split("/")[1:]
        if parts == ["sections", "-"]:
            continue
        if len(parts) == 3 and parts[0] == "sections" and parts[2] in fields:
            replacements[path] = deepcopy(fields[parts[2]])
        elif len(parts) == 2 and parts[0] == "sections" and parts[1].isdigit():
            index = int(parts[1])
            replacements[path] = {"$ref": "#/$defs/PlanItem"}
            # Removing a parent alone silently reparents its children. Such a
            # structural merge must instead be expressed as a complete sections edit.
            if (
                index + 1 == len(sections)
                or sections[index + 1]["level"] <= sections[index]["level"]
            ):
                removable.append(index)
        elif len(parts) == 1 and parts[0] in canonical["properties"]:
            replacements[path] = deepcopy(canonical["properties"][parts[0]])
    return {
        "type": "object",
        "additionalProperties": False,
        "$defs": deepcopy(canonical["$defs"]),
        "properties": {
            "replacements": {
                "type": "object",
                "properties": replacements,
                "additionalProperties": False,
            },
            "remove_indices": {
                "type": "array",
                "items": {"type": "integer", **({"enum": removable} if removable else {})},
                "maxItems": len(removable),
            },
            "append_sections": {
                "type": "array",
                "items": {"$ref": "#/$defs/PlanItem"},
                "maxItems": 100 if "/sections/-" in paths else 0,
            },
        },
        "required": ["replacements", "remove_indices", "append_sections"],
    }


def restore_repair(value):
    removed = set(value["remove_indices"])
    if len(removed) != len(value["remove_indices"]):
        raise ValueError("Directory removal indices must be unique")

    def deleted(path):
        parts = path.split("/")
        return (
            len(parts) >= 3
            and parts[1] == "sections"
            and parts[2].isdigit()
            and int(parts[2]) in removed
        )

    edits = [
        {"op": "replace", "path": path, "value_json": json.dumps(item, ensure_ascii=False)}
        for path, item in value["replacements"].items()
        if not deleted(path)
    ]
    if "/sections" in value["replacements"] and (
        value["remove_indices"] or value["append_sections"]
    ):
        raise ValueError("Do not combine whole directory replacement and indexed edits")
    # Writing into a node immediately removed by the same repair has no effect.
    # Omit those dead writes; retain every change to surviving nodes.
    edits.extend(
        {"op": "remove", "path": f"/sections/{index}"} for index in sorted(removed, reverse=True)
    )
    edits.extend(
        {"op": "add", "path": "/sections/-", "value_json": json.dumps(node, ensure_ascii=False)}
        for node in value["append_sections"]
    )
    if not edits:
        raise ValueError("Directory repair must contain at least one edit")
    return {"edits": edits}


WIRE_INSTRUCTION = (
    "\nUse the current Schema for repair transport: replacements maps JSON Pointer paths to native JSON "
    "new values. Write strings as strings and arrays as arrays; do not return edits or value_json or "
    "encode JSON a second time. remove_indices contains only original array indices to delete; the "
    "program applies field edits before deleting in descending index order. append_sections contains only"
    " necessary new nodes. Return only necessary changes, omit unchanged fields, and never modify and "
    "delete the same node. Do not delete a parent alone while it has a subtree. When duplicate nodes have"
    " the same business scope, preserve the existing complete subtree and prefer deleting a duplicate "
    "without children rather than arbitrarily turning it into another section."
)
