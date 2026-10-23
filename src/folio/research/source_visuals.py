"""Bounded, immutable views of original source pages for business research."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from PIL import Image

from folio.research.workspace import WorkspaceError

MAX_PIXELS = 40_000_000
MAX_DIMENSION = 2400


def _region(value):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or any(type(number) not in (int, float) or not math.isfinite(number) for number in value)):
        raise WorkspaceError('region must be a normalized rectangle of four finite numbers')
    x0, y0, x1, y1 = value
    if not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
        raise WorkspaceError('region must satisfy 0 ≤ x0 < x1 ≤ 1 and 0 ≤ y0 < y1 ≤ 1')
    return value


class SourceVisuals:
    def __init__(self, workspace, sandbox=None):
        self.workspace = workspace
        self.sandbox = sandbox

    def render(self, path, page=1, region=None, execution_id=None, sandbox=None):
        """View exactly one source page/frame without asserting its completeness.

        Page numbers always refer to the original PDF or image frame. The caller
        forwards ``page_images`` to the model; paths never come from model input.
        """
        if type(page) is not int or not 1 <= page <= 2000:
            raise WorkspaceError('page must be an original page number from 1 to 2000')
        region = _region(region)
        source = self.workspace._safe_path(path, file=True)
        self.workspace._check_version(path)
        structure = self.workspace.source_index.structure(path)
        suffix = source.suffix.lower()
        converted = None
        if execution_id is not None:
            sandbox = sandbox or self.sandbox
            if not sandbox or suffix not in {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp"}:
                raise WorkspaceError('Viewing converted pages requires a link to the original Office source and a controlled execution record')
            from pypdf import PdfReader

            from folio.research.sandbox import _safe
            from folio.research.sandbox_conversions import conversion_script

            record = sandbox.verify_execution(execution_id)
            scripts = [conversion_script(operation, path)[0] for operation in ("office_pdf", "office_ocr")]
            extraction = next((item for item in record["artifacts"] if item["path"].endswith("/extraction.json")), None)
            if extraction:
                extraction_path = _safe(self.workspace.task_root, extraction["path"])
                if extraction_path.stat().st_size > 8_000_000:
                    raise WorkspaceError('The OCR extraction record exceeds reading capacity')
                selected = json.loads(extraction_path.read_text(encoding="utf-8")).get("selected_pages")
                if isinstance(selected, list) and len(selected) <= 20:
                    scripts.append(conversion_script("office_ocr", path, pages=selected)[0])
            if record.get("script_sha256") not in {hashlib.sha256(script.encode()).hexdigest() for script in scripts}:
                raise WorkspaceError('Only pages produced by controlled office_pdf or office_ocr conversion may be viewed')
            products = [item for item in record["artifacts"] if item["path"].lower().endswith(".pdf")]
            if len(products) != 1:
                raise WorkspaceError('The controlled conversion did not produce a unique PDF')
            converted = products[0]
            source = _safe(self.workspace.task_root, converted["path"])
            structure = {"metadata": {"page_count": len(PdfReader(source).pages)}}
            suffix = ".pdf"
        with tempfile.TemporaryDirectory(prefix="folio-source-view-") as directory:
            if suffix == ".pdf":
                page_count = structure["metadata"].get("page_count", 0)
                if page > page_count:
                    raise WorkspaceError('The requested page is outside the original PDF range')
                renderer = shutil.which("pdftoppm")
                if not renderer:
                    raise WorkspaceError('Viewing original PDF pages requires Poppler (pdftoppm)')
                prefix = Path(directory) / "page"
                try:
                    subprocess.run(
                        [renderer, "-f", str(page), "-l", str(page), "-scale-to", "3600" if region else "2400",
                         "-png", "-singlefile", str(source), str(prefix)],
                        check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise WorkspaceError('Original PDF page rendering failed or timed out') from exc
                image_path = prefix.with_suffix(".png")
            elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}:
                image_path = source
            else:
                raise WorkspaceError('Direct original viewing supports PDFs and images. Convert Office files to PDF through controlled conversion first')
            with Image.open(image_path) as original:
                if suffix != ".pdf":
                    page_count = getattr(original, "n_frames", 1)
                    if page > page_count:
                        raise WorkspaceError('The requested page is outside the original image-frame range')
                    original.seek(page - 1)
                if original.width * original.height > MAX_PIXELS:
                    raise WorkspaceError('The original image exceeds the reading capacity of 40 million pixels')
                view = original.convert("RGB")
                if region:
                    x0, y0, x1, y1 = region
                    box = (int(x0 * view.width), int(y0 * view.height),
                           math.ceil(x1 * view.width), math.ceil(y1 * view.height))
                    view = view.crop(box)
                view.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
                rendered = Path(directory) / "view.png"
                view.save(rendered, format="PNG")
                value = rendered.read_bytes()
        self.workspace._check_version(path)
        if len(value) > 8 * 1024 * 1024:
            raise WorkspaceError('The original page image exceeds image-input capacity. Select a smaller region')
        sha256 = hashlib.sha256(value).hexdigest()
        target_dir = self.workspace.task_root / ".research" / "source-visuals"
        if any(item.is_symlink() for item in (target_dir, *target_dir.parents)):
            raise WorkspaceError('The original page image storage path cannot pass through symbolic links')
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sha256}.png"
        if target.is_symlink():
            raise WorkspaceError('The original page image cannot be a symbolic link')
        if not target.exists():
            temporary = target.with_suffix("." + uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(value)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        elif hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
            raise WorkspaceError('The saved original page image has changed')
        descriptor = {
            "path": str(target), "sha256": sha256, "page": page,
            "source_id": path, "source_version": self.workspace._manifest[path]["version"],
            "region": region, "provenance": "source_page",
        }
        if converted:
            descriptor.update(provenance="converted_source_page", execution_id=execution_id,
                              converted_sha256=converted["sha256"])
        return {"source_id": path, "page": page, "page_count": page_count, "region": region,
                "page_images": [descriptor], "verified": False,
                "note": "The image is a bounded view of the original page; viewing it does not establish complete extraction or verification."}
