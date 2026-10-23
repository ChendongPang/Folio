"""Short extraction output keys; stored facts and independent validation stay canonical."""

from copy import deepcopy
from itertools import islice

from jsonschema import Draft202012Validator

EXTRACTION_KEYS = {"summary": "s", "claims": "c", "entities": "e"}
CLAIM_KEYS = {
    "claim_id": "i",
    "block_ids": "b",
    "statement": "t",
    "subject": "s",
    "predicate": "p",
    "value": "v",
    "scope": "o",
    "cardinality": "k",
    "evidence_ids": "e",
}
WIRE_INSTRUCTION = (
    "\nFact-extraction output uses short field names without changing semantics: top-level s=summary, "
    "c=claims, e=entities. In each claim, i=claim_id, b=block_ids, t=statement, s=subject, p=predicate, "
    "v=value, o=scope, k=cardinality, e=evidence_ids. Read input using its original field names. Preserve"
    " every fact's applicability, qualifiers, meaning, entities, and references; short names do not "
    "justify merging or omitting facts. s and e still summarize the complete section body, including "
    "model_proposal. c extracts only non-model_proposal blocks allowed by the b enum; do not extract "
    "proposals as facts or attribute them to other blocks. Return c=[] when its maxItems is 0. Every "
    "extraction remains a candidate awaiting verification. The program sets status=candidate; do not "
    "output status. Subsequent independent validation still decides support. Return short reference IDs "
    "unchanged. fact_extraction_scope explicitly lists fact-eligible and summary-only body blocks. "
    "When a statement combines source-backed requirements with your proposed implementation, extract "
    "only the source-backed statement; keep the proposed design in s/e, never in c. Do not attach a "
    "requirement's evidence IDs to unsupported implementation details."
)


def extraction_context(context):
    """Make provenance visible beside the body, preserving every original block."""
    blocks = context.get("content", {}).get("blocks")
    if not isinstance(blocks, list):
        return context
    return context | {
        "fact_extraction_scope": {
            "fact_block_ids": [
                block["block_id"] for block in blocks if block.get("origin") != "model_proposal"
            ],
            "summary_only_block_ids": [
                block["block_id"] for block in blocks if block.get("origin") == "model_proposal"
            ],
            "rule": "Only fact_block_ids may appear in claims. Summary and entities cover the entire "
            "body, including summary_only_block_ids. Proposed implementation is not a source fact.",
        }
    }


def extraction_scope_schema(schema, context):
    """Limit fact references without removing any prose used for summary/entities."""
    if (
        schema.get("title") != "Extraction"
        or "Claim" not in schema.get("$defs", {})
        or "claims" not in schema.get("properties", {})
        or not isinstance(context.get("content", {}).get("blocks"), list)
    ):
        return schema
    schema = deepcopy(schema)
    field = schema["$defs"]["Claim"]["properties"]["block_ids"]
    previous = field["items"].get("enum")
    allowed = list(
        dict.fromkeys(
            block["block_id"]
            for block in context["content"]["blocks"]
            if block.get("origin") != "model_proposal"
            and (previous is None or block["block_id"] in previous)
        )
    )
    if allowed:
        field["items"]["enum"] = allowed
        field["description"] = (
            "References to fact-eligible body blocks only. model_proposal blocks are available "
            "for summary/entities, not claims; do not combine their proposals with source facts."
        )
    else:
        # An empty enum is invalid JSON Schema and unsupported by some grammars.
        # No Claim can be emitted, while the full body remains available to s/e.
        field["items"].pop("enum", None)
        schema["properties"]["claims"]["maxItems"] = 0
    return schema


def _rename_properties(node, mapping):
    node["properties"] = {mapping.get(k, k): v for k, v in node["properties"].items()}
    if "required" in node:
        node["required"] = [mapping.get(k, k) for k in node["required"]]


def extraction_schema(task, schema):
    """Retain the request's exact limits/enums, changing only output field names."""
    if (
        task != "extract"
        or schema.get("title") != "Extraction"
        or "Claim" not in schema.get("$defs", {})
        or "summary" not in schema.get("properties", {})
    ):
        return schema
    schema = deepcopy(schema)
    _rename_properties(schema, EXTRACTION_KEYS)
    claim = schema["$defs"]["Claim"]
    claim["properties"].pop("status", None)
    if "required" in claim:
        claim["required"] = [key for key in claim["required"] if key != "status"]
    _rename_properties(claim, CLAIM_KEYS)
    return schema


