"""Immutable generated document previews and cached PDF/page representations."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from filelock import FileLock

from folio.document.pagination import paginate
from folio.document.reader import read_docx


class PreviewLibrary:
    def __init__(self, root: Path):
        self.root = root / "previews"
        self.root.mkdir(parents=True, exist_ok=True)
        # Read-only lookup for previously saved directory previews. No template catalogue.
        self.legacy_root = root / "templates"

    def add(self, source: Path, name: str):
        read_docx(source)
        preview_id = hashlib.sha256(source.read_bytes()).hexdigest()[:32]
        folder = self.root / preview_id
        with FileLock(self.root / (preview_id + ".lock")):
            folder.mkdir(exist_ok=True)
            manifest = folder / "metadata.json"
            if not manifest.exists():
                shutil.copyfile(source, folder / "source.docx")
                temporary = folder / "metadata.tmp"
                temporary.write_text(
                    json.dumps({"id": preview_id, "name": name}, ensure_ascii=False),
                    encoding="utf-8",
                )
                temporary.replace(manifest)
        return self.get(preview_id)

    def path(self, preview_id):
        if len(preview_id) != 32 or any(c not in "0123456789abcdef" for c in preview_id):
            raise KeyError(preview_id)
        for root in (self.root, self.legacy_root):
            path = root / preview_id / "source.docx"
            if path.is_file():
                return path
        raise KeyError(preview_id)

    def get(self, preview_id):
        path = self.path(preview_id)
        metadata = json.loads(path.with_name("metadata.json").read_text(encoding="utf-8"))
        return {
            "id": preview_id,
            "name": metadata["name"],
            "preview_url": f"/previews/{preview_id}/preview",
            "download_url": f"/previews/{preview_id}/download",
        }

    def structure(self, preview_id):
        return [
            {"kind": b.kind, "text": b.text, "level": b.level, "rows": b.rows}
            for b in read_docx(self.path(preview_id)).blocks
        ]

    def preview(self, preview_id):
        source = self.path(preview_id)
        pdf = source.with_name("preview.pdf")
        with FileLock(source.with_name("preview.lock"), timeout=60):
            if not pdf.is_file():
                with tempfile.TemporaryDirectory(prefix="folio-preview-") as directory:
                    copy = Path(directory) / "document.docx"
                    shutil.copyfile(source, copy)
                    result = Path(directory) / "preview.pdf"
                    paginate(copy, pdf=result, required=True)
                    shutil.copyfile(result, source.with_name("preview.tmp"))
                    source.with_name("preview.tmp").replace(pdf)
        return pdf

    def pages(self, preview_id):
        """Raster pages display consistently in browsers without a PDF viewer."""
        pdf = self.preview(preview_id)
        destination = pdf.parent / "pages"
        with FileLock(pdf.with_name("pages.lock"), timeout=60):
            if not destination.is_dir():
                renderer = shutil.which("pdftoppm")
                if not renderer:
                    raise RuntimeError("Page preview requires pdftoppm")
                with tempfile.TemporaryDirectory(dir=pdf.parent, prefix="pages-") as directory:
                    subprocess.run(
                        [
                            renderer,
                            "-scale-to",
                            "1200",
                            "-png",
                            str(pdf),
                            str(Path(directory) / "page"),
                        ],
                        capture_output=True,
                        check=True,
                        timeout=90,
                    )
                    if not list(Path(directory).glob("page-*.png")):
                        raise RuntimeError("Page renderer produced no pages")
                    Path(directory).rename(destination)
        return sorted(destination.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
