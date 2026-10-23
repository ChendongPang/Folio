"""Apply model-selected edits to an unapproved proposal before full validation."""

import copy
import json
from typing import Literal

from pydantic import Field

from folio.models import Model


class PlanEdit(Model):
    op: Literal["add", "replace", "remove"]
    path: str = Field(
        description="JSON Pointer, e.g. /sections/3/instruction; indexes are zero-based."
    )
    value_json: str = Field(
        default="",
        description="JSON-encoded new value, e.g. null, [] or a double-quoted string. Leave empty for remove.",
    )


class PlanRepair(Model):
    edits: list[PlanEdit] = Field(min_length=1, max_length=100)


class DirectoryValidationError(ValueError):
    def __init__(self, message, repair_paths):
        super().__init__(message)
        self.repair_paths = sorted(repair_paths)


def apply_plan_repair(proposal: dict, repair: PlanRepair, allowed_paths=None) -> dict:
    result = copy.deepcopy(proposal)
    for edit in repair.edits:
        if allowed_paths is not None and edit.path not in allowed_paths:
            raise ValueError("Directory repair must only change fields that failed validation")
        if not edit.path.startswith("/"):
            raise ValueError("Directory repair path must be a JSON Pointer")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in edit.path[1:].split("/")]
        if parts[0] not in {"sections", "notes"}:
            raise ValueError("Directory repair must only edit proposal fields")
        parent = result
        try:
            for part in parts[:-1]:
                if isinstance(parent, list):
                    if not part.isdecimal() or int(part) >= len(parent):
                        raise ValueError("Invalid directory repair index")
                    parent = parent[int(part)]
                else:
                    parent = parent[part]
            key = parts[-1]
            value = json.loads(edit.value_json) if edit.op != "remove" else None
            if isinstance(parent, list):
                index = len(parent) if key == "-" and edit.op == "add" else int(key)
                maximum = len(parent) if edit.op == "add" else len(parent) - 1
                if index < 0 or index > maximum:
                    raise ValueError("Invalid directory repair index")
                if edit.op == "add":
                    parent.insert(index, value)
                elif edit.op == "replace":
                    parent[index] = value
                else:
                    del parent[index]
            elif isinstance(parent, dict):
                if edit.op != "add" and key not in parent:
                    raise ValueError("Directory repair target does not exist")
                if edit.op == "remove":
                    del parent[key]
                else:
                    parent[key] = value
            else:
                raise ValueError("Invalid directory repair parent")
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid directory repair path") from exc
    return result
