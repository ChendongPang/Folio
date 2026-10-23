"""Keep confirmed table labels in code; the model supplies the actual cell values."""

import re
from copy import deepcopy

from folio.runtime.pending_metadata import validate_pending_metadata


def _fresh_without_evidence(task, context):
    return (
        task == "generate"
        and "current" not in context
        and context.get("evidence") == []
        and context.get("model_completion_allowed") is True
    )


def _proposal_only_repair(task, context):
    """A repair cannot discover facts when its entire input is unsupported design."""
    current = context.get("current")
    if task != "repair" or context.get("evidence") != [] or not isinstance(current, dict):
        return False
    blocks = [b for b in current.get("blocks", []) if b.get("kind", "paragraph") != "heading"]
    return bool(blocks) and all(
        b.get("origin") == "model_proposal" and not b.get("evidence_ids") for b in blocks
    )


def _validate_repair_provenance(context, value):
    current = context.get("current")
    if not isinstance(current, dict):
        return
    previous = {b.get("block_id"): b for b in current.get("blocks", [])}
    available = {e["evidence_id"] for e in context.get("evidence", [])}
    blocks = list(value.get("blocks", []))
    if isinstance(value.get("primary_table"), dict):
        blocks.append(value["primary_table"])
    for block in blocks:
        if block.get("kind", "paragraph") == "heading" or block.get("origin") != "evidence":
            continue
        old = previous.get(block.get("block_id"))
        unchanged_untraced = (
            old is not None
            and old.get("origin") == "evidence"
            and not old.get("evidence_ids")
            and all(block.get(k) == old.get(k) for k in ("kind", "text", "rows"))
        )
        if unchanged_untraced:
            continue  # Existing metadata/legacy issues remain for the normal source audit.
        refs = set(block.get("evidence_ids", []))
        if not refs or not refs <= available:
            raise ValueError(
                f"Repair block {block.get('block_id')!r} cannot become an evidence fact "
                "without valid evidence_ids from this request; preserve the original "
                "model_proposal provenance or supply the actual supporting citations"
            )


def validate_writing_provenance(task, context, value):
    """Reject false provenance on new or repaired prose; never reclassify it."""
    if task in {"generate", "repair"}:
        validate_pending_metadata(value)
    if task == "repair":
        _validate_repair_provenance(context, value)
    if not (_fresh_without_evidence(task, context) or _proposal_only_repair(task, context)):
        return
    blocks = list(value.get("blocks", []))
    if isinstance(value.get("primary_table"), dict):
        blocks.append(value["primary_table"])
    for block in blocks:
        if block.get("evidence_ids"):
            raise ValueError("Fresh writing without evidence cannot cite unavailable sources")
        if block.get("kind", "paragraph") != "heading" and block.get("origin") != "model_proposal":
            raise ValueError(
                "Fresh writing without evidence must use origin=model_proposal for every "
                "paragraph/table; do not present a proposal as a verified fact"
            )


def remove_duplicate_inline_citations(value, references, available_ids):
    """Remove only citation labels already represented by this block's valid metadata.

    No business wording, source association, or unsupported citation is rewritten.
    Unknown/unattached references remain visible to the strict document validator.
    """
    result = deepcopy(value)
    token = r"(?:sfref\d+|ev_[a-fA-F0-9]{8,})"
    citation = re.compile(r"\[(" + token + r"(?:[\s,，;；]+" + token + r")*)\]")
    bare_citation = re.compile(token + r"(?:[\s,，;；]+" + token + r")*")
    allowed = set(available_ids)
    for block in result.get("blocks", []):
        backed = set(block.get("evidence_ids", [])) & allowed

        def citation_cell(text, backed=backed):
            text = text.strip()
            if not (bare_citation.fullmatch(text) or citation.fullmatch(text)):
                return False
            return all(references.get(ref, ref) in backed for ref in re.findall(token, text))

        def clean(text, backed=backed):
            def replace(match):
                ids = re.findall(token, match[1])
                return "" if all(references.get(ref, ref) in backed for ref in ids) else match[0]

            return citation.sub(replace, text)

        if "text" in block:
            block["text"] = clean(block["text"])
        if "rows" in block:
            rows = block["rows"]
            if rows and rows[0]:
                width = len(rows[0])
                # A model may append a citation cell beyond the header's width.
                # Trim only proven, redundant citations; never pad/truncate data.
                if all(
                    len(row) >= width and all(citation_cell(c) for c in row[width:]) for row in rows
                ):
                    rows = [row[:width] for row in rows]
                # Also handle a redundant trailing column present in every row.
                # Keep business headers and all other column positions unchanged.
                if all(len(row) == len(rows[0]) for row in rows):
                    while len(rows[0]) > 1 and all(citation_cell(row[-1]) for row in rows):
                        rows = [row[:-1] for row in rows]
            block["rows"] = [
                ["" if citation_cell(cell) else clean(cell) for cell in row] for row in rows
            ]
    return result


