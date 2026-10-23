"""Read DOCX in document order, including visible tracked insertions.

This is a reading view, not an approval of revisions. Originals are never edited.
Comments are returned separately and never mixed into requirement text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {
    "w": W,
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
}


def xpath(element, expression):
    return etree.XPath(expression, namespaces=NS)(element)


def local_name(element) -> str:
    return etree.QName(element).localname


def visible_text(element, view: str = "final") -> str:
    hidden = {"del", "moveFrom"} if view == "final" else {"ins", "moveTo"}

    def walk(node):
        name = local_name(node)
        if name in hidden or name.endswith("PrChange") or name in {"instrText", "delInstrText"}:
            return ""
        if name in {"t", "delText"}:
            return node.text or ""
        if name == "tab":
            return "\t"
        if name in {"br", "cr"}:
            return "\n"
        return "".join(walk(child) for child in node)

    return walk(element)


def heading_level(element, document) -> int | None:
    if local_name(element) != "p":
        return None
    direct = element.find("./" + qn("w:pPr") + "/" + qn("w:outlineLvl"))
    if direct is not None:
        value = int(direct.get(qn("w:val")))
        return value + 1 if value < 9 else None
    ids = xpath(element, "./w:pPr/w:pStyle/@w:val")
    style = next((s for s in document.styles if ids and s.style_id == ids[0]), None)
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        # TOC styles are not document headings, regardless of their displayed text.
        if re.match(r"(?:toc|目录)\s*\d+", style.name, re.I):
            return None
        values = style.element.xpath("./w:pPr/w:outlineLvl/@w:val")
        if values:
            value = int(values[0])
            return value + 1 if value < 9 else None
        match = re.fullmatch(r"(?:heading|标题)\s*(\d+)", style.name, re.I)
        if match:
            return int(match[1])
        style = style.base_style
    return None


def is_toc(element, document=None) -> bool:
    instructions = xpath(element, ".//w:instrText/text()")
    if any(re.match(r"\s*TOC\b", t, re.I) for t in instructions):
        return True
    if local_name(element) == "p" and document is not None:
        ids = xpath(element, "./w:pPr/w:pStyle/@w:val")
        style = next((s for s in document.styles if ids and s.style_id == ids[0]), None)
        return bool(style and re.match(r"(?:toc|目录)\s*\d+", style.name, re.I))
    return False


def table_rows(element, view="final") -> list[list[str]]:
    rows = []
    for tr in element.findall(qn("w:tr")):
        marker = "del" if view == "final" else "ins"
        if tr.find(f"./{qn('w:trPr')}/{qn('w:' + marker)}") is not None:
            continue
        row = []
        for cell in tr.findall(qn("w:tc")):
            value = "\n".join(visible_text(p, view) for p in cell.findall(qn("w:p"))).strip()
            span = cell.find(f"./{qn('w:tcPr')}/{qn('w:gridSpan')}")
            row.extend([value] * (int(span.get(qn("w:val"))) if span is not None else 1))
        rows.append(row)
    return rows


@dataclass
class ReadBlock:
    locator: str
    body_index: int
    kind: str
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    level: int | None = None
    path: list[str] = field(default_factory=list)
    comment_ids: list[str] = field(default_factory=list)
    inserted: bool = False
    deleted: bool = False
    element: object = field(default=None, repr=False)


@dataclass
class ReadDocument:
    path: Path
    document: object
    blocks: list[ReadBlock]
    comments: list[dict]
    revisions: dict[str, int]
    warnings: list[str]


def read_docx(path: Path, view: str = "final") -> ReadDocument:
    if view not in {"final", "original"}:
        raise ValueError("Revision view must be final or original")
    document = Document(path)
    revisions = {
        name: len(document.element.xpath(f".//w:{name}"))
        for name in ("ins", "del", "moveTo", "moveFrom")
    }
    warnings = []
    if any(revisions.values()):
        warnings.append(f"包含修订；按 {view} 视图读取，视图选择不代表业务确认")
    if document.element.xpath(".//w:txbxContent | .//w:drawing"):
        warnings.append("存在图片或文本框，当前不提取其业务文字")
    blocks, stack, active_comments = [], [], set()
    hidden = {"del", "moveFrom"} if view == "final" else {"ins", "moveTo"}

    def walk(element, locator, body_index):
        name = local_name(element)
        if name in hidden or is_toc(element, document):
            return
        if name not in {"p", "tbl"}:
            if name in {"sdt", "sdtContent", "ins", "moveTo", "del", "moveFrom"}:
                for i, child in enumerate(element):
                    walk(child, f"{locator}/{name}[{i}]", body_index)
            return
        comment_ids = set(active_comments)
        for marker in element.iter():
            tag = local_name(marker)
            cid = marker.get(qn("w:id"))
            if tag == "commentRangeStart":
                active_comments.add(cid)
                comment_ids.add(cid)
            elif tag in {"commentReference", "commentRangeEnd"}:
                comment_ids.add(cid)
                if tag == "commentRangeEnd":
                    active_comments.discard(cid)
        text = visible_text(element, view).strip() if name == "p" else ""
        level = heading_level(element, document) if text else None
        if level:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
        block = ReadBlock(
            locator=locator,
            body_index=body_index,
            kind="heading" if level else ("table" if name == "tbl" else "paragraph"),
            text=text,
            rows=table_rows(element, view) if name == "tbl" else [],
            level=level,
            path=[v for _, v in stack],
            comment_ids=sorted(c for c in comment_ids if c),
            inserted=bool(xpath(element, ".//w:ins")),
            deleted=bool(xpath(element, ".//w:del")),
            element=element,
        )
        if text or block.rows:
            blocks.append(block)

    for index, element in enumerate(document.element.body):
        walk(element, f"body[{index}]", index)
    comments = []
    with ZipFile(path) as archive:
        resolved, parents = {}, {}
        if "word/commentsExtended.xml" in archive.namelist():
            extended = etree.fromstring(archive.read("word/commentsExtended.xml"))
            for comment in extended:
                pid = comment.get(f"{{{NS['w15']}}}paraId")
                resolved[pid] = comment.get(f"{{{NS['w15']}}}done") == "1"
                parents[pid] = comment.get(f"{{{NS['w15']}}}paraIdParent")
        if "word/comments.xml" in archive.namelist():
            root = etree.fromstring(archive.read("word/comments.xml"))
            paragraph_ids = {
                c.get(qn("w:id")): c.xpath(".//w:p/@w14:paraId", namespaces=NS) for c in root
            }
            para_to_comment = {pid: cid for cid, pids in paragraph_ids.items() for pid in pids}
            for comment in root:
                cid = comment.get(qn("w:id"))
                pids = paragraph_ids[cid]
                comments.append(
                    {
                        "comment_id": cid,
                        "text": visible_text(comment, view),
                        "author": comment.get(qn("w:author")),
                        "date": comment.get(qn("w:date")),
                        "resolved": any(resolved.get(pid, False) for pid in pids),
                        "parent_comment_id": next(
                            (
                                para_to_comment.get(parents.get(pid))
                                for pid in pids
                                if parents.get(pid)
                            ),
                            None,
                        ),
                        "locators": [b.locator for b in blocks if cid in b.comment_ids],
                    }
                )
    # Replies inherit their thread's anchor, even when they have no range in document.xml.
    by_id = {c["comment_id"]: c for c in comments}
    for comment in comments:
        if not comment["locators"] and comment["parent_comment_id"] in by_id:
            comment["locators"] = by_id[comment["parent_comment_id"]]["locators"]
    return ReadDocument(path, document, blocks, comments, revisions, warnings)
