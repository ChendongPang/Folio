"""Render a private DOCX copy to real PDF pages for bounded visual inspection."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from folio.document.pagination import paginate


def prepare_page_preview(source, directory, *, max_pages=120):
    """Paginate once and count pages before deciding how much visual work to run.

    Page descriptors contain no image bytes or extracted text. The review loop
    materializes only the next pair of pages with ``materialize_page_images``.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tools = {name: shutil.which(name) for name in ("pdftoppm", "pdftotext", "pdfinfo")}
    if not all(tools.values()):
        return {"status": "skipped", "reason": "requires_poppler"}
    paginated = directory / "paginated.docx"
    pdf = directory / "document.pdf"
    shutil.copyfile(source, paginated)
    layout = paginate(paginated, pdf=pdf)
    if layout["status"] != "paginated":
        return {"status": "skipped", "reason": layout["status"]}
    info = subprocess.run(
        [tools["pdfinfo"], str(pdf)], capture_output=True, text=True, check=True, timeout=20
    ).stdout
    count = re.search(r"^Pages:\s*(\d+)", info, re.MULTILINE)
    if not count or int(count[1]) < 1:
        raise ValueError("Cannot determine rendered PDF page count")
    total = int(count[1])
    # A resumed export can reuse this round directory with a shorter document.
    for old_image in directory.glob("page-*.png"):
        old_image.unlink()
    return {
        "status": "rendered",
        "pages": [{"page": number} for number in range(1, min(total, max_pages) + 1)],
        "total_pages": total,
        "layout": layout,
        "pdf": str(pdf),
        "document": str(paginated),
        "image_directory": str(directory),
    }


def materialize_page_images(preview, pages):
    """Render a consecutive batch; never load all document images into memory."""
    if not pages or all("path" in page for page in pages):
        return pages
    numbers = [page["page"] for page in pages]
    first, last = min(numbers), max(numbers)
    if numbers != list(range(first, last + 1)) or last > preview["total_pages"]:
        raise ValueError("Page render batch must contain consecutive physical PDF pages")
    directory, pdf = Path(preview["image_directory"]), Path(preview["pdf"])
    # Match PDF physical page indices, rather than Office bookmark page numbers.
    text = subprocess.run(
        ["pdftotext", "-f", str(first), "-l", str(last), "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split("\f")
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(first),
            "-l",
            str(last),
            "-scale-to",
            "1500",
            "-png",
            str(pdf),
            str(directory / "page"),
        ],
        capture_output=True,
        check=True,
        timeout=90,
    )
    images = {int(path.stem.rsplit("-", 1)[1]): path for path in directory.glob("page-*.png")}
    if not set(numbers) <= images.keys():
        raise ValueError("Rendered image count does not match the PDF batch")
    for index, page in enumerate(pages):
        page.update(path=str(images[page["page"]]), text=text[index] if index < len(text) else "")
    return pages


def render_page_preview(source, directory, *, max_pages=24):
    """Convenience API for callers that need an already materialized preview."""
    preview = prepare_page_preview(source, directory, max_pages=max_pages)
    if preview["status"] == "rendered":
        for start in range(0, len(preview["pages"]), 2):
            materialize_page_images(preview, preview["pages"][start : start + 2])
    return preview


def immutable_page_images(pages, directory):
    """Cache identity follows bytes, not round-specific temporary paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for page in pages:
        value = Path(page["path"]).read_bytes()
        key = hashlib.sha256(value).hexdigest()
        target = directory / (key + ".png")
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != key:
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(value)
            temporary.replace(target)
        result.append({"page": page["page"], "path": str(target.resolve()), "sha256": key})
    return result
