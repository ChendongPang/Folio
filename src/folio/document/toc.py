"""Build a linked Word table of contents from the generated document headings."""

from copy import deepcopy

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from folio.document.reader import is_toc, xpath
from folio.document.typography import prop


def field_run(kind: str, text: str = ""):
    run = OxmlElement("w:r")
    node = OxmlElement("w:instrText" if kind == "instruction" else "w:fldChar")
    if kind == "instruction":
        node.set(qn("xml:space"), "preserve")
        node.text = text
    else:
        node.set(qn("w:fldCharType"), kind)
    run.append(node)
    return run


def rebuild_toc(document, entries, typography):
    toc = next((el for el in document.element.body if is_toc(el, document)), None)
    if toc is None:
        return
    content = OxmlElement("w:sdtContent")
    for bookmark, title, level, number in entries:
        if not typography.toc_min <= level <= typography.toc_max:
            continue
        element = OxmlElement("w:p")
        paragraph = Paragraph(element, document._body)
        paragraph.style = f"Folio TOC {level}"
        if not len(content):
            # Include the actual rendered headings, including normalized model subheadings.
            element.extend(
                [
                    field_run("begin"),
                    field_run(
                        "instruction",
                        f' TOC \\o "{typography.toc_min}-{typography.toc_max}" \\h \\z \\u ',
                    ),
                    field_run("separate"),
                ]
            )
        link = OxmlElement("w:hyperlink")
        link.set(qn("w:anchor"), bookmark)
        for value in (f"{number}  ", title):
            run = OxmlElement("w:r")
            text = OxmlElement("w:t")
            text.set(qn("xml:space"), "preserve")
            text.text = value
            run.append(text)
            link.append(run)
        element.append(link)
        paragraph.add_run("\t")
        element.extend(
            [
                field_run("begin"),
                field_run("instruction", f" PAGEREF {bookmark} \\h "),
                field_run("separate"),
                field_run("end"),
            ]
        )
        content.append(element)
    if len(content):
        content[-1].append(field_run("end"))

    def retain_section_breaks(elements):
        # A TOC's final paragraph may also terminate its Word section.
        # Rebuilding the field must not merge its page layout into the body/cover.
        for element in elements:
            for section_properties in xpath(element, ".//w:sectPr"):
                paragraph = OxmlElement("w:p")
                properties = OxmlElement("w:pPr")
                properties.append(deepcopy(section_properties))
                paragraph.append(properties)
                content.append(paragraph)

    if toc.tag == qn("w:sdt"):
        existing = toc.find(qn("w:sdtContent"))
        if existing is not None:
            retain_section_breaks([existing])
            toc.remove(existing)
        toc.append(content)
    else:
        body = document.element.body
        items = list(body)
        index = items.index(toc)
        depth, consumed = 0, []
        for element in items[index:]:
            consumed.append(element)
            for f in xpath(element, ".//w:fldChar/@w:fldCharType"):
                depth += 1 if f == "begin" else (-1 if f == "end" else 0)
            if depth == 0:
                break
        retain_section_breaks(consumed)
        for paragraph in list(content):
            body.insert(index, paragraph)
            index += 1
        for element in consumed:
            body.remove(element)
    prop(document.settings.element, "updateFields", val="true")
