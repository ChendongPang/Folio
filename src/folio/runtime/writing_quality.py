"""Narrow document-quality guards; detection never silently deletes prose."""


def repeated_figure_paragraphs(value):
    """Locate copied source passages inside their own explicit figure-caption section.

    Ordinary headings, different citations and table/list paraphrases are not
    compared. Requiring multiple substantial paragraphs avoids treating short
    repeated labels as duplicated narrative. The caller asks the writer to fix
    the result rather than guessing which wording or context can be discarded.
    """
    blocks = value.get("blocks", [])
    locations = {b.get("block_id"): (index, b) for index, b in enumerate(blocks)}
    captions = {}
    for visual in value.get("visuals", []):
        if visual.get("caption"):
            captions.setdefault(visual["caption"], []).append(visual)
    findings = []
    for index, heading in enumerate(blocks):
        if heading.get("kind") != "heading":
            continue
        visuals = captions.get(heading.get("text"), [])
        if len(visuals) != 1:
            continue  # An ambiguous caption is not enough to infer a shared scope.
        visual = visuals[0]
        sources = []
        for ref in visual.get("source_block_ids", []):
            source_index, source = locations.get(ref, (index, {}))
            if source_index < index and source.get("kind", "paragraph") == "paragraph":
                sources.append(source)
        duplicates = []
        for candidate in blocks[index + 1 :]:
            if candidate.get("kind") == "heading":
                break  # Nested or sibling business scope belongs to that heading.
            if candidate.get("kind", "paragraph") != "paragraph":
                continue
            text = candidate.get("text", "")
            if len(text.strip()) < 40:
                continue
            for source in sources:
                if (
                    text == source.get("text")
                    and candidate.get("origin") == source.get("origin")
                    and set(candidate.get("evidence_ids", []))
                    == set(source.get("evidence_ids", []))
                ):
                    duplicates.append(
                        {
                            "figure_id": visual.get("figure_id"),
                            "source_block_id": source.get("block_id"),
                            "duplicate_block_id": candidate.get("block_id"),
                        }
                    )
                    break
        if len({item["source_block_id"] for item in duplicates}) >= 2:
            findings.extend(duplicates)
    return findings


def validate_writing_repetition(task, context, value):
    if task not in {"generate", "repair"}:
        return
    duplicates = repeated_figure_paragraphs(value)
    if not duplicates:
        return
    pairs = ", ".join(
        f"{item['duplicate_block_id']} repeats {item['source_block_id']}"
        for item in duplicates[:20]
    )
    raise ValueError(
        "Figure-caption paragraphs repeat their own source passages verbatim: "
        + pairs
        + ". Keep the original business wording and source references, use visuals for the "
        "figure instead of copying its source paragraphs again. Preserve any distinct "
        "conditions or business scope explicitly; do not remove unrelated content."
    )
