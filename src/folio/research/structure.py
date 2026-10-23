"""Lightweight navigation ranges over the unchanged canonical source blocks."""

from __future__ import annotations

import hashlib
import json
import re

from folio.research.formats import READER_VERSION, SourceDocument

_LIGHT_METADATA = {
    "size", "characters", "block_count", "page_count", "slide_count", "width", "height",
    "needs_ocr", "unread_pages", "unread_visuals", "needs_conversion", "formula_count",
    "outline_status", "outline_ordered", "unread_visual_pages", "unread_sheets", "unread_slides",
}


def _node(format_name, kind, locator, title, level, start, end, *, parent_id=None, metadata=None):
    identity = json.dumps([format_name, kind, locator], ensure_ascii=False, separators=(",", ":"))
    values = dict(metadata or {})
    if len(title) > 1000:
        values["title_truncated"] = True
    return {
        "node_id": "node_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        "parent_id": parent_id, "kind": kind, "title": title[:1000], "level": level,
        "locator": locator, "range": {"start_block": start, "end_block": end}, "metadata": values,
    }


def _select(node, positions):
    """Preserve exact canonical block membership for appended extraction blocks."""
    positions = sorted(set(positions))
    if not positions:
        node["range"]["end_block"] = node["range"]["start_block"]
        node.pop("block_indices", None)
        return node
    start, end = positions[0], positions[-1] + 1
    node["range"] = {"start_block": start, "end_block": end}
    if len(positions) != end - start:
        node["block_indices"] = positions
    else:
        node.pop("block_indices", None)
    return node


def _positions(node):
    return node.get("block_indices", range(node["range"]["start_block"], node["range"]["end_block"]))


def _heading_nodes(document, root):
    nodes, stack = [], []
    format_name = document.metadata["format"]
    original_positions = [index for index, block in enumerate(document.blocks) if not block.metadata.get("extraction")]
    original_end = original_positions[-1] + 1 if original_positions else 0
    for index, block in enumerate(document.blocks):
        if block.kind != "heading" or block.metadata.get("extraction"):
            continue
        level = block.metadata.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level < 1:
            continue
        while stack and stack[-1]["level"] >= level:
            stack.pop()["range"]["end_block"] = index
        title = block.text.strip()
        if format_name == "md":
            title = re.sub(r"^#{1,6}[ \t]+", "", title)
            title = re.sub(r"[ \t]+#+[ \t]*$", "", title)
        node = _node(format_name, "section", block.locator, title, level, index, original_end,
                     parent_id=stack[-1]["node_id"] if stack else root["node_id"],
                     metadata={"range_precision": "block"})
        nodes.append(node)
        stack.append(node)
    if len(original_positions) != original_end:
        for node in nodes:
            _select(node, (index for index in original_positions if node["range"]["start_block"] <= index < node["range"]["end_block"]))
    return nodes


def _group_positions(document, key, count):
    positions = {}
    for index, block in enumerate(document.blocks):
        number = block.metadata.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count:
            positions.setdefault(number, []).append(index)
    return positions


def _group_ranges(document, key, count, *, positions=None):
    """Include empty pages/slides at the correct boundary between readable blocks."""
    positions = _group_positions(document, key, count) if positions is None else positions
    next_block = len(document.blocks)
    ranges = {}
    for number in range(count, 0, -1):
        selected = positions.get(number, [])
        start = selected[0] if selected else next_block
        ranges[number] = (start, selected[-1] + 1 if selected else start)
        next_block = start
    return ranges


