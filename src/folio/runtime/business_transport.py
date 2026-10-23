"""Lossless wire encoding for business tasks; canonical data stays in the store."""

import json
import re
from copy import deepcopy

from folio.runtime.extraction_transport import extraction_scope_schema

BUSINESS_TASKS = {
    "plan_review",
    "read_review_materials",
    "review_focus",
    "select_context",
    "understand_work",
    "next_action",
    "query",
    "complete_gaps",
    "read_gap_materials",
    "generate",
    "repair",
    "integrate_supplement",
    "extract",
    "validate",
    "review_work",
    "review",
    "drill_down",
}
REFERENCE_FIELDS = {
    "context_ref",
    "selected_refs",
    "risk_refs",
    "source_refs",
    "section_id",
    "section_ids",
    "parent_id",
    "evidence_id",
    "adoption_evidence_id",
    "evidence_ids",
    "evidence_refs",
    "required_project_evidence_ids",
    "existing_evidence_ids",
    "claim_id",
    "claim_ids",
    "block_id",
    "block_ids",
    "fact_block_ids",
    "summary_only_block_ids",
    "remove_block_ids",
    "protected_block_ids",
    "source_block_ids",
    "after_block_id",
    "fact_id",
    "gap_id",
    "proposal_ref",
    "affected_section_ids",
}
WIRE_INSTRUCTIONS = (
    '\nTransport format: {"$columns":[field_names...],"$rows":[[corresponding_values...],...]} represents '
    "an array of row objects, not a document-body table. Read every field in each row according to its "
    'column name. All original text is preserved in full. If a table has "$row_refs":1, a cell '
    '{"$same":i} denotes the complete value in the same column of this table\'s zero-based row i. The '
    "first occurrence is preserved in full. Reuse only the cell value; do not merge source rows. Evidence"
    " IDs and sources remain distinct per row. Short IDs beginning with sfref are only for structured "
    "reference fields such as evidence_ids and must be returned unchanged. Never put them in body text, "
    "table rows, or notes, or use sfref for newly created IDs. Output must still conform to the output "
    "Schema using ordinary JSON objects and arrays; do not return $columns/$rows/$same encoding. Use "
    "compact JSON. Fields the program has determined to be empty are removed from the output Schema; do "
    "not emit them yourself or fill with whitespace."
)


def translate_ids(value, mapping, field=None):
    if isinstance(value, dict):
        return {k: translate_ids(v, mapping, k) for k, v in value.items()}
    if isinstance(value, list):
        return [translate_ids(v, mapping, field) for v in value]
    if isinstance(value, str) and field in REFERENCE_FIELDS:
        return mapping.get(value, value)
    if (
        isinstance(value, str)
        and field in {"format_feedback", "repair_instruction", "description", "reason", "key"}
        and mapping
    ):
        pattern = (
            r"(?<![A-Za-z0-9_])(?:"
            + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
            + r")(?![A-Za-z0-9_])"
        )
        return re.sub(pattern, lambda match: mapping[match[0]], value)
    return value


def pack_rows(value):
    if isinstance(value, dict):
        return {k: pack_rows(v) for k, v in value.items()}
    if isinstance(value, list):
        if (
            len(value) >= 2
            and all(isinstance(row, dict) for row in value)
            and len(value[0]) >= 3
            and all(row.keys() == value[0].keys() for row in value)
        ):
            columns = list(value[0])
            packed = {
                "$columns": columns,
                "$rows": [[pack_rows(row[k]) for k in columns] for row in value],
            }
            return pack_repeated_cells(packed)
        return [pack_rows(v) for v in value]
    return value


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def pack_repeated_cells(table):
    """Reuse exact values in an earlier row, retaining every evidence/source row."""
    rows = table["$rows"]
    # A literal source object must never acquire protocol meaning.
    if any(isinstance(cell, dict) and set(cell) == {"$same"} for row in rows for cell in row):
        return table
    seen = [{} for _ in table["$columns"]]
    shared = []
    for index, row in enumerate(rows):
        encoded = []
        for column, cell in enumerate(row):
            serialized = compact_json(cell)
            previous = seen[column].get(serialized)
            reference = {"$same": previous}
            if previous is not None and len(compact_json(reference)) < len(serialized):
                encoded.append(reference)
            else:
                encoded.append(cell)
                seen[column].setdefault(serialized, index)
        shared.append(encoded)
    candidate = table | {"$rows": shared, "$row_refs": 1}
    return candidate if len(compact_json(candidate)) < len(compact_json(table)) else table


