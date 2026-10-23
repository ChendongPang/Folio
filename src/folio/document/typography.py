"""Built-in document typography and structural layout rules."""

import re
from copy import deepcopy
from dataclasses import dataclass

from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.paragraph import Paragraph

from folio.document.reader import is_toc, xpath

BODY = "Folio Body"
NOTICE = "Folio Notice"
TOC_HEADING = "Folio TOC Heading"
DOCUMENT_STATUS = "Folio Document Status"
TABLE_BODY = "Folio Table Body"
TABLE_HEADER = "Folio Table Header"


def prop(parent, name, **attrs):
    tag = qn("w:" + name)
    node = parent.find(tag)
    if node is None:
        node = OxmlElement("w:" + name)
        parent.append(node)
    for key, value in attrs.items():
        node.set(qn("w:" + key), str(value))
    return node


def _merge(target, source):
    if source is None:
        return
    # Partial overrides of these properties retain attributes from their base style.
    attribute_properties = {qn("w:" + name) for name in ("rFonts", "ind", "spacing", "lang")}
    for item in source:
        old = target.find(item.tag)
        if old is not None and item.tag in attribute_properties:
            old.attrib.update(item.attrib)
            if item.tag == qn("w:rFonts"):
                for name in ("ascii", "hAnsi", "eastAsia", "cs"):
                    if qn("w:" + name) in item.attrib:
                        old.attrib.pop(qn("w:" + name + "Theme"), None)
                        if name == "cs":
                            old.attrib.pop(qn("w:cstheme"), None)
            continue
        if old is not None:
            target.remove(old)
        target.append(deepcopy(item))


def _style_chain(style):
    chain, seen = [], set()
    while style is not None and style.style_id not in seen:
        chain.append(style)
        seen.add(style.style_id)
        style = style.base_style
    return reversed(chain)


def resolved_format(document, element=None, style=None, *, toc=False):
    """Resolve defaults, inherited styles and the prototype's explicit formatting."""
    ppr, rpr = OxmlElement("w:pPr"), OxmlElement("w:rPr")
    for name, target in (("pPr", ppr), ("rPr", rpr)):
        nodes = xpath(document.styles.element, f"./w:docDefaults/w:{name}Default/w:{name}")
        if nodes:
            _merge(target, nodes[0])
    normal = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
    if element is not None:
        style = Paragraph(element, document._body).style
    style = style if style is not None else normal
    for base in [normal, *_style_chain(style)]:
        _merge(ppr, base.element.find(qn("w:pPr")))
        _merge(rpr, base.element.find(qn("w:rPr")))
    if element is not None:
        _merge(ppr, element.find(qn("w:pPr")))
        if toc:
            # Word stores the displayed TOC overrides in the paragraph mark.
            # Ignoring these resurrects inherited italics/caps and the wrong size.
            marks = xpath(element, "./w:pPr/w:rPr")
            if marks:
                _merge(rpr, marks[0])
        # A label or instruction's single emphasized word must not set the whole body font.
        runs = [r for r in xpath(element, ".//w:r") if xpath(r, "./w:t")]
        if runs:
            run = max(runs, key=lambda r: sum(len(t) for t in xpath(r, "./w:t/text()")))
            character_styles = xpath(run, "./w:rPr/w:rStyle/@w:val")
            if character_styles and not toc:
                character_style = document.styles.get_by_id(
                    character_styles[0], WD_STYLE_TYPE.CHARACTER
                )
                for base in _style_chain(character_style):
                    _merge(rpr, base.element.find(qn("w:rPr")))
            _merge(rpr, run.find(qn("w:rPr")))
    for ref in list(rpr.findall(qn("w:rStyle"))):
        rpr.remove(ref)
    return ppr, rpr


def _install(document, name, formats, *, level=None):
    style = (
        document.styles[name]
        if name in document.styles
        else document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    )
    style.base_style = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
    ppr, rpr = map(deepcopy, formats)
    for item in list(ppr):
        if item.tag in {qn("w:" + key) for key in ("pStyle", "numPr", "sectPr", "rPr")}:
            ppr.remove(item)
    prop(ppr, "outlineLvl", val=level - 1 if level else 9)
    if level:
        prop(ppr, "keepNext", val=1)
        prop(ppr, "keepLines", val=1)
    prop(ppr, "widowControl", val=1)
    for key in ("pPr", "rPr"):
        existing = style.element.find(qn("w:" + key))
        if existing is not None:
            style.element.remove(existing)
    style.element.extend([ppr, rpr])
    return style


@dataclass
class DocumentTypography:
    toc_min: int = 1
    toc_max: int = 3


def _apply_body_paragraph_policy(document, language):
    body = document.styles[BODY]
    indent = body.element.get_or_add_pPr().get_or_add_ind()
    if language == "en":
        indent.attrib.pop(qn("w:firstLineChars"), None)
        indent.set(qn("w:firstLine"), "720")  # 0.5 inch in twentieths of a point
        body.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        body.paragraph_format.line_spacing = 1.15
        body.paragraph_format.space_after = Pt(6)
    else:
        indent.attrib.pop(qn("w:firstLine"), None)
        indent.set(qn("w:firstLineChars"), "200")  # Two East Asian character widths
        body.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        body.paragraph_format.line_spacing = 1.5
        body.paragraph_format.space_after = Pt(0)


