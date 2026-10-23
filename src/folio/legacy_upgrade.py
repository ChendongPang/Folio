"""Read archived planning contracts without retaining them in current model schemas.

These adapters belong exclusively at persisted-data boundaries. They never run
on fresh provider responses and never rewrite original files or checkpoints.
"""

from copy import deepcopy
from pathlib import Path

_MAPPING_FIELDS = {
    "outline_refs", "template_slot_id", "template_table_ref", "mapping_reason",
}


def upgrade_proposal(data: dict) -> dict:
    result = deepcopy(data)
    result.pop("unused_template_slots", None)
    for item in result.get("sections", []):
        for field in _MAPPING_FIELDS | {"expected_table_headers"}:
            item.pop(field, None)
    return result


def upgrade_plan(data: dict) -> dict:
    result = deepcopy(data)
    result.pop("planning_method", None)
    result.pop("unused_template_slots", None)
    for item in result.get("sections", []):
        if "outline_node_id" in item and "node_id" not in item:
            # A single old requirement block could be shared by many chapters.
            # Persisted section IDs are unique and must stay linked to all states.
            item["node_id"] = item["section_id"]
        for field in _MAPPING_FIELDS | {"outline_node_id", "origin"}:
            item.pop(field, None)
    return result


def upgrade_job_input(data: dict) -> dict:
    result = deepcopy(data)
    legacy_path = result.pop("outline_path", None)
    result.pop("template_path", None)
    if legacy_path and not result.get("user_request") and not result.get("requirements"):
        # Old CLI jobs used a separate requirements file. Recover its actual
        # content once for the read view rather than inventing a replacement.
        path = Path(legacy_path)
        if path.is_file():
            from folio.research.formats import read_source

            result["user_request"] = "\n".join(block.text for block in read_source(path).blocks)
        else:
            # Completed artifacts remain viewable even when an old input was
            # removed. Do not invent requirements from the document title.
            result["user_request"] = ""
    return result