def writing_schema(task, context, schema):
    section = context.get("section", {})
    headers = section.get("expected_table_headers", []) if isinstance(section, dict) else []
    children = context.get("child_sections", [])
    proposal_only = _fresh_without_evidence(task, context) or _proposal_only_repair(task, context)
    current = context.get("current")
    has_visuals = context.get("required_visual_kinds") or (
        current.get("visuals") if isinstance(current, dict) else None
    )
    if task not in {"generate", "repair"} or not (
        headers or children or proposal_only or has_visuals
    ):
        return schema
    result = deepcopy(schema)
    block = result["$defs"]["Block"]
    body = result["properties"]["blocks"]
    if has_visuals:
        paragraph = next(
            branch
            for branch in block["anyOf"]
            if branch["properties"]["kind"].get("const") == "paragraph"
            or "paragraph" in branch["properties"]["kind"].get("enum", [])
        )
        paragraph["properties"]["text"]["description"] = (
            "Final body text preserving business conditions and sources. visuals renders figures using existing "
            "source_block_ids. Do not copy the already referenced preceding text wholesale under a matching "
            "figure heading; figure explanations add only necessary, distinct information."
        )
    if proposal_only:
        for branch in block["anyOf"]:
            props = branch["properties"]
            props["evidence_ids"]["maxItems"] = 0
            if props["kind"].get("const") != "heading":
                props["origin"].pop("enum", None)
                props["origin"].update(const="model_proposal", default="model_proposal")
    if children:
        body["description"] = (
            "Parent-section body: summarize scope, module relationships, and shared cross-module rules. Children "
            "own detailed requirements in child_sections; do not rewrite their fields, steps, metrics, or tables "
            "here. A short overview is usually sufficient, but preserve this section's unique applicability "
            "conditions, negative constraints, interface relationships, and explicitly required figures/tables; "
            "brevity must not drop requirements. The parent's instruction governs the subtree but does not ask "
            "for another complete copy of it in this section. writing_scope specifies this section's "
            "responsibility."
        )
    if not headers:
        return result
    table = next(
        branch for branch in block["anyOf"] if branch["properties"]["kind"].get("const") == "table"
    )
    table = deepcopy(table)
    table["properties"]["block_id"]["const"] = "primary_table"
    rows = table["properties"]["rows"]
    rows.update(
        minItems=1,
        maxItems=99,
        description=(
            "The sole collection of data rows for this section's designated main table, without headers. Column "
            "order must exactly follow expected_table_headers. Integrate new main-table metrics and supplemental "
            "rules here, rather than producing another paraphrased version in blocks. Unknown metadata may remain"
            " blank."
        ),
    )
    rows["items"].update(minItems=len(headers), maxItems=len(headers))
    table["properties"]["text"]["description"] = (
        "Main-table title or explanation. Put any required table title here instead of creating a duplicate "
        "table to add a title."
    )
    result["properties"]["primary_table"] = table
    result["required"] = list(dict.fromkeys(result.get("required", []) + ["primary_table"]))
    # Only the dedicated primary_table omits its header. Other tables are full
    # independent tables; neither matching column labels nor similar wording is
    # enough to decide that their business data can be discarded.
    additional_table = next(
        branch for branch in block["anyOf"] if branch["properties"]["kind"].get("const") == "table"
    )
    additional_table["properties"]["rows"].update(
        description=(
            "Complete contents of an independent supplemental table: its first row must be its own header, "
            "followed by at least one data row. Only primary_table omits its header. Do not repeat the main "
            "table's metric or record set here."
        ),
    )
    additional_table["properties"]["text"].update(
        description=(
            "When a table title is needed, explain the business object, purpose, or applicability that "
            "distinguishes it from the designated main table."
        ),
    )
    # The wire has a separate main table; restoration prepends it to blocks.
    # Reserve its slot now instead of accepting 100 extra blocks then failing
    # SectionContent's existing total limit after a long model response.
    if "maxItems" in body:
        body["maxItems"] = max(0, body["maxItems"] - 1)
    body["description"] = body.get("description", "") + (
        " Necessary body text outside the main table; may be empty. Independent supplemental tables must have"
        " their own headers and cannot restate the designated main table. primary_table counts as one body "
        "block; blocks may use only the remaining slots."
    )
    return result


def restore_primary_table(task, context, value):
    section = context.get("section", {})
    headers = section.get("expected_table_headers", []) if isinstance(section, dict) else []
    if task not in {"generate", "repair"} or not headers:
        return value
    value = deepcopy(value)
    table = value.pop("primary_table", None)
    if not isinstance(table, dict) or table.get("kind") != "table":
        raise ValueError("Required primary_table is missing")
    rows = table.get("rows", [])
    if not rows or any(len(row) != len(headers) for row in rows):
        raise ValueError("Primary table data must match the confirmed column count")
    # Tolerate an exactly repeated header, never guess whether a business row is one.
    table["rows"] = rows if rows[0] == headers else [list(headers), *rows]
    blocks = []
    aliases = {}
    for block in value.get("blocks", []):
        if (
            block.get("kind") == "table"
            and block.get("rows") in (rows, table["rows"])
            and block.get("origin") == table.get("origin")
            and set(block.get("evidence_ids", [])) == set(table.get("evidence_ids", []))
            and (
                block.get("text", "") == table.get("text", "")
                or block.get("text", "") == ""
                or table.get("text", "") == ""
            )
        ):
            if table.get("text", "") == "" and block.get("text", ""):
                table["text"] = block["text"]
            if block.get("block_id") and table.get("block_id"):
                aliases[block["block_id"]] = table["block_id"]
            continue  # Identical data and provenance; retain the only nonempty caption.
        blocks.append(block)
    value["blocks"] = [table, *blocks]
    for item in value.get("record_only_pending", []):
        item["block_id"] = aliases.get(item.get("block_id"), item.get("block_id"))
    for visual in value.get("visuals", []):
        visual["source_block_ids"] = list(
            dict.fromkeys(aliases.get(ref, ref) for ref in visual.get("source_block_ids", []))
        )
        after = visual.get("after_block_id")
        if after in aliases:
            visual["after_block_id"] = aliases[after]
    return value