def is_compact_extraction(schema):
    return schema.get("title") == "Extraction" and "s" in schema.get("properties", {})


def _legacy_to_wire(value, schema):
    """Accept existing canonical responses without relaxing the new wire contract."""
    if not set(value).issubset(EXTRACTION_KEYS):
        raise ValueError("Mixed or unknown extraction output fields")
    value = deepcopy(value)
    if isinstance(value.get("claims"), list):
        claims = []
        fields = schema["$defs"]["Claim"]["properties"]
        for claim in value["claims"]:
            if not isinstance(claim, dict):
                claims.append(claim)  # The schema validator rejects the wrong type.
                continue
            if not set(claim).issubset(CLAIM_KEYS.keys() | {"status"}):
                raise ValueError("Mixed or unknown claim output fields")
            if claim.pop("status", "candidate") not in ("candidate", "supported"):
                raise ValueError("Invalid legacy claim status")
            # Canonical responses previously passed through Pydantic defaults
            # (notably cardinality="unspecified"). Keep that compatibility,
            # using only explicit schema defaults, never inventing provenance.
            for full, short in CLAIM_KEYS.items():
                if full not in claim and "default" in fields.get(short, {}):
                    claim[full] = deepcopy(fields[short]["default"])
            claims.append(
                {
                    CLAIM_KEYS[k]: v
                    for k, v in claim.items()
                    # business_payload omits only statically empty fields. Old
                    # responses may still include their exact empty value.
                    if CLAIM_KEYS[k] in fields or v != []
                }
            )
        value["claims"] = claims
    return {
        EXTRACTION_KEYS[k]: v
        for k, v in value.items()
        if EXTRACTION_KEYS[k] in schema["properties"] or v != []
    }


def _schema_error_detail(error):
    """Actionable wire paths and rules without quoting model/source content."""
    known_fields = set(EXTRACTION_KEYS.values()) | set(CLAIM_KEYS.values())
    path = ""
    for part in list(error.absolute_path)[:8]:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += ("." if path else "") + (part if part in known_fields else "field")
    rule = error.validator
    detail = "does not satisfy the declared schema"
    if rule in {"maxLength", "minLength", "maxItems", "minItems"}:
        operator = "<=" if rule.startswith("max") else ">="
        unit = "characters" if rule.endswith("Length") else "items"
        detail = f"expected {operator} {error.validator_value} {unit}; actual {len(error.instance)}"
    elif rule == "enum":
        reference = list(error.absolute_path)[-2] if len(error.absolute_path) > 2 else ""
        kind = {"e": "evidence references", "b": "fact-eligible block references"}.get(
            reference, "values"
        )
        detail = f"expected one of {len(error.validator_value)} allowed {kind} in the schema"
        if reference == "b":
            detail += "; exclude model_proposal details from claims, retain them in summary/entities"
    elif rule == "type":
        expected = error.validator_value
        detail = f"expected {expected}; actual {type(error.instance).__name__}"
    elif rule == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        detail = "missing required fields: " + ", ".join(
            key if key in known_fields else "field" for key in missing[:10]
        )
    elif rule == "additionalProperties":
        detail = "unexpected fields are not allowed"
    return f"{path or '$'}: {rule}; {detail}"[:240]


def restore_extraction(value, schema):
    """Restore names before sfref translation; only validation may promote status."""
    if not is_compact_extraction(schema):
        return value
    if isinstance(value, dict) and any(k in value for k in EXTRACTION_KEYS):
        value = _legacy_to_wire(value, schema)
    errors = list(islice(Draft202012Validator(schema).iter_errors(value), 9))
    if errors:
        details = "; ".join(_schema_error_detail(error) for error in errors[:8])
        if len(errors) > 8:
            details += "; additional schema errors omitted"
        raise ValueError("Invalid compact extraction response: " + details)
    names = {v: k for k, v in EXTRACTION_KEYS.items()}
    result = {names[k]: v for k, v in value.items()}
    if "claims" in result:
        names = {v: k for k, v in CLAIM_KEYS.items()}
        result["claims"] = [
            {**{names[k]: v for k, v in claim.items()}, "status": "candidate"}
            for claim in result["claims"]
        ]
    return result
