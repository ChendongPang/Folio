"""Deterministic Markdown navigation over the authoritative structure index.

The Markdown contains only source structure. Parsing status and research advice
belong to the reader response, not to this derived file.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

NAVIGATION_VERSION = 2


def annotate_nodes(index, aliases=None):
    """Assign stable, snapshot-local aliases without changing canonical nodes."""
    result, number = [], 0
    for node in index["nodes"]:
        if node["kind"] == "document":
            short_id = "N000"
        else:
            number += 1
            short_id = f"N{number:03d}"
        if aliases is not None:
            short_id = aliases[node["node_id"]]
        else:
            short_id = node.get("short_id", short_id)
        result.append(node | {"short_id": short_id})
    return result


def resolve_node(index, node_id, aliases=None):
    if not isinstance(node_id, str):
        raise ValueError("node_id must come from this file's structure index")
    for node in annotate_nodes(index, aliases):
        if node_id in (node["node_id"], node["short_id"]):
            return node
    raise ValueError('The structure node does not exist. Inspect the current file to obtain its node_id first')


def _inline(value):
    value = " ".join(str(value).split())
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)


def _column(number):
    result = ""
    while number > 0:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def _node_text(node):
    metadata = node.get("metadata", {})
    kind = node["kind"]
    indent = "  " * max(0, min(node.get("level", 1) - 1, 64))
    title = _inline(node["title"])
    # These labels are generated navigation, never user-authored headings.
    # Rebuild old cached Chinese labels without changing canonical node IDs.
    if kind == "document":
        title = "Full text"
    elif kind == "page" and metadata.get("page"):
        label = "Converted page" if metadata.get("range_precision") == "extraction_page" else "Page"
        title = f"{label} {metadata['page']}"
    details = []
    if kind == "sheet":
        title = 'Worksheet: ' + title
        rows, columns = metadata.get("rows", 0), metadata.get("columns", 0)
        if rows and columns:
            details.append(f'Range: A1:{_column(columns)}{rows}')
        if metadata.get("header_row"):
            details.append(f"First nonempty row: {metadata['header_row']}")
        if metadata.get("headers"):
            headers = "；".join(
                f"{_column(number)}：{_inline(value)}"
                for number, value in enumerate(metadata["headers"], 1)
                if str(value).strip()
            )
            details.append('Candidate headers: ' + headers + (' (some columns)' if metadata.get("headers_truncated") else ""))
    elif kind in {"section", "bookmark"} and "page" in metadata:
        start, end = metadata["page"], metadata.get("end_page", metadata["page"])
        title += f' (page {start}' + (f' through page {end})' if end != start else ")")
    elif kind == "slide":
        title = f"Slide {metadata['slide']}: {title}"
    elif kind == "image" and metadata.get("width") and metadata.get("height"):
        title += f"（{metadata['width']} × {metadata['height']}）"
    result = f"{indent}- {node['short_id']} {title}\n"
    for detail in details:
        result += f"{indent}  - {detail}\n"
    return result


def navigation_entries(index, nodes):
    """Group consecutive PDF pages while keeping all canonical node mappings."""
    entries = []
    for node in nodes:
        if (
            index["format"] == "pdf" and node["kind"] == "page" and entries
            and entries[-1][0][-1]["kind"] == "page"
            and entries[-1][0][-1]["metadata"].get("page", 0) + 1 == node["metadata"].get("page")
            and int(entries[-1][0][-1]["short_id"][1:]) + 1 == int(node["short_id"][1:])
        ):
            entries[-1][0].append(node)
        else:
            entries.append(([node], ""))
    result = []
    for group, _ in entries:
        if len(group) == 1:
            text = _node_text(group[0])
        else:
            first, last = group[0], group[-1]
            text = (
                f"- {first['short_id']}–{last['short_id']}: pages {first['metadata']['page']}–{last['metadata']['page']} (IDs correspond to page numbers)\n"
            )
        result.append((group, text))
    return result


def navigation_header(index, path, selected):
    name = PurePosixPath(path).name
    name = re.sub(r"^[a-fA-F0-9]{32}__", "", name)
    result = f"# {_inline(name)}\n\n"
    if selected is not None:
        result += _node_text(selected)
        if selected["kind"] == "document" and not any(node["kind"] != "document" for node in index["nodes"]):
            count = index.get("metadata", {}).get("block_count", 0)
            if count:
                result += f'  - Body blocks: 1–{count}\n'
    return result


def render_navigation(index, path, aliases=None):
    nodes = annotate_nodes(index, aliases)
    root = next((node for node in nodes if node["kind"] == "document"), None)
    children = [node for node in nodes if node["kind"] != "document"]
    return navigation_header(index, path, root) + "".join(text for _, text in navigation_entries(index, children))