def unpack_rows(value):
    """Inverse used by protocol tests and request inspection, never to infer content."""
    if isinstance(value, dict):
        if set(value) in ({"$columns", "$rows"}, {"$columns", "$rows", "$row_refs"}):
            decoded = []
            for row in value["$rows"]:
                cells = []
                for column, cell in enumerate(row):
                    if (
                        value.get("$row_refs") == 1
                        and isinstance(cell, dict)
                        and set(cell) == {"$same"}
                    ):
                        index = cell["$same"]
                        if type(index) is not int or not 0 <= index < len(decoded):
                            raise ValueError("Row reference must point to an earlier row")
                        cell = deepcopy(decoded[index][column])
                    else:
                        cell = unpack_rows(cell)
                    cells.append(cell)
                decoded.append(cells)
            return [dict(zip(value["$columns"], row, strict=True)) for row in decoded]
        return {k: unpack_rows(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack_rows(v) for v in value]
    return value


def evidence_schema(schema, context):
    """Use the current request's evidence namespace, never other context identifiers."""
    if "evidence" not in context:
        return schema
    schema = deepcopy(schema)
    available = list(dict.fromkeys(e["evidence_id"] for e in context["evidence"]))
    if schema.get("title") == "SupplementIntegration":
        available = list(dict.fromkeys(available + context.get("existing_evidence_ids", [])))
    fields = []

    def collect(definition):
        props = definition.get("properties", {})
        if "evidence_ids" in props:
            fields.append(props["evidence_ids"])
        for branch in ("anyOf", "oneOf", "allOf"):
            for child in definition.get(branch, []):
                collect(child)

    for name in ("BusinessFact", "Block", "Claim"):
        collect(schema.get("$defs", {}).get(name, {}))
    if "evidence_refs" in schema.get("properties", {}):
        fields.append(schema["properties"]["evidence_refs"])
    for field in fields:
        # This also runs during wire encoding. Keep narrower per-edit contracts,
        # such as new retrieved blocks that may cite only this request's sources.
        previous = field["items"].get("enum")
        allowed = [ref for ref in available if previous is None or ref in previous]
        if allowed:
            field["items"]["enum"] = allowed
        else:
            field["items"].pop("enum", None)
            field["maxItems"] = 0
    if not available and "BusinessFact" in schema.get("$defs", {}):
        schema["properties"]["facts"]["maxItems"] = 0
    return schema


def supplement_edit_schema(schema, context):
    """Constrain edit targets before generation; application checks stay authoritative."""
    if schema.get("title") != "SupplementIntegration" or "current" not in context:
        return schema
    schema = deepcopy(schema)
    current = context["current"]
    blocks = current.get("blocks", [])
    protected = set(context.get("protected_block_ids", []))
    figure_anchors = {
        block_id
        for visual in current.get("visuals", [])
        for block_id in [*visual.get("source_block_ids", []), visual.get("after_block_id")]
        if block_id
    }
    editable = [block["block_id"] for block in blocks if block["block_id"] not in protected]
    known = {block["block_id"] for block in blocks}
    new_ids, index = [], 1
    while len(new_ids) < 20:
        value = f"si{index}"
        index += 1
        if value not in known:
            new_ids.append(value)
    retrieved = bool(context.get("integration_item", {}).get("evidence_refresh"))
    current_sources = list(dict.fromkeys(e["evidence_id"] for e in context.get("evidence", [])))
    provenance = (
        (
            "Research has found support for this item. New facts must use origin=evidence and cite exact IDs from"
            " this call's evidence in evidence_ids. Keep parts unanswered by the originals pending."
        )
        if retrieved
        else (
            "New proposed business additions must use origin=model_proposal with evidence_ids=[]."
        )
    )
    branches = []
    for shape in schema["$defs"]["Block"]["anyOf"]:
        if editable:
            existing = deepcopy(shape)
            existing["properties"]["block_id"].update(
                enum=editable,
                description=(
                    "Preserve existing factual blocks verbatim; only accurate citations may be added. Existing proposal "
                    "blocks may be merged. Proposal blocks referenced by figures must retain their original block_id and "
                    "be updated in place, never deleted and recreated under another ID."
                ),
            )
            branches.append(existing)
        if not retrieved or current_sources:
            added = deepcopy(shape)
            fields = added["properties"]
            fields["block_id"].update(
                enum=new_ids,
                description=("Use an unused ID beginning with si for new content. ") + provenance,
            )
            fields["origin"] = {
                "type": "string",
                "const": "evidence" if retrieved else "model_proposal",
            }
            if retrieved:
                fields["evidence_ids"] = {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "enum": current_sources},
                }
            else:
                fields["evidence_ids"] = {
                    "type": "array",
                    "maxItems": 0,
                    "items": {"type": "string"},
                }
            branches.append(added)
    if branches:
        schema["$defs"]["Block"]["anyOf"] = branches
    else:
        # No current blocks or new sources: preserve the question, invent no edit.
        schema["properties"]["updates"]["maxItems"] = 0
    allowed = {
        "remove_block_ids": [
            block["block_id"]
            for block in blocks
            if block.get("origin") == "model_proposal"
            and block.get("kind") != "table"
            and block["block_id"] not in protected
            and block["block_id"] not in figure_anchors
        ],
        "resolved_pending_items": current.get("pending_items", []),
    }
    for field, values in allowed.items():
        definition = schema["properties"][field]
        definition["maxItems"] = min(20, len(values))
        if values:
            definition["items"]["enum"] = values
    return schema


