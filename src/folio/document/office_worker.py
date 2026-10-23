"""Run with LibreOffice's Python/UNO interpreter, not the application's venv.

Loads a private copy to obtain real bookmark pages without round-tripping the DOCX.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext
from pathlib import Path


def bookmark_layout(document):
    bookmarks = document.getBookmarks()
    cursor = document.getCurrentController().getViewCursor()
    cursor.jumpToLastPage()
    total = cursor.getPage()
    pages, labels = {}, {}
    for name in bookmarks.getElementNames():
        anchor = bookmarks.getByName(name).getAnchor()
        cursor.gotoRange(anchor.getStart(), False)
        pages[name] = cursor.getPage()
        # A bookmark may belong to a table cell or frame, not document.Text.
        text_cursor = anchor.getText().createTextCursorByRange(anchor)
        labels[name] = text_cursor.getPropertyValue("ListLabelString")
    return {"pages": pages, "labels": labels, "total_pages": total}


def render_layout(document, pdf, value, page_references):
    """Read displayed PAGEREFs after layout; physical pages remain diagnostics only."""
    bookmarks = document.getBookmarks()
    names = set(page_references)
    missing_bookmarks = names - set(bookmarks.getElementNames())
    if missing_bookmarks:
        raise ValueError(
            "Missing page-reference bookmarks: " + ", ".join(sorted(missing_bookmarks))
        )
    fields = document.getTextFields()
    references = []
    enumeration = fields.createEnumeration()
    while enumeration.hasMoreElements():
        field = enumeration.nextElement()
        if (
            field.supportsService("com.sun.star.text.textfield.GetReference")
            and field.ReferenceFieldSource == 2  # ReferenceFieldSource.BOOKMARK
            and field.ReferenceFieldPart in {0, 4}  # PAGE / PAGE_DESC
            and field.SourceName in names
        ):
            # PAGE_DESC keeps the target section's Roman/letter/decimal format.
            # PAGE is always Arabic and getPage() ignores section page restarts.
            field.ReferenceFieldPart = 4
            references.append(field)
    # document.refresh() alone leaves imported PAGEREF cache text unchanged.
    fields.refresh()
    document.refresh()
    document.getCurrentController().getViewCursor().jumpToLastPage()
    fields.refresh()
    if pdf:
        document.storeToURL(
            Path(pdf).resolve().as_uri(),
            (
                value("FilterName", "writer_pdf_Export"),
                value("Overwrite", True),
                value("FilterData", (value("ExportNotes", False),)),
            ),
        )
    # Export can finish deferred layout. Never return an earlier page snapshot.
    mapping = bookmark_layout(document)
    probes = []
    try:
        # Nested TOC PAGEREFs become a DocumentIndex on import and are absent
        # from TextFields. Hidden, in-memory references query their actual page
        # labels without updating/replacing the imported TOC or touching DOCX.
        missing = names - {field.SourceName for field in references}
        if missing:
            # Use a real text owner: a document containing only a table need
            # not expose a usable body cursor, while its cell's text does.
            anchor = bookmarks.getByName(sorted(missing)[0]).getAnchor()
            probe_text = anchor.getText()
            cursor = probe_text.createTextCursorByRange(anchor)
            # Stay in the bookmark's text owner. Moving to the whole document
            # end can enter its final table cell and invalidate this body cursor.
            cursor.collapseToEnd()
            cursor.CharHidden = True
            for name in sorted(missing):
                field = document.createInstance("com.sun.star.text.textfield.GetReference")
                field.ReferenceFieldSource = 2
                field.ReferenceFieldPart = 4
                field.SourceName = name
                probe_text.insertTextContent(cursor, field, False)
                probes.append(field)
                anchor = field.getAnchor()
                probe_text = anchor.getText()
                cursor = probe_text.createTextCursorByRange(anchor)
                cursor.collapseToEnd()
                cursor.CharHidden = True
            fields.refresh()
            document.refresh()
            if bookmark_layout(document) != mapping:
                raise RuntimeError("Hidden page-reference probes changed document layout")
        display_pages = {}
        for field in references + probes:
            name, display = field.SourceName, field.getPresentation(False).strip()
            if not display:
                raise ValueError("Empty page reference: " + name)
            if name in display_pages and display_pages[name] != display:
                raise ValueError("Inconsistent page reference: " + name)
            display_pages[name] = display
    finally:
        for field in probes:
            field.getAnchor().getText().removeTextContent(field)
    if probes:
        document.refresh()
        if bookmark_layout(document) != mapping:
            raise RuntimeError("Removing page-reference probes changed document layout")
    # UNO counts automatic blank pages which PDF export may omit. The legacy
    # physical values are diagnostics, not PDF indices or displayed page labels.
    return mapping | {"display_pages": display_pages}


def main():
    import uno

    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    pipe = "folio_" + uuid.uuid4().hex
    source = Path(request["source"]).resolve()
    output = Path(request["result"])

    def value(name, item):
        result = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
        result.Name, result.Value = name, item
        return result

    directory_context = (
        nullcontext(request["office_directory"])
        if request.get("office_directory")
        else tempfile.TemporaryDirectory(prefix="folio-office-")
    )
    with directory_context as directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        profile = Path(directory, "profile").as_uri()
        log_path = Path(directory, "office.log")
        with log_path.open("w+") as log:
            process = subprocess.Popen(
                [
                    request["soffice"],
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--norestore",
                    f"-env:UserInstallation={profile}",
                    f"--accept=pipe,name={pipe};urp;StarOffice.ComponentContext",
                ],
                stdout=log,
                stderr=log,
                start_new_session=False,
                env=os.environ | {"SAL_DISABLE_CUPS": "1", "SAL_USE_VCLPLUGIN": "svp"},
            )
            document = None
            try:
                local = uno.getComponentContext()
                resolver = local.ServiceManager.createInstanceWithContext(
                    "com.sun.star.bridge.UnoUrlResolver", local
                )
                deadline = time.monotonic() + 20
                while True:
                    try:
                        context = resolver.resolve(
                            f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext"
                        )
                        break
                    except Exception:
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise RuntimeError(
                                "Office could not start: " + log.read()[-1500:]
                            ) from None
                        time.sleep(0.1)
                desktop = context.ServiceManager.createInstanceWithContext(
                    "com.sun.star.frame.Desktop", context
                )
                document = desktop.loadComponentFromURL(
                    source.as_uri(),
                    "_blank",
                    0,
                    (
                        value("Hidden", True),
                        value("ReadOnly", True),
                        value(
                            "UpdateDocMode",
                            uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE"),
                        ),
                        value(
                            "MacroExecutionMode",
                            uno.getConstantByName(
                                "com.sun.star.document.MacroExecMode.NEVER_EXECUTE"
                            ),
                        ),
                    ),
                )
                if document is None:
                    raise RuntimeError("Office could not load the Word document")
                mapping = render_layout(
                    document, request.get("pdf"), value, request.get("page_references", [])
                )
                output.write_text(
                    json.dumps(mapping, ensure_ascii=False),
                    encoding="utf-8",
                )
            finally:
                if document is not None:
                    document.close(True)
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
