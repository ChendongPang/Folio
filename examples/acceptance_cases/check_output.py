"""Offline structural precheck of a generated DOCX; semantic review remains required."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

ROOT = Path(__file__).resolve().parent
EFFECTIVE_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffA-Za-z0-9]")
TOC_FIELD = re.compile(r"^\s*TOC\b", re.I)


def _style_chain(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def _outline_level(properties):
    outline = properties.find(qn("w:outlineLvl")) if properties is not None else None
    if outline is None:
        return None
    try:
        return int(outline.get(qn("w:val")))
    except (TypeError, ValueError):
        return None


def _is_heading(properties, style):
    # A directly assigned body-text level (9) overrides an inherited heading.
    level = _outline_level(properties)
    if level is not None:
        return 0 <= level < 9
    for candidate in _style_chain(style):
        level = _outline_level(candidate.element.pPr)
        if level is not None:
            return 0 <= level < 9
        if re.match(r"^(heading|标题)\s*[1-9]", candidate.name, re.I):
            return True
    return False


def _paragraph_contents(document_body):
    """Read cached text once, retaining complex field scope across paragraphs."""
    paragraphs, paragraph_stack, fields = [], [], []
    toc_containers = set()
    toc_depth = 0
    for event, node in etree.iterwalk(document_body, events=("start", "end")):
        if event == "end":
            if node.tag == qn("w:p"):
                paragraph_stack.pop()
            if node in toc_containers:
                toc_depth -= 1
            continue
        if node.tag == qn("w:p"):
            paragraph = {"element": node, "all_text": [], "body_text": []}
            paragraphs.append(paragraph)
            paragraph_stack.append(paragraph)
        elif node.tag == qn("w:fldSimple") and TOC_FIELD.match(node.get(qn("w:instr"), "")):
            toc_containers.add(node)
            toc_depth += 1
        elif node.tag == qn("w:sdt") and TOC_FIELD.match("".join(
            part.text or "" for part in node.iter(qn("w:instrText"))
        )):
            toc_containers.add(node)
            toc_depth += 1
        elif node.tag == qn("w:fldChar"):
            kind = node.get(qn("w:fldCharType"))
            if kind == "begin":
                fields.append("")
            elif kind == "end" and fields:
                # Nested PAGEREF fields must not close their enclosing TOC.
                fields.pop()
        elif node.tag == qn("w:instrText") and fields:
            fields[-1] += node.text or ""
        elif node.tag == qn("w:t") and paragraph_stack:
            text = node.text or ""
            paragraph_stack[-1]["all_text"].append(text)
            if not toc_depth and not any(TOC_FIELD.match(instruction) for instruction in fields):
                paragraph_stack[-1]["body_text"].append(text)
    return paragraphs


def inspect_document(path):
    doc = Document(path)
    styles = {style.style_id: style for style in doc.styles}
    headings, body, all_text = [], [], []
    started = False
    # Inspect XML in document order, counting each merged table cell only once.
    for paragraph in _paragraph_contents(doc.element.body):
        element = paragraph["element"]
        original_text = "".join(paragraph["all_text"])
        if original_text.strip():
            all_text.append(original_text)
        text = "".join(paragraph["body_text"])
        if not text.strip():
            continue
        prop = element.find(qn("w:pPr"))
        style_ref = prop.find(qn("w:pStyle")) if prop is not None else None
        style = styles.get(style_ref.get(qn("w:val"))) if style_ref is not None else None
        if _is_heading(prop, style):
            headings.append(text)
            started = True
            continue
        if not started or any(
            candidate.name.lower().startswith(("toc", "title", "subtitle"))
            for candidate in _style_chain(style)
        ):
            continue
        body.append(text)
    duplicates = Counter(text for text in body if len(EFFECTIVE_CHAR.findall(text)) >= 80)
    return {
        "effective_body_chars": len(EFFECTIVE_CHAR.findall("\n".join(body))),
        "headings": headings,
        "heading_detection": "recognized" if started else "no_recognized_heading",
        "counting_warnings": [] if started else [
            "No recognizable body heading; the zero body count needs manual review."
        ],
        "table_count": len(doc.tables),
        "inline_image_count": len(doc.inline_shapes),
        "long_duplicate_paragraphs": [
            {"count": count, "preview": text[:160]} for text, count in duplicates.items() if count > 1
        ],
        "text": "\n".join(all_text),
    }


def check(case_dir, output):
    case = json.loads((case_dir / "expected" / "expected.json").read_text(encoding="utf-8"))
    measured = inspect_document(output)
    text = measured.pop("text")
    target = case["target_chars"]
    anchors = []
    for fact in case["expected"]["facts"]:
        missing = [token for token in fact["required_tokens"] if token not in text]
        anchors.append({"id": fact["id"], "severity": fact["severity"], "missing_tokens": missing,
                        "signal": "missing_anchor" if missing else "anchor_present_unverified"})
    chapter_signals = [{"title": chapter["title"], "exact_heading_present": any(
        chapter["title"] in heading for heading in measured["headings"]
    )} for chapter in case["expected"]["chapters"]]
    return {
        "case_id": case["id"],
        "document": str(output),
        "semantic_verdict": "requires_manual_review",
        "counting_method": case["counting_method"],
        "counting_limit": "Requires heading styles or outline levels, including inherited styles. Plain-text cover/TOC may need manual exclusion; heading synonyms are allowed after review.",
        "length_in_range": target["min"] <= measured["effective_body_chars"] <= target["max"],
        "target_chars": target,
        **measured,
        "chapter_signals": chapter_signals,
        "fact_anchor_signals": anchors,
        "missing_coverage_ids": [identifier for identifier in case["expected"].get("coverage_ids", []) if identifier not in text],
        "required_visuals": case["expected"].get("visuals", []),
        "manual_review": case["expected"]["acceptance"],
        "note": "关键词命中不能证明条件、否定、作用域或计算关系正确；不依据此报告判定整案通过。原文引用与流程次数须另核对作业记录。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", help="Case folder, e.g. cases/01_expense_urd")
    parser.add_argument("docx", type=Path, help="Actual generated document")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    case_dir = Path(args.case)
    if not case_dir.is_dir():
        case_dir = ROOT / "cases" / args.case
    result = check(case_dir, args.docx)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{result['case_id']}: {result['effective_body_chars']} body characters; length_in_range={result['length_in_range']}; semantic review required")


if __name__ == "__main__":
    main()
