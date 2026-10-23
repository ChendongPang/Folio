"""Missing citations apply to sourced facts, not permitted red proposals."""

from copy import deepcopy

from folio.models import LocalReview, Review


def citation_blocks(context):
    """Return actual page-local fact anchors, or None for index/note-only reads."""
    if "content" in context and "section" in context:
        sections = [{"section_id": context["section"]["section_id"], "content": context["content"]}]
    elif "sections" in context and context.get("review_scope") != "cross_section_consistency":
        sections = context["sections"]
    else:
        return None
    if any(not isinstance(s.get("content", {}).get("blocks"), list) for s in sections):
        return None
    return {
        section["section_id"]: {
            block["block_id"]
            for block in section["content"]["blocks"]
            # Legacy persisted blocks without origin use the model's evidence default.
            if block.get("block_id") and block.get("origin", "evidence") != "model_proposal"
        }
        for section in sections
    }


def citation_review_schema(task, schema, context):
    if task not in {"validate", "review_work"}:
        return schema
    blocks = citation_blocks(context)
    if blocks is None or "Finding" not in schema.get("$defs", {}):
        return schema
    result = deepcopy(schema)
    finding = result["$defs"]["Finding"]
    citation = deepcopy(finding)
    rules = finding["properties"]["rule"]["enum"]
    finding["properties"]["rule"]["enum"] = [r for r in rules if r != "citation_required"]
    finding["required"] = list(dict.fromkeys([*finding["required"], "rule"]))
    anchors = [
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
    if anchors:
        citation["properties"]["rule"] = {"type": "string", "const": "citation_required"}
        citation["properties"]["block_refs"].update(
            minItems=1, items={"$ref": "#/$defs/CitationFindingBlockRef"}
        )
        citation["required"] = list(dict.fromkeys([*citation["required"], "rule", "block_refs"]))
        citation["description"] = (
            "Use only for missing citations on actual evidence-backed body text. Red model_proposal content does not require a citation. Report real conflicts, fabricated facts and coverage problems under their appropriate rule; never relabel them to evade this constraint."
        )
        result["$defs"]["CitationFinding"] = citation
        result["$defs"]["CitationFindingBlockRef"] = {"anyOf": anchors}
        result["properties"]["findings"]["items"] = {
            "anyOf": [{"$ref": "#/$defs/Finding"}, {"$ref": "#/$defs/CitationFinding"}]
        }
    return result


def validate_citation_review(parsed, context, *, task=None):
    if task is not None and task not in {"validate", "review_work"}:
        return
    if not isinstance(parsed, (LocalReview, Review)):
        return
    blocks = citation_blocks(context)
    if blocks is None:
        return
    for finding in parsed.findings:
        if finding.rule != "citation_required":
            continue
        if not finding.block_refs or any(
            ref.section_id not in finding.section_ids
            or ref.block_id not in blocks.get(ref.section_id, set())
            for ref in finding.block_refs
        ):
            raise ValueError(
                "citation_required must reference actual non-model_proposal blocks in their own sections. Red proposals are not erroneous merely because they lack citations. Still check real conflicts, fabricated facts and coverage issues under the appropriate rules; do not change rule merely to evade validation."
            )
