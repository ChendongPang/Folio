"""Document-facing text and layout, separate from the internal evidence ledger."""

import re

from docx.oxml.ns import qn

from folio.models import Block, SectionContent

PROPOSAL_COLOR = "FF0000"
INTERNAL_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])(?:ev_[a-fA-F0-9]{8,}|sfref\d+|model_proposal)(?![A-Za-z0-9_])"
)
AUDIT_HEADERS = {"证据来源", "证据id", "证据编号", "依据id", "来源id", "evidence source", "evidence id", "evidence ids", "source id", "source ids", "reference id"}


def missing_provenance(block: Block) -> bool:
    """Headings and empty table cells assert no business facts."""
    if block.origin != "evidence" or block.evidence_ids or block.kind == "heading":
        return False
    if block.kind == "table":
        return any(cell.strip() for row in block.rows[1:] for cell in row)
    return bool(block.text.strip())


def provenance_summary(states) -> dict:
    """Reference integrity is not a claim that the cited text proves every sentence."""
    result = {
        "total_blocks": 0,
        "proposal_blocks": 0,
        "untraced_blocks": [],
        "invalid_references": [],
    }
    for state in states:
        available = {e.evidence_id for e in state.evidence}
        for block in state.content.blocks:
            result["total_blocks"] += 1
            result["proposal_blocks"] += block.origin == "model_proposal"
            location = {"section_id": state.section_id, "block_id": block.block_id}
            if missing_provenance(block):
                result["untraced_blocks"].append(location)
            invalid = sorted(set(block.evidence_ids) - available)
            if invalid:
                result["invalid_references"].append(location | {"evidence_ids": invalid})
    return result


def prose_paragraphs(text: str) -> list[str]:
    """Unwrap Markdown and split explicit sequential items, never decimal/version numbers."""
    text = re.sub(r"^(?:【模型建议】|\[Model proposal\])\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        matches = list(re.finditer(r"(?<![A-Za-z0-9_.])([1-9]\d*)[.、．](?!\d)\s*", line))
        # A single numeric mention is not enough to infer a list. Require a sequence.
        starts = set()
        for first, second in zip(matches, matches[1:], strict=False):
            if int(second[1]) == int(first[1]) + 1:
                starts.update((first.start(), second.start()))
        cuts = sorted({0, *starts, len(line)})
        result.extend(
            line[a:b].strip() for a, b in zip(cuts, cuts[1:], strict=False) if line[a:b].strip()
        )
    return result


def normalize_content(content: SectionContent, section=None) -> SectionContent:
    """Normalize presentation before extraction so block references remain consistent."""
    normalized = content.model_copy(deep=True)
    normalized.blocks = []
    reserved = {b.block_id for b in content.blocks}
    expanded = {}
    for block_index, block in enumerate(content.blocks):
        if block.kind == "heading" and section is not None:
            from folio.document.typography import clean_heading

            if clean_heading(block.text) == clean_heading(section.title):
                continue  # The renderer already emits the confirmed DocPlan heading.
        if block.kind != "paragraph":
            normalized.blocks.append(block.model_copy(deep=True))
            expanded[block.block_id] = [block.block_id]
            continue
        paragraphs = prose_paragraphs(block.text)
        remaining = len(content.blocks) - block_index - 1
        if len(normalized.blocks) + len(paragraphs) + remaining > 100:
            # Keep a valid persisted SectionContent for very long lists. Both Word
            # renderers still expand the lines into paragraphs without a block limit.
            paragraphs = [block.text]
        expanded[block.block_id] = []
        for index, text in enumerate(paragraphs):
            identifier = block.block_id
            if index:
                serial = index
                identifier = f"{block.block_id[:80]}_p{serial}"
                while identifier in reserved:
                    serial += 1
                    identifier = f"{block.block_id[:80]}_p{serial}"
                reserved.add(identifier)
            normalized.blocks.append(
                block.model_copy(update={"block_id": identifier, "text": text})
            )
            expanded[block.block_id].append(identifier)
    for visual in normalized.visuals:
        visual.source_block_ids = list(
            dict.fromkeys(
                child
                for original in visual.source_block_ids
                for child in expanded.get(original, [])
            )
        )
        if visual.after_block_id in expanded and expanded[visual.after_block_id]:
            visual.after_block_id = expanded[visual.after_block_id][-1]
        elif visual.after_block_id not in {b.block_id for b in normalized.blocks}:
            # A redundant generated heading was removed; keep the figure after its own sources.
            visual.after_block_id = visual.source_block_ids[-1]
    return normalized


def validate_document_content(content: SectionContent, reference_ids=()) -> None:
    """Reject internal citations in prose; structured evidence_ids stay in the backend."""
    for visual in content.visuals:
        if INTERNAL_REFERENCE.search(visual.caption) or any(
            ref in visual.caption for ref in reference_ids if ref
        ):
            raise ValueError("Figure captions must not contain internal evidence IDs; retain source references internally.")
    for block in content.blocks:
        texts = [block.text, *(cell for row in block.rows for cell in row)]
        if any(
            INTERNAL_REFERENCE.search(text) or any(ref in text for ref in reference_ids if ref)
            for text in texts
        ):
            raise ValueError(
                "Do not expose internal references such as ev_, sfref or model_proposal in document body text or tables. "
                "Put references only in evidence_ids; notes should contain business explanations only."
            )
        if block.kind == "table" and any(c.strip().casefold() in AUDIT_HEADERS for c in block.rows[0]):
            raise ValueError("Do not add evidence-source or evidence-ID columns to the deliverable; retain audit information internally.")
        if any(re.search(r"【模型建议】|\[Model proposal\]", text, re.I) for text in texts):
            raise ValueError(
                "Do not write model-proposal labels in body text; set origin=model_proposal and formatting will highlight it in red."
            )


def color_proposal(element, block: Block):
    """Only color changes; inherited document fonts, sizes and spacing remain intact."""
    if block.origin != "model_proposal":
        return
    from folio.document.typography import prop

    for run in element.xpath(".//w:r"):
        color = prop(run.get_or_add_rPr(), "color", val=PROPOSAL_COLOR)
        for name in ("themeColor", "themeTint", "themeShade"):
            color.attrib.pop(qn("w:" + name), None)
