"""Consistent, portable download names for persisted document jobs."""

import re
import unicodedata

from folio.runtime.language import output_language


def artifact_filename(job: dict) -> str:
    """Name the download from the saved title without using it as a filesystem path."""
    inputs = job.get("input", {})
    language = inputs.get("language") or output_language(inputs.get("user_request"))
    fallback = "Document" if language == "en" else "文档"
    draft_suffix = "-draft" if language == "en" else "-草稿"
    title = inputs.get("title") or fallback
    title = "".join(char for char in title if not unicodedata.category(char).startswith("C"))
    title = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" ._")
    title = re.sub(r"(?:\.docx)+$", "", title, flags=re.IGNORECASE).strip(" ._") or fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", title, re.IGNORECASE):
        title = "_" + title
    # Leave room for the extension and draft suffix on filesystems with 255-byte names.
    title = title.encode("utf-8")[:220].decode("utf-8", errors="ignore").rstrip(" .")
    suffix = "" if job["status"] == "published" or title.endswith(("-草稿", "-draft")) else draft_suffix
    return title + suffix + ".docx"
