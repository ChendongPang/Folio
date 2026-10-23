"""A cross-section diagnosis names both existing sides of a disagreement."""

from copy import deepcopy

from folio.models import Review

CROSS_REVIEW_SCOPE = "cross_section_consistency"


def comparison_blocks(context):
    result = {}
    for section in context.get("sections", []):
        blocks = {bid for claim in section.get("claims", []) for bid in claim.get("block_ids", [])}
        blocks.update(b["block_id"] for b in section.get("model_proposals", []))
        result[section["section_id"]] = blocks
    return result


def cross_review_schema(model, schema, context):
    schema = deepcopy(schema)
    if model is not Review or context.get("review_scope") != CROSS_REVIEW_SCOPE:
        fields = schema.get("$defs", {}).get("Finding", {}).get("properties", {})
        for field in ("comparison_scope", "incompatibility"):
            fields.pop(field, None)
        return schema
    blocks = comparison_blocks(context)
    fields = schema["$defs"]["Finding"]["properties"]
    fields["kind"] = {"type": "string", "const": "conflict"}
    fields["basis"] = {"type": "string", "const": "content"}
    fields["rule"] = {"type": "string", "enum": ["evidence_conflict", "other"]}
    # The deployed grammar compiler rejects uniqueItems. Distinct section IDs
    # remain mandatory in validate_cross_review, including cached model output.
    fields["section_ids"].update(minItems=2)
    fields["block_refs"]["minItems"] = 2
    for field in ("comparison_scope", "incompatibility"):
        fields[field]["minLength"] = 1
        schema["$defs"]["Finding"]["required"].append(field)
    branches = [
        {
            "type": "object",
            "properties": {
                "section_id": {"type": "string", "const": sid},
                "block_id": {"type": "string", "enum": sorted(ids)},
            },
            "required": ["section_id", "block_id"],
            "additionalProperties": False,
        }
        for sid, ids in blocks.items()
        if ids
    ]
    if len(branches) >= 2:
        schema["$defs"]["FindingBlockRef"] = {"anyOf": branches}
    else:
        # With no second indexed statement this page cannot assert a cross-node
        # conflict. Local/body coverage and source checks still run independently.
        schema["properties"]["findings"]["maxItems"] = 0
    evidence = sorted(
        {
            eid
            for s in context.get("sections", [])
            for c in s.get("claims", [])
            for eid in c.get("evidence_ids", [])
        }
    )
    if evidence:
        fields["evidence_ids"]["items"]["enum"] = evidence
    else:
        fields["evidence_ids"]["maxItems"] = 0
    return schema


def validate_cross_review(parsed, context):
    if not isinstance(parsed, Review) or context.get("review_scope") != CROSS_REVIEW_SCOPE:
        return
    blocks = comparison_blocks(context)
    sources = {
        eid
        for s in context.get("sections", [])
        for c in s.get("claims", [])
        for eid in c.get("evidence_ids", [])
    }
    for finding in parsed.findings:
        sections = set(finding.section_ids)
        refs = {(ref.section_id, ref.block_id) for ref in finding.block_refs}
        if (
            not finding.comparison_scope.strip()
            or not finding.incompatibility.strip()
            or finding.kind != "conflict"
            or finding.basis != "content"
            or finding.rule not in {"evidence_conflict", "other"}
            or len(sections) < 2
            or len(sections) != len(finding.section_ids)
            or {sid for sid, _ in refs} != sections
            or any(bid not in blocks.get(sid, set()) for sid, bid in refs)
            or not set(finding.evidence_ids) <= sources
        ):
            raise ValueError(
                "Cross-section conflict must identify existing statements from at least two different sections; local citation/coverage checks belong to node/task review"
            )


def scope_cross_review(context):
    """After capacity splitting, keep complete originals for this page's claims."""
    if context.get("review_scope") != CROSS_REVIEW_SCOPE:
        return context
    sections = []
    for section in context.get("sections", []):
        if "comparison_blocks" not in section:
            sections.append(section)
            continue
        retained = {
            bid for claim in section.get("claims", []) for bid in claim.get("block_ids", [])
        }
        sections.append(
            section
            | {
                "comparison_blocks": [
                    block for block in section["comparison_blocks"] if block["block_id"] in retained
                ]
            }
        )
    return context | {"sections": sections}


def defer_comparison_originals(context):
    """Keep the established complete index; original blocks remain in source audit."""
    if context.get("review_scope") != CROSS_REVIEW_SCOPE or not any(
        section.get("comparison_blocks") for section in context.get("sections", [])
    ):
        return None
    return context | {
        "comparison_originals_deferred_to_drill_down": True,
        "sections": [
            {key: value for key, value in section.items() if key != "comparison_blocks"}
            for section in context["sections"]
        ],
    }