def empty_constant(field):
    if field.get("type") == "array" and field.get("maxItems") == 0:
        return []
    if field.get("type") == "string" and field.get("const") == "":
        return ""
    return None


def omit_empty_fields(schema):
    """Don't ask constrained decoding to generate a statically empty value."""
    if isinstance(schema, list):
        return [omit_empty_fields(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {k: omit_empty_fields(v) for k, v in schema.items()}
    props = result.get("properties", {})
    empty = {k for k, v in props.items() if empty_constant(v) is not None}
    if empty:
        result["properties"] = {k: v for k, v in props.items() if k not in empty}
        if "required" in result:
            result["required"] = [k for k in result["required"] if k not in empty]
    return result


def restore_empty_fields(value, schema):
    """Restore only declared constants; never overwrite an invalid returned value."""
    if not isinstance(value, dict):
        return value
    return {
        k: empty_constant(v)
        for k, v in schema.get("properties", {}).items()
        if empty_constant(v) is not None
    } | value


def material_read_schema(schema, context):
    """Source kind belongs to the catalog, not to a model classification."""
    schema = deepcopy(schema)
    groups = {}
    for item in context.get("catalog", []):
        groups.setdefault(item["kind"], []).append(item["context_ref"])
    for name in ("GapConstraint", "ReviewNote"):
        definition = schema.get("$defs", {}).get(name)
        if not definition or not groups:
            continue
        definition["properties"].pop("source_kind", None)
        definition["required"] = [k for k in definition.get("required", []) if k != "source_kind"]
        definition["properties"]["source_refs"] = {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": len(refs),
                    "items": {"type": "string", "enum": refs},
                }
                for refs in groups.values()
            ]
        }
    return schema


def restore_material_kinds(value, context):
    kinds = {item["context_ref"]: item["kind"] for item in context["catalog"]}
    value = deepcopy(value)
    for name in ("constraints", "notes"):
        for note in value.get(name, []):
            refs = note.get("source_refs", [])
            found = {kinds.get(ref) for ref in refs}
            if not refs or None in found or len(found) != 1:
                raise ValueError("Material note must reference known sources of exactly one kind")
            kind = next(iter(found))
            if "source_kind" in note and note["source_kind"] != kind:
                raise ValueError("Material note cannot change its source kind")
            note["source_kind"] = kind
    return value


def business_payload(context, schema):
    schema = material_read_schema(schema, context)
    schema = omit_empty_fields(evidence_schema(schema, context))
    schema = extraction_scope_schema(schema, context)
    ids = []

    def collect(value, field=None):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, key)
        elif isinstance(value, list):
            for item in value:
                collect(item, field)
        elif isinstance(value, str) and field in REFERENCE_FIELDS and value not in ids:
            ids.append(value)

    collect(context)
    aliases = {}
    index = 1
    for identifier in ids:
        if len(identifier) <= 16:
            continue
        while f"sfref{index}" in ids:
            index += 1
        aliases[identifier] = f"sfref{index}"
        index += 1

    def schema_refs(value, field=None):
        if isinstance(value, dict):
            return {k: schema_refs(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [schema_refs(v, field) for v in value]
        if isinstance(value, str) and field in {"enum", "const"}:
            return aliases.get(value, value)
        return value

    wire = pack_rows(translate_ids(context, aliases))
    return wire, schema_refs(deepcopy(schema)), {v: k for k, v in aliases.items()}