def _pdf_nodes(document, root, warnings):
    count = document.metadata.get("page_count", 0)
    positions = _group_positions(document, "page", count)
    pages = _group_ranges(document, "page", count, positions=positions)
    outline = document.metadata.get("outline", [])
    reliable = document.metadata.get("outline_status") == "available" and document.metadata.get("outline_ordered", True)
    nodes = []
    if outline:
        following_sections, stack = {}, []
        for index, item in enumerate(outline):
            while stack and outline[stack[-1]]["level"] >= item["level"]:
                following_sections[stack.pop()] = item
            stack.append(index)
        by_path = {}
        for index, item in enumerate(outline):
            page = item["page"]
            if page not in pages:
                continue
            start, page_end = pages[page]
            end = page_end
            end_page = page
            boundary_page_overlap = False
            if reliable:
                following = following_sections.get(index)
                if following is None:
                    end, end_page = len(document.blocks), count
                else:
                    # A next chapter can begin halfway down its destination page.
                    # Keeping the boundary page avoids silently omitting the end
                    # of this chapter when only page-level locations are known.
                    end, end_page = pages[following["page"]][1], following["page"]
                    boundary_page_overlap = True
            path = tuple(item["bookmark_path"])
            parent = by_path.get(tuple(item.get("parent_bookmark_path") or ()))
            node = _node("pdf", "section" if reliable else "bookmark", "bookmark " + ".".join(map(str, path)),
                         item["title"], item["level"], start, end,
                         parent_id=parent["node_id"] if parent else root["node_id"],
                         metadata={"page": page, "end_page": end_page, "range_precision": "page",
                                   "boundary_page_overlap": boundary_page_overlap,
                                   "destination_type": item.get("destination_type", ""),
                                   **({"title_truncated": True} if item.get("title_truncated") else {}),
                                   **({"top": item["top"]} if "top" in item else {})})
            _select(node, (position for number in range(page, end_page + 1) for position in positions.get(number, [])))
            nodes.append(node)
            by_path[path] = node
        warnings.append('PDF section locations come from actual bookmarks and are read by page. Boundary pages may contain adjacent sections and are not exact paragraph boundaries.')
    for page, (start, end) in sorted(pages.items()):
        node = _node("pdf", "page", f"page {page}", f'Page {page}', 1, start, end,
                     parent_id=root["node_id"], metadata={"page": page, "range_precision": "page",
                     "needs_ocr": page in document.metadata.get("unread_pages", [])})
        nodes.append(_select(node, positions.get(page, [])))
    if not outline:
        warnings.append('The PDF has no usable bookmark outline. Navigation uses actual page numbers without guessing sections from body text or font size.')
    nodes.sort(key=lambda node: (node["metadata"]["page"], node["kind"] == "page", node["level"]))
    return nodes, "outline" if reliable and outline else "outline_navigation_only" if outline else "no_outline"


def _extraction_nodes(document, root, warnings):
    """Office render pages cannot safely be assigned to original sections/sheets."""
    by_page = {}
    for index, block in enumerate(document.blocks):
        if block.metadata.get("extraction"):
            by_page.setdefault(block.metadata.get("page", 1), []).append(index)
    nodes = []
    for page, positions in sorted(by_page.items()):
        node = _node(document.metadata["format"], "page", f"extraction page {page}", f'Converted page {page}', 1,
                     positions[0], positions[-1] + 1, parent_id=root["node_id"],
                     metadata={"page": page, "range_precision": "extraction_page", "requires_extraction_review": True})
        nodes.append(_select(node, positions))
    if nodes:
        warnings.append('Office OCR pages are navigated separately after conversion. They have not been reliably mapped to original sections, sheets, or slides; their text still requires verification.')
    return nodes


