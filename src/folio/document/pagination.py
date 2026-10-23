"""Cache actual TOC page numbers while preserving the original OOXML formatting."""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from folio.document.reader import xpath
from folio.document.typography import prop


def office_runtime():
    configured = os.environ.get("FOLIO_OFFICE_PYTHON"), os.environ.get("FOLIO_SOFFICE")
    if any(configured):
        if not all(configured):
            raise RuntimeError("Both FOLIO_OFFICE_PYTHON and FOLIO_SOFFICE are required")
        return {"python": configured[0], "soffice": configured[1]}
    local = Path.home() / ".local/share/folio/office/runtime.json"
    if local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    soffice, python = (
        shutil.which("libreoffice") or shutil.which("soffice"),
        shutil.which("python3"),
    )
    if soffice and python:
        check = subprocess.run([python, "-c", "import uno"], capture_output=True, timeout=10)
        if check.returncode == 0:
            return {"python": python, "soffice": soffice}
    return None


def cache_page_references(path, pages, labels=None):
    """Only replace PAGEREF results; never rewrite business prose or table cells."""
    with ZipFile(path) as archive:
        files = {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}
    root = etree.fromstring(files["word/document.xml"][1])
    if labels:
        for link in xpath(root, ".//w:hyperlink[@w:anchor]"):
            names = xpath(link, "./@w:anchor")
            texts = xpath(link, "./w:r/w:t")
            if names[0] in labels and len(texts) == 2:
                # Number labels come from the layout engine, preserving Roman/letter/
                # localized document numbering instead of guessing their display text.
                texts[0].text = labels[names[0]] + "  " if labels[names[0]] else ""
    count = 0
    for instruction in xpath(root, ".//w:instrText"):
        match = re.fullmatch(r"\s*PAGEREF\s+(\S+)\s*(?:\\h)?\s*", instruction.text or "")
        if not match:
            continue
        if match[1] not in pages:
            raise ValueError("Cannot paginate missing bookmark: " + match[1])
        parent = instruction.getparent().getparent()
        siblings = list(parent)
        index = siblings.index(instruction.getparent()) + 1
        separate = None
        for node in siblings[index:]:
            kinds = xpath(node, ".//w:fldChar/@w:fldCharType")
            if "separate" in kinds:
                separate = node
            elif "end" in kinds:
                if separate is None:
                    raise ValueError("Invalid PAGEREF field")
                result = etree.Element(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r"
                )
                text = etree.SubElement(
                    result, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                )
                text.text = str(pages[match[1]])
                node.addprevious(result)
                count += 1
                break
            elif separate is not None:
                parent.remove(node)
    files["word/document.xml"] = (
        files["word/document.xml"][0],
        etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),
    )
    settings = etree.fromstring(files["word/settings.xml"][1])
    # Ready to read on first open. Users can still update the live TOC after editing.
    prop(settings, "updateFields", val="false")
    files["word/settings.xml"] = (
        files["word/settings.xml"][0],
        etree.tostring(settings, xml_declaration=True, encoding="UTF-8", standalone=True),
    )
    temporary = Path(str(path) + ".paginated")
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for info, content in files.values():
                archive.writestr(info, content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def paginate(path: Path, *, pdf: Path | None = None, required=False):
    runtime = office_runtime()
    if runtime is None:
        if required:
            raise RuntimeError(
                'Contents pagination requires LibreOffice and Python UNO. Configure FOLIO_SOFFICE and FOLIO_OFFICE_PYTHON.'
            )
        return {
            "status": "requires_office",
            "message": "已保留可更新目录域；本机缺少 LibreOffice/UNO，页码需在 Word/WPS 中更新。",
        }
    worker = Path(__file__).with_name("office_worker.py")
    with ZipFile(path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    page_references = sorted(
        {
            match[1]
            for instruction in xpath(root, ".//w:instrText")
            if (match := re.fullmatch(r"\s*PAGEREF\s+(\S+)\s*(?:\\h)?\s*", instruction.text or ""))
        }
    )
    with tempfile.TemporaryDirectory(prefix="folio-pagination-") as directory:
        folder = Path(directory)
        working = folder / "document.docx"
        shutil.copy2(path, working)
        previous = None
        for _ in range(4):
            request = folder / "request.json"
            result = folder / "result.json"
            preview = folder / "preview.pdf"
            request.write_text(
                json.dumps(
                    {
                        "source": str(working),
                        "result": str(result),
                        "soffice": runtime["soffice"],
                        "page_references": page_references,
                        # Parent cleanup runs after the entire private Office group
                        # has stopped; the launcher can exit before soffice.bin.
                        "office_directory": str(folder / f"office-{_}"),
                        "pdf": str(preview) if pdf else None,
                    }
                ),
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [runtime["python"], str(worker), str(request)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
            try:
                _, stderr = process.communicate(timeout=90)
            finally:
                # Includes the private office server, even if the UNO worker times out.
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                process.wait()
            if process.returncode:
                raise RuntimeError("Word pagination failed: " + stderr[-1500:])
            mapping = json.loads(result.read_text(encoding="utf-8"))
            # PAGEREF displays section-aware page labels, not Office's physical
            # layout index (which can also include blank pages omitted from PDF).
            count = cache_page_references(working, mapping["display_pages"], mapping.get("labels"))
            if mapping == previous:
                # No LibreOffice DOCX round-trip: preserve formatting and all source content.
                shutil.copyfile(working, path)
                if pdf:
                    pdf.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(preview, pdf)
                return {"status": "paginated", "toc_entries": count, **mapping}
            previous = mapping
        raise RuntimeError("Word pagination did not stabilize after four passes")