def install_styles(document, *, language="zh-CN"):
    """Install one built-in style policy; source documents never determine output formatting."""
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    prop(
        normal.element.get_or_add_rPr(), "rFonts", ascii="Calibri", hAnsi="Calibri", eastAsia="宋体"
    )
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    body = resolved_format(document)
    for name in (BODY, NOTICE, TABLE_BODY, TABLE_HEADER):
        style = _install(document, name, body)
        if name in {TABLE_BODY, TABLE_HEADER}:
            style.paragraph_format.space_after = Pt(3)
            prop(style.element.get_or_add_pPr(), "ind", firstLine=0, firstLineChars=0)
        if name == TABLE_HEADER:
            style.font.bold = True
    _apply_body_paragraph_policy(document, language)
    for level in range(1, 7):
        heading = document.styles[f"Heading {level}"]
        heading.font.name = "Calibri"
        heading.font.size = Pt(max(12, 20 - 2 * level))
        heading.font.bold = True
        heading.font.color.rgb = RGBColor(0, 0, 0)
        prop(
            heading.element.get_or_add_rPr(),
            "rFonts",
            ascii="Calibri",
            hAnsi="Calibri",
            eastAsia="黑体",
        )
        heading.paragraph_format.keep_with_next = True
        heading.paragraph_format.keep_together = True
        style = _install(
            document,
            f"Folio Heading {level}",
            resolved_format(document, style=heading),
            level=level,
        )
        style.next_paragraph_style = document.styles[BODY]
        toc = _install(document, f"Folio TOC {level}", body)
        prop(toc.element.get_or_add_pPr(), "ind", firstLine=0, firstLineChars=0)
        toc.paragraph_format.left_indent = Pt((level - 1) * 12)
        page = document.sections[0]
        width = page.page_width - page.left_margin - page.right_margin
        toc.paragraph_format.tab_stops.add_tab_stop(
            width, WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
    toc_heading = _install(
        document,
        TOC_HEADING,
        resolved_format(document, style=document.styles["Heading 1"]),
    )
    toc_heading.font.name = "Calibri"
    toc_heading.font.size = Pt(16)
    toc_heading.font.bold = True
    toc_heading.paragraph_format.space_before = Pt(0)
    toc_heading.paragraph_format.space_after = Pt(12)
    status = _install(document, DOCUMENT_STATUS, body)
    status.font.name = "Calibri"
    status.font.size = Pt(9)
    status.font.bold = True
    status.font.color.rgb = RGBColor(96, 96, 96)
    status.paragraph_format.space_before = Pt(2)
    status.paragraph_format.space_after = Pt(14)
    return DocumentTypography()


def install_template_styles(document, *, body=None, headings=None):
    """Add generated-content styles without rewriting the template's own styles.

    Direct formatting on a template paragraph is part of its authoring contract,
    as are inherited fonts and spacing. Only structural properties that are
    missing from that contract receive defaults.
    """
    headings = headings or {}
    body_formats = resolved_format(document, body)
    toc_prototypes = {}
    for element in document.element.body:
        if not is_toc(element, document):
            continue
        paragraphs = [element] if element.tag == qn("w:p") else xpath(element, ".//w:p")
        for paragraph in paragraphs:
            style = Paragraph(paragraph, document._body).style
            match = re.fullmatch(r"(?:toc|目录)\s*(\d+)", style.name, re.I)
            if match:
                toc_prototypes.setdefault(int(match[1]), paragraph)

    def install(name, formats, level=None):
        if name in document.styles:
            return document.styles[name]
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
        ppr, rpr = map(deepcopy, formats)
        for item in list(ppr):
            if item.tag in {qn("w:" + key) for key in ("pStyle", "numPr", "sectPr", "rPr")}:
                ppr.remove(item)
        prop(ppr, "outlineLvl", val=level - 1 if level else 9)
        if level:
            for name in ("keepNext", "keepLines"):
                if ppr.find(qn("w:" + name)) is None:
                    prop(ppr, name, val=1)
        style.element.extend([ppr, rpr])
        return style

    for name in (BODY, NOTICE, TABLE_BODY, TABLE_HEADER):
        install(name, body_formats)
    for level in range(1, 7):
        prototype = headings.get(level)
        formats = resolved_format(document, prototype, style=document.styles[f"Heading {level}"])
        install(f"Folio Heading {level}", formats, level)
        existing_toc = next(
            (style for style in document.styles if style.name.lower() == f"toc {level}"),
            None,
        )
        toc_formats = (
            resolved_format(document, toc_prototypes.get(level), style=existing_toc, toc=True)
            if level in toc_prototypes or existing_toc is not None
            else body_formats
        )
        toc_name = f"Folio TOC {level}"
        original_style = toc_name in document.styles
        toc = install(toc_name, toc_formats)
        if not original_style and not xpath(toc.element, "./w:pPr/w:tabs"):
            page = document.sections[0]
            width = page.page_width - page.left_margin - page.right_margin
            toc.paragraph_format.tab_stops.add_tab_stop(
                width, WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
            )
    return DocumentTypography()


def disable_table_numbering(table):
    """Generated cell text already includes its labels; cancel inherited list counters."""
    for paragraph in table._tbl.iter(qn("w:p")):
        # Removing numPr is insufficient: the paragraph/table/default style can
        # supply numbering again. An explicit zero overrides inherited counters
        # without altering the document's fonts, widths, borders or alignment.
        paragraph.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = 0


def repair_table_pagination(table):
    """Retain fonts, column widths and decoration; repair pagination only."""
    for i, row in enumerate(table.rows):
        trpr = row._tr.get_or_add_trPr()
        if i == 0:
            prop(trpr, "tblHeader")
        if (
            sum(len(cell.text) for cell in row.cells) < 1200
            and trpr.find(qn("w:cantSplit")) is None
        ):
            prop(trpr, "cantSplit")
        if i == 0:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = True


def clean_heading(text):
    return re.sub(
        r"^\s*(?:\d+(?:[.．]\d+)+(?:[.．、])?(?=\s|$)\s*|\d+[.．、]\s+|第[一二三四五六七八九十百\d]+[章节]\s*)",
        "",
        text,
    ).strip()
