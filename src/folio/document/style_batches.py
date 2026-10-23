"""Lossless located text slices and compact accumulated style observations."""

from __future__ import annotations

from collections import defaultdict

from folio.document.style_contracts import DIMENSIONS

TEXT_DIMENSIONS = frozenset({"structure", "writing", "organization", "detail", "headings", "figures_tables", "expression"})
DIMENSION_DETAILS = {
    "structure": "Section hierarchy, table-of-contents organization, section order, and placement of abstract, conclusions, appendices and references",
    "writing": "Formality, technical sophistication, objectivity, wording, sentence length, and orientation toward technical description or management reporting",
    "organization": "How sections develop, the order of background and conclusions, and argument structures such as problem, analysis and recommendation",
    "detail": "Section length, depth of argument, balance of explanation and conclusions, and use of examples and data",
    "headings": "Heading length, naming patterns and numbering system",
    "figures_tables": "Extractable captions, reference frequency, table column counts and headers, and explanations before and after figures/tables; identify image-internal content separately",
    "typography": "Fonts, sizes, colors, weight, line/paragraph spacing, indentation and alignment from program measurements only; never infer them from text",
    "page": "Paper, page dimensions, margins, headers/footers, page numbering and breaks from program measurements only; do not infer missing properties",
    "visual": "Measurable colors and decoration; explicitly mark whitespace, cover design and image/text placement requiring page images as visual gaps",
    "expression": "Terminology conventions, citation forms, unit formatting, and phrasing of risks and recommendations; do not inherit specific named entities or data",
}


def full_text_segments(source, *, document_id, sample_chars, max_samples):
    """Retain each extracted character exactly once, including long block tails."""
    samples = []
    total_segments = total_characters = 0
    for block_index, block in enumerate(source.blocks):
        total_characters += len(block.text)
        total_segments += (len(block.text) + sample_chars - 1) // sample_chars
        if len(samples) >= max_samples:
            continue
        for start in range(0, len(block.text), sample_chars):
            if len(samples) >= max_samples:
                break
            end = min(start + sample_chars, len(block.text))
            locator = block.locator
            if start or end < len(block.text):
                locator += f"; chars {start + 1}-{end}"
            samples.append({
                "sample_id": f"{document_id}-S{len(samples) + 1}",
                "locator": locator, "kind": block.kind,
                "heading_level": block.metadata.get("level") if block.kind == "heading" else None,
                "block_index": block_index, "char_start": start, "char_end": end,
                "characters": end - start, "text": block.text[start:end],
                "truncated": False, "segmented": end - start < len(block.text),
                "reasons": ["全文顺序分段", block.kind],
            })
    return samples, {
        "characters_total": total_characters, "characters_read": 0,
        "segments_total": total_segments, "segments_read": 0,
        "complete": total_characters == 0,
    }


def merge_rules(previous, incoming):
    """A later batch cannot erase already evidenced observations."""
    merged = {(rule["dimension"], rule["scope"], rule["description"]): dict(rule) for rule in previous}
    for rule in incoming:
        key = rule["dimension"], rule["scope"], rule["description"]
        if key in merged:
            old = merged[key]
            rule = dict(rule)
            rule["sample_ids"] = list(dict.fromkeys(old["sample_ids"] + rule["sample_ids"]))[:6]
            if old["confidence"] == "observed":
                rule["confidence"] = "observed"
        merged[key] = rule
    return list(merged.values())


def observation_memory(rules, checks, *, limit=None):
    """Bound prompt memory while the full evidence stays in the stored profile."""
    grouped = defaultdict(list)
    for rule in rules:
        grouped[rule["dimension"]].append(rule)
    lines, size = [], 0
    # Round robin retains all dimensions before spending space on repeated detail.
    for index in range(max((len(items) for items in grouped.values()), default=0)):
        for dimension in DIMENSIONS:
            if index >= len(grouped[dimension]):
                continue
            rule = grouped[dimension][index]
            text = f"- {dimension} / {rule['scope']}: {rule['description']}; evidence {', '.join(rule['sample_ids'])}"
            if limit is not None and size + len(text) > limit:
                continue
            lines.append(text)
            size += len(text)
    for dimension, check in checks.items():
        text = f"- {dimension} checked: {check['status']}; {check['reason']}; evidence {', '.join(check.get('sample_ids', []))}"
        if limit is None or size + len(text) <= limit:
            lines.append(text)
            size += len(text)
    return lines


def update_text_coverage(document, delivered):
    samples = [sample for sample in delivered.values() if not sample.get("page_image")]
    coverage = document["text_coverage"]
    coverage["characters_read"] = sum(sample["characters"] for sample in samples)
    coverage["segments_read"] = len(samples)
    coverage["complete"] = coverage["characters_read"] == coverage["characters_total"]


def dimension_coverage(document, checks):
    """Missing text evidence stays unresolved; only visual-only gaps are optional."""
    result = {}
    extracted_text = document["text_coverage"]["characters_total"] > 0
    layout = document.get("layout", {})
    observations = {item["dimension"] for item in document.get("layout_coverage", {}).get("observations", [])}
    for dimension in DIMENSIONS:
        rules = [rule for rule in document.get("rules", []) if rule["dimension"] == dimension]
        check = checks.get(dimension)
        native = document.get("layout_coverage", {})
        if dimension == "typography" and native.get("complete") is False:
            result[dimension] = {"status": "unresolved", "reason": "原生段落排版测量未覆盖全部可读取位置。"}
        elif dimension == "figures_tables" and native.get("table", {}).get("complete") is False:
            result[dimension] = {"status": "unresolved", "reason": "原生表格测量未覆盖全部可读取单元格。"}
        elif check and check["status"] == "unresolved" and dimension in TEXT_DIMENSIONS:
            result[dimension] = {"status": "unresolved", "reason": check["reason"]}
        elif rules:
            result[dimension] = {"status": "analyzed", "reason": "已依据已读内容提取风格规则。"}
        elif check and check["status"] == "not_present" and document["text_coverage"]["complete"] and check.get("characters_read_at_check") == document["text_coverage"]["characters_total"]:
            result[dimension] = {"status": "not_present", "reason": check["reason"]}
        elif dimension in TEXT_DIMENSIONS:
            result[dimension] = {
                "status": "unresolved" if extracted_text else "requires_visual",
                "reason": "尚缺有依据的风格规则或未出现说明。" if extracted_text else "没有可提取文本，需要识别页面图像才能核对。",
            }
        elif dimension == "typography" and (layout.get("body") or layout.get("headings")):
            result[dimension] = {"status": "analyzed", "reason": "已提取原生字体与排版属性；未测出的属性不推测。"}
        elif dimension == "page" and layout.get("page"):
            result[dimension] = {"status": "analyzed", "reason": "已提取原生页面参数；未测出的属性不推测。"}
        elif dimension in observations:
            result[dimension] = {"status": "analyzed", "reason": "已记录程序测得的原生属性；视觉效果另行说明。"}
        else:
            result[dimension] = {"status": "requires_visual", "reason": "当前没有可可靠读取的原生属性；页面视觉效果需图像输入确认。"}
    return result