def build_structure(document: SourceDocument) -> dict:
    """Build bounded structure without copying body text into the navigation file.

    Every range is a zero-based, half-open envelope in ``document.blocks``.
    ``block_indices`` takes precedence if blocks are not contiguous (e.g. OCR
    appended after original text). IDs are scoped to the source file and based
    on canonical positions, not title text.
    ``complete`` retains the source readability state; it does not assert that a
    document without a real outline has inferred semantic sections.
    """
    format_name = document.metadata["format"]
    root = _node(format_name, "document", "document", 'Full text', 0, 0, len(document.blocks))
    nodes, warnings = [root], list(document.warnings)
    status = "no_outline"
    if format_name in {"docx", "md", "txt"}:
        headings = _heading_nodes(document, root)
        nodes.extend(headings)
        status = "outline" if headings else "no_outline"
    elif format_name == "pdf":
        pages, status = _pdf_nodes(document, root, warnings)
        nodes.extend(pages)
    elif format_name == "xlsx":
        cursor = 0
        sheet_positions = {}
        for index, block in enumerate(document.blocks):
            sheet_positions.setdefault(block.metadata.get("sheet"), []).append(index)
        for number, sheet in enumerate(document.metadata.get("sheets", []), 1):
            positions = sheet_positions.get(sheet["name"], [])
            start = positions[0] if positions else cursor
            end = positions[-1] + 1 if positions else start
            cursor = end
            info = {key: sheet[key] for key in (
                "name", "rows", "columns", "data_cells", "range_cells", "hidden",
                "headers", "headers_truncated", "header_row", "header_basis",
                "merged_cells", "merged_cells_total", "merged_cells_truncated", "unread_visual_count",
            ) if key in sheet}
            info["header_basis"] = "first_nonempty_row"
            info["range_precision"] = "block"
            node = _node("xlsx", "sheet", f"sheet {number}", sheet["name"], 1, start, end,
                         parent_id=root["node_id"], metadata=info)
            nodes.append(_select(node, positions))
            if sheet.get("merged_cells_truncated"):
                warnings.append(f"The merged-cell list for sheet {sheet['name']} is limited. Body text is still read using original row ranges.")
        status = "sheets"
    elif format_name == "pptx":
        count = document.metadata.get("slide_count", 0)
        positions = _group_positions(document, "slide", count)
        ranges = _group_ranges(document, "slide", count, positions=positions)
        for slide in document.metadata.get("slides", []):
            number = slide["slide"]
            start, end = ranges.get(number, (len(document.blocks), len(document.blocks)))
            node = _node("pptx", "slide", f"slide {number}", slide.get("title") or f'Slide {number}', 1, start, end,
                         parent_id=root["node_id"], metadata={"slide": number, "range_precision": "block"})
            nodes.append(_select(node, positions.get(number, [])))
        status = "slides"
    else:
        count = document.metadata.get("page_count", 1)
        positions = _group_positions(document, "page", count)
        ranges = _group_ranges(document, "page", count, positions=positions)
        for number in range(1, count + 1):
            start, end = ranges[number]
            node = _node(format_name, "image", f"frame {number}", 'Image' if count == 1 else f'Frame {number}', 1, start, end,
                         parent_id=root["node_id"], metadata={"frame": number, "width": document.metadata.get("width"),
                         "height": document.metadata.get("height"), "needs_ocr": document.metadata.get("needs_ocr", True)})
            nodes.append(_select(node, positions.get(number, [])))
        status = "images"
    if format_name in {"docx", "xlsx", "pptx"}:
        nodes.extend(_extraction_nodes(document, root, warnings))
    by_id = {node["node_id"]: node for node in nodes}
    for node in sorted(nodes, key=lambda item: item["level"], reverse=True):
        if parent := by_id.get(node["parent_id"]):
            if "block_indices" in parent or not (
                parent["range"]["start_block"] <= node["range"]["start_block"] <= node["range"]["end_block"] <= parent["range"]["end_block"]
            ):
                _select(parent, [*_positions(parent), *_positions(node)])
            if "end_page" in parent["metadata"]:
                parent["metadata"]["end_page"] = max(parent["metadata"]["end_page"], node["metadata"].get("end_page", 0))
    metadata = {key: value for key, value in document.metadata.items() if key in _LIGHT_METADATA}
    metadata["block_count"] = len(document.blocks)
    return {
        "version": 1, "source_version": document.source_version,
        "reader_version": document.metadata.get("reader_version", READER_VERSION),
        "format": format_name, "metadata": metadata, "nodes": nodes,
        "warnings": list(dict.fromkeys(warnings)),
        "complete": document.metadata.get("complete", True), "structure_status": status,
    }
