"""Program measurements and bounded, located samples for style references."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from statistics import median

from docx.oxml import parse_xml
from docx.oxml.ns import qn

from folio.document.reader import heading_level, is_toc, read_docx, visible_text, xpath
from folio.document.typography import resolved_format
from folio.research.formats import SourceBlock, SourceDocument
from folio.research.source_index import cached_source


def _native_extra_paragraphs(parsed, revision_view):
    """Locate native text deliberately excluded from the business reading view."""
    covered = set()
    for block in parsed.blocks:
        paragraphs = [block.element] if block.kind != "table" else xpath(block.element, "./w:tr/w:tc/w:p")
        for paragraph in paragraphs:
            covered.add(paragraph)
            covered.update(paragraph.iter(qn("w:p")))
    hidden = {qn("w:" + name) for name in ({"del", "moveFrom"} if revision_view == "final" else {"ins", "moveTo"})}
    regions = [("body", parsed.document.element.body, None)]
    seen = set()
    for relation in parsed.document.part.rels.values():
        if relation.is_external or not relation.reltype.endswith(("/header", "/footer", "/footnotes", "/endnotes")):
            continue
        part = relation.target_part
        if part.partname not in seen:
            seen.add(part.partname)
            element = getattr(part, "element", None)
            kind = relation.reltype.rsplit("/", 1)[-1].removesuffix("s")
            regions.append((str(part.partname).removeprefix("/word/"), element if element is not None else parse_xml(part.blob), kind))
    result = []
    for region, root, native_kind in regions:
        for index, paragraph in enumerate(root.iter(qn("w:p"))):
            ancestors = list(paragraph.iterancestors())
            if paragraph in covered or any(item in covered or item.tag in hidden for item in ancestors):
                continue
            if not visible_text(paragraph, revision_view).strip():
                continue
            if native_kind:
                kind = native_kind
            elif any(item.tag == qn("w:txbxContent") for item in ancestors):
                kind = "text_box"
            elif is_toc(paragraph, parsed.document) or any(is_toc(item, parsed.document) for item in ancestors[:ancestors.index(root)]):
                kind = "toc"
            else:
                kind = "native_text"
            result.append((f"{region}/native-paragraph[{index}]", paragraph, kind))
            # An outer paragraph's visible_text already includes nested text boxes.
            covered.add(paragraph)
    return result


def read_style_source(path, cache_root=None, revision_view="final"):
    source = cached_source(path, cache_root)
    if path.suffix.lower() != ".docx":
        return source
    parsed = read_docx(path, revision_view)
    order = {element: index for index, element in enumerate(parsed.document.element.body.iter())}
    located = []
    for block in parsed.blocks:
        metadata = {"level": block.level, "heading_path": block.path}
        if block.kind == "table":
            for number, row in enumerate(block.rows, 1):
                located.append((order[block.element], SourceBlock(
                    f"{block.locator}/row[{number}]", " | ".join(row), "table_row",
                    metadata | {"cells": row, "row": number},
                )))
        else:
            located.append((order[block.element], SourceBlock(block.locator, block.text, block.kind, metadata)))
    for index, (locator, paragraph, region) in enumerate(_native_extra_paragraphs(parsed, revision_view)):
        located.append((order.get(paragraph, len(order) + index), SourceBlock(
            locator, visible_text(paragraph, revision_view).strip(), "paragraph", {"style_region": region},
        )))
    blocks = [block for _, block in sorted(located, key=lambda item: item[0])]
    metadata = source.metadata | {"revision_view": revision_view, "characters": sum(len(block.text) for block in blocks), "block_count": len(blocks)}
    return SourceDocument(source.source_version, metadata, blocks, parsed.warnings)


def measure_source(source):
    pdf = source.metadata["format"] == "pdf"
    paragraphs = [len(block.text) for block in source.blocks if block.kind == "paragraph" and block.text]
    table_rows = [block for block in source.blocks if block.kind in {"table_row", "row"}]
    tables = {re.sub(r"/row\[\d+\]$", "", block.locator) for block in table_rows}
    return {
        "version": source.source_version,
        "source_hash": source.source_version,
        "format": source.metadata["format"],
        "heading_counts_by_level": {str(level): count for level, count in sorted(Counter(
            block.metadata.get("level", 1) for block in source.blocks if block.kind == "heading"
        ).items())},
        "paragraph_count": len(paragraphs),
        "paragraph_count_reliable": not pdf,
        "text_block_count": len(paragraphs),
        "median_paragraph_chars": int(median(paragraphs)) if paragraphs else 0,
        "table_count": len(tables),
        "table_count_reliable": not pdf,
        "table_column_counts": sorted({len(block.metadata.get("cells", [])) for block in table_rows if block.metadata.get("cells")}),
        "block_count": len(source.blocks),
        "characters": sum(len(block.text) for block in source.blocks),
        "source_complete": source.metadata.get("complete", True),
    }


def _value(element, name, attribute="val"):
    child = element.find(qn("w:" + name))
    return child.get(qn("w:" + attribute)) if child is not None else None


def paragraph_format(document, element):
    ppr, rpr = resolved_format(document, element)
    result = {}
    for key, attribute in (("font_name", "ascii"), ("east_asia_font", "eastAsia")):
        value = _value(rpr, "rFonts", attribute)
        if value:
            result[key] = value
    size = _value(rpr, "sz")
    if size and size.isdigit():
        result["font_size_pt"] = int(size) / 2
    for key, tag in (("bold", "b"), ("italic", "i")):
        if rpr.find(qn("w:" + tag)) is not None:
            result[key] = _value(rpr, tag) not in {"0", "false", "off"}
    color = _value(rpr, "color")
    if color and re.fullmatch(r"[0-9a-fA-F]{6}", color):
        result["color"] = color.upper()
    alignment = {"both": "justify", "start": "left", "end": "right"}.get(
        _value(ppr, "jc"), _value(ppr, "jc")
    )
    if alignment in {"left", "center", "right", "justify"}:
        result["alignment"] = alignment
    for key, tag, attribute in (
        ("space_before_pt", "spacing", "before"), ("space_after_pt", "spacing", "after"),
        ("first_line_indent_pt", "ind", "firstLine"), ("left_indent_pt", "ind", "left"),
        ("right_indent_pt", "ind", "right"),
    ):
        value = _value(ppr, tag, attribute)
        if value is not None:
            try:
                result[key] = int(value) / 20
            except ValueError:
                pass
    spacing = _value(ppr, "spacing", "line")
    if spacing is not None:
        try:
            rule = {"auto": "multiple", "exact": "exactly", "atLeast": "at_least"}.get(
                _value(ppr, "spacing", "lineRule") or "auto"
            )
            if rule:
                result["line_spacing"] = int(spacing) / (240 if rule == "multiple" else 20)
                result["line_spacing_rule"] = rule
        except ValueError:
            pass
    return result


def _dominant(values):
    """Unknown measurements remain in the denominator, so sparse data cannot win."""
    output, observations = {}, {}
    for key in sorted({key for value in values for key in value}):
        counts = Counter(json.dumps(value[key], sort_keys=True) for value in values if key in value)
        encoded, count = counts.most_common(1)[0]
        observations[key] = {"matched": count, "measured": len(values)}
        if count / len(values) >= 0.6:
            output[key] = json.loads(encoded)
    # The line rule and line value are one measurement, never mix competing rules.
    pairs = Counter((value.get("line_spacing_rule"), value.get("line_spacing")) for value in values)
    pair, count = pairs.most_common(1)[0] if pairs else ((None, None), 0)
    output.pop("line_spacing", None)
    output.pop("line_spacing_rule", None)
    if pair[0] and pair[1] and count / len(values) >= 0.6:
        output.update(line_spacing_rule=pair[0], line_spacing=pair[1])
    return output, observations


def _explicit_cell_paragraph(document, element):
    """Table conditional styles are not resolved here; retain only direct values."""
    measured = paragraph_format(document, element)
    runs = [run for run in xpath(element, "./w:r") if xpath(run, "./w:t")]
    run = max(runs, key=lambda item: len(visible_text(item)), default=None)
    rpr = run.find(qn("w:rPr")) if run is not None else None
    ppr = element.find(qn("w:pPr"))
    allowed = set()
    if rpr is not None:
        for tag, keys in {
            "sz": {"font_size_pt"},
            "b": {"bold"}, "i": {"italic"}, "color": {"color"},
        }.items():
            if rpr.find(qn("w:" + tag)) is not None:
                allowed.update(keys)
        for key, attribute in (("font_name", "ascii"), ("east_asia_font", "eastAsia")):
            if _value(rpr, "rFonts", attribute) is not None:
                allowed.add(key)
    if ppr is not None:
        for key, tag, attribute in (
            ("alignment", "jc", "val"), ("line_spacing", "spacing", "line"),
            ("line_spacing_rule", "spacing", "line"), ("space_before_pt", "spacing", "before"),
            ("space_after_pt", "spacing", "after"), ("first_line_indent_pt", "ind", "firstLine"),
            ("left_indent_pt", "ind", "left"), ("right_indent_pt", "ind", "right"),
        ):
            if _value(ppr, tag, attribute) is not None:
                allowed.add(key)
    return {key: value for key, value in measured.items() if key in allowed}


def _table_border(table):
    """The renderer applies a uniform grid; accept only an observed uniform grid."""
    borders = table.find("./" + qn("w:tblPr") + "/" + qn("w:tblBorders"))
    if borders is None or xpath(table, ".//w:tcPr/w:tcBorders"):
        return {}
    edges = []
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = borders.find(qn("w:" + name))
        if edge is None or edge.get(qn("w:val")) != "single":
            return {}
        color, size = edge.get(qn("w:color")), edge.get(qn("w:sz"))
        if not color or not re.fullmatch(r"[0-9a-fA-F]{6}", color) or not size or not size.isdigit():
            return {}
        edges.append((color.upper(), int(size) / 8))
    if len(set(edges)) != 1:
        return {}
    return {"border_color": edges[0][0], "border_size_pt": edges[0][1]}


def measure_docx_tables(document, parsed, *, max_tables=None, max_cells=None):
    """Measure every confirmed data-table cell unless a caller sets a limit."""
    tables = [block for block in parsed.blocks if block.kind == "table"]
    first_heading = next((block.body_index for block in parsed.blocks if block.kind == "heading"), None)
    eligible, unconfirmed = [], 0
    for block in tables:
        table = block.element
        rows = table.findall(qn("w:tr"))
        before_body = first_heading is not None and block.body_index < first_heading
        if before_body or len(rows) < 2 or len(rows[0].findall(qn("w:tc"))) < 2 or xpath(table, ".//w:gridSpan | .//w:vMerge | ./w:tr/w:tc//w:tbl"):
            unconfirmed += 1
            continue
        explicit_headers = []
        for index, row in enumerate(rows):
            flag = row.find("./" + qn("w:trPr") + "/" + qn("w:tblHeader"))
            if flag is None or flag.get(qn("w:val")) in {"0", "false", "off"}:
                break
            explicit_headers.append(index)
        look = table.find("./" + qn("w:tblPr") + "/" + qn("w:tblLook"))
        first_row = look.get(qn("w:firstRow")) if look is not None else None
        if first_row is None and look is not None:
            try:
                first_row = "1" if int(look.get(qn("w:val"), "0"), 16) & 0x20 else "0"
            except ValueError:
                first_row = "0"
        if not explicit_headers and first_heading is not None and first_row in {"1", "true", "on"}:
            explicit_headers = [0]
        if not explicit_headers or len(explicit_headers) >= len(rows):
            unconfirmed += 1
            continue
        eligible.append((block, rows, explicit_headers))
    indices = range(len(eligible)) if max_tables is None else sorted({round(i * (len(eligible) - 1) / max(1, max_tables - 1)) for i in range(min(max_tables, len(eligible)))})
    header_values, body_values, fills, borders, locations = [], [], [], [], []
    measured_cells, available_cells = 0, sum(len(row.findall(qn("w:tc"))) for _, rows, _ in eligible for row in rows)
    for index in indices:
        if max_cells is not None and measured_cells >= max_cells:
            break
        block, rows, header_rows = eligible[index]
        borders.append(_table_border(block.element))
        locations.append(block.locator)
        for row_index, row in enumerate(rows):
            for cell in row.findall(qn("w:tc")):
                if max_cells is not None and measured_cells >= max_cells:
                    break
                measured_cells += 1
                paragraphs = [item for item in cell.findall(qn("w:p")) if visible_text(item).strip()]
                values = [_explicit_cell_paragraph(document, paragraph) for paragraph in paragraphs] or [{}]
                (header_values if row_index in header_rows else body_values).extend(values)
                if row_index in header_rows:
                    shade = cell.find("./" + qn("w:tcPr") + "/" + qn("w:shd"))
                    fill = shade.get(qn("w:fill")) if shade is not None else None
                    valid = shade is not None and shade.get(qn("w:val"), "clear") in {"clear", "solid"} and not any("theme" in key.lower() for key in shade.attrib)
                    fills.append({"header_fill": fill.upper()} if valid and fill and re.fullmatch(r"[0-9a-fA-F]{6}", fill) else {})
    header, header_coverage = _dominant(header_values)
    body, body_coverage = _dominant(body_values)
    fill, fill_coverage = _dominant(fills)
    # A color/width pair must have been observed together on a whole table.
    counts = Counter(json.dumps(value, sort_keys=True) for value in borders)
    encoded, matched = counts.most_common(1)[0] if counts else ("{}", 0)
    border = json.loads(encoded) if borders and matched / len(borders) >= 0.6 else {}
    return {"header": header, "body": body, **fill, **border}, {
        "tables_total": len(tables), "tables_eligible": len(eligible), "tables_measured": len(locations),
        "cells_available": available_cells, "cells_measured": measured_cells,
        "complete": measured_cells == available_cells and len(locations) == len(eligible),
        "unconfirmed_tables": unconfirmed, "locations": locations,
        "field_coverage": {"header": header_coverage, "body": body_coverage, "fill": fill_coverage},
        "limitations": ["仅采用已确认表头的数据表格；布局表、合并单元格和嵌套表不作为全局样式。", "未解析表格样式的条件继承；无显式参数的字体、底色和边框保持未确认。", "只复用明确且一致的单线网格边框，不将局部或特殊边框改成全表网格。"] if tables else [],
    }


def measure_docx_layout(path, *, revision_view="final", max_paragraphs=None):
    """Only explicit/resolved Office values enter executable layout parameters."""
    from folio.document.style_layout import StyleLayout

    parsed = read_docx(path, revision_view)
    document = parsed.document
    blocks = [block for block in parsed.blocks if block.kind in {"paragraph", "heading"}]
    paragraphs = [block.element for block in blocks]
    first_heading = next((i for i, block in enumerate(blocks) if block.kind == "heading"), 0)
    eligible = paragraphs[first_heading:]
    positions = sorted({round(i * (len(eligible) - 1) / max(1, max_paragraphs - 1)) for i in range(min(len(eligible), max_paragraphs))}) if max_paragraphs is not None and len(eligible) > max_paragraphs else range(len(eligible))
    groups, format_groups = defaultdict(list), defaultdict(list)
    for index in positions:
        element = eligible[index]
        level = heading_level(element, document)
        group = str(level) if level and level <= 6 else "body"
        value = paragraph_format(document, element)
        groups[group].append(value)
        format_groups[json.dumps(value, sort_keys=True)].append(index)
    # Measure cover/TOC/header/footer parameters separately: they must be visible
    # in the analysis without changing the dominant body or heading settings.
    supplemental = [(block.locator, block.element, "cover") for block in blocks[:first_heading]]
    supplemental.extend(_native_extra_paragraphs(parsed, revision_view))
    supplemental_elements = {element for _, element, _ in supplemental}
    hidden = {qn("w:" + name) for name in ({"del", "moveFrom"} if revision_view == "final" else {"ins", "moveTo"})}
    for index, paragraph in enumerate(document.element.body.iter(qn("w:p"))):
        if paragraph in supplemental_elements or not visible_text(paragraph, revision_view).strip():
            continue
        if any(ancestor.tag in hidden for ancestor in paragraph.iterancestors()):
            continue
        if any(ancestor.tag == qn("w:txbxContent") for ancestor in paragraph.iterancestors()):
            supplemental.append((f"body/text-box-paragraph[{index}]", paragraph, "text_box"))
    labels = {"cover": "封面/正文前", "toc": "目录", "header": "页眉", "footer": "页脚", "footnote": "脚注", "endnote": "尾注", "text_box": "文本框", "native_text": "补充原生文字"}
    fields = {
        "font_name": "西文字体", "east_asia_font": "中文字体", "font_size_pt": "字号(pt)",
        "bold": "加粗", "italic": "斜体", "color": "文字颜色", "alignment": "对齐",
        "line_spacing": "行距", "line_spacing_rule": "行距规则", "space_before_pt": "段前(pt)",
        "space_after_pt": "段后(pt)", "first_line_indent_pt": "首行缩进(pt)",
        "left_indent_pt": "左缩进(pt)", "right_indent_pt": "右缩进(pt)",
    }
    observations, native_missing = [], []
    for locator, element, region in supplemental:
        try:
            measured = paragraph_format(document, element)
        except (ValueError, TypeError, KeyError, AttributeError):
            native_missing.append(locator)
            continue
        values = "；".join(f"{fields.get(key, key)}：{value}" for key, value in measured.items())
        observations.append({"dimension": "typography", "text": f"{labels[region]}（{locator}）原生排版：" + (values or "未保存可可靠解析的显式参数；不推测。")})
    layout, confidence = {"headings": {}}, {}
    for group, values in groups.items():
        selected, confidence[group] = _dominant(values)
        if group == "body":
            layout[group] = selected
        else:
            layout["headings"][group] = selected
    sections = []
    for section in document.sections:
        sections.append({key: round(getattr(section, attribute).mm, 2) for key, attribute in (
            ("width_mm", "page_width"), ("height_mm", "page_height"),
            ("margin_top_mm", "top_margin"), ("margin_bottom_mm", "bottom_margin"),
            ("margin_left_mm", "left_margin"), ("margin_right_mm", "right_margin"),
            ("header_distance_mm", "header_distance"), ("footer_distance_mm", "footer_distance"),
        ) if getattr(section, attribute) is not None})
    if sections:
        # A whole page geometry is adopted together; never create an unobserved hybrid.
        boundaries = [index for index, element in enumerate(document.element.body) if element.tag == qn("w:sectPr") or element.find(".//" + qn("w:sectPr")) is not None]
        weights = Counter()
        for block in parsed.blocks:
            if block.kind == "paragraph" and block.text:
                section_index = next((i for i, boundary in enumerate(boundaries) if block.body_index <= boundary), len(sections) - 1)
                weights[min(section_index, len(sections) - 1)] += len(block.text)
        counts = Counter()
        for index, value in enumerate(sections):
            counts[json.dumps(value, sort_keys=True)] += weights[index]
        if not sum(counts.values()):
            counts = Counter(json.dumps(value, sort_keys=True) for value in sections) if len({json.dumps(value, sort_keys=True) for value in sections}) == 1 else Counter()
        total_weight = sum(counts.values())
        encoded, count = counts.most_common(1)[0] if counts else ("{}", 0)
        if total_weight and count / total_weight >= 0.6:
            layout["page"] = json.loads(encoded)
    layout["table"], table_coverage = measure_docx_tables(document, parsed)
    # An out-of-range source property is omitted individually, not used to reject all good measurements.
    for group in [layout.get("body", {}), *layout["headings"].values(), layout["table"]["body"], layout["table"]["header"]]:
        from folio.document.style_layout import ParagraphStyle
        for key in list(group):
            if key in {"line_spacing", "line_spacing_rule"}:
                continue
            try:
                ParagraphStyle.model_validate({key: group[key]})
            except ValueError:
                group.pop(key)
        try:
            ParagraphStyle.model_validate({key: group[key] for key in ("line_spacing", "line_spacing_rule") if key in group})
        except ValueError:
            group.pop("line_spacing", None)
            group.pop("line_spacing_rule", None)
    from folio.document.style_layout import TableStyle
    for key in ("header_fill", "border_color", "border_size_pt"):
        if key in layout["table"]:
            try:
                TableStyle.model_validate({key: layout["table"][key]})
            except ValueError:
                layout["table"].pop(key)
    try:
        validated = StyleLayout.model_validate(layout).model_dump(exclude_none=True)
    except ValueError:
        layout.pop("page", None)
        validated = StyleLayout.model_validate(layout).model_dump(exclude_none=True)
    unusual = [
        blocks[first_heading + positions_in_group[0]].locator
        for _, positions_in_group in sorted(format_groups.items(), key=lambda item: len(item[1]))[:6]
        if len(positions_in_group) < max(2, len(positions) * 0.2)
    ]
    return validated, {
        "paragraphs_total": len(eligible) + len(supplemental), "paragraphs_measured": len(positions) + len(supplemental) - len(native_missing),
        "complete": len(positions) == len(eligible) and not native_missing, "field_coverage": confidence,
        "format_variants": len(format_groups),
        "unusual_format_locations": unusual,
        "table": table_coverage,
        "observations": observations,
        "limitations": ["以下原生区域的排版读取失败：" + "、".join(native_missing)] if native_missing else [],
    }


def sample_candidates(source, *, document_id="D1", max_candidates=24, sample_chars=1200, unusual_locations=()):
    """Stratify by position, block kind and section; do not send an entire source."""
    blocks = source.blocks
    if not blocks:
        return []
    choices = {}

    def add(index, reason):
        if 0 <= index < len(blocks) and blocks[index].text.strip():
            choices.setdefault(index, set()).add(reason)

    for fraction, label in ((0, "开头"), (0.5, "中部"), (1, "末尾")):
        add(round((len(blocks) - 1) * fraction), label)
    body_positions = [i for i, block in enumerate(blocks) if block.kind == "paragraph"]
    if body_positions:
        add(body_positions[len(body_positions) // 2], "典型正文")
        after_heading = next((i for i in body_positions if i > 0 and blocks[i - 1].kind == "heading"), body_positions[0])
        add(after_heading, "章节展开")
    table_positions = [i for i, block in enumerate(blocks) if block.kind in {"table_row", "row"}]
    if table_positions:
        add(table_positions[len(table_positions) // 2], "典型表格")
    for index, block in enumerate(blocks):
        if block.locator in unusual_locations:
            add(index, "局部特殊格式")
    for fraction, label in ((0.25, "前部"), (0.75, "后部")):
        add(round((len(blocks) - 1) * fraction), label)
    for kind in sorted({block.kind for block in blocks}):
        positions = [i for i, block in enumerate(blocks) if block.kind == kind]
        for index in (positions[0], positions[len(positions) // 2], positions[-1]):
            add(index, "表格" if kind in {"table_row", "row"} else kind)
    headings = [i for i, block in enumerate(blocks) if block.kind == "heading"]
    for level in sorted({blocks[i].metadata.get("level", 1) for i in headings}):
        positions = [i for i in headings if blocks[i].metadata.get("level", 1) == level]
        add(positions[len(positions) // 2], f"{level}级标题")
        add(min(positions[len(positions) // 2] + 1, len(blocks) - 1), "章节展开")
    for index in sorted(range(len(blocks)), key=lambda i: len(blocks[i].text), reverse=True)[:2]:
        add(index, "长段落")
    # Positional strata come first; remaining slots favor new kinds/sections.
    selected = list(choices)[:max_candidates]
    candidates = []
    for number, index in enumerate(selected, 1):
        block = blocks[index]
        text = block.text[:sample_chars]
        candidates.append({
            "sample_id": f"{document_id}-S{number}", "document_id": document_id,
            "locator": block.locator, "block_index": index, "kind": block.kind,
            "section_hint": str((block.metadata.get("heading_path") or [""])[-1])[:80],
            "reasons": sorted(choices[index]), "text": text,
            "characters": len(text), "original_characters": len(block.text),
            "truncated": len(text) < len(block.text),
        })
    # A PDF page or a one-line TXT can itself be huge. Sample its middle/end too.
    for index in sorted(range(len(blocks)), key=lambda i: len(blocks[i].text), reverse=True)[:2]:
        block = blocks[index]
        if len(block.text) < sample_chars * 3:
            continue
        for start, reason in ((len(block.text) // 2, "长文本中段"), (len(block.text) - sample_chars, "长文本尾段")):
            if len(candidates) >= max_candidates:
                break
            text = block.text[start:start + sample_chars]
            candidates.append({
                "sample_id": f"{document_id}-S{len(candidates) + 1}", "document_id": document_id,
                "locator": f"{block.locator} / chars[{start}:{start + len(text)}]",
                "block_index": index, "kind": block.kind, "reasons": [reason], "text": text,
                "characters": len(text), "original_characters": len(block.text), "truncated": True,
            })
    return candidates
