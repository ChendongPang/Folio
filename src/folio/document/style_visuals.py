"""Bounded original-page images for optional example-style analysis.

This module only prepares visual inputs. A prepared page is not a claim that a
model has seen it; the style research loop records actual message delivery.
"""

import hashlib
import re
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from folio.document.page_preview import (
    immutable_page_images,
    materialize_page_images,
    prepare_page_preview,
)

MAX_PAGES = 3
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 1500
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}


def _reason(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return "页面转换或渲染超时"
    if isinstance(
        exc, (Image.DecompressionBombError, Image.DecompressionBombWarning)
    ) or "pixel limit" in str(exc):
        return "原始图片超过像素读取容量"
    if "byte limit" in str(exc):
        return "图像超过模型输入容量"
    if isinstance(exc, subprocess.SubprocessError):
        return "页面转换或渲染程序失败"
    if isinstance(exc, RuntimeError):
        return "Office 页面转换不可用或未能完成"
    return "文件或页面读取失败"


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _selected(total, limit):
    if total < 1:
        raise ValueError("The source contains no pages or frames")
    if limit == 1:
        return [1]
    if limit == 2:
        return sorted({1, total})
    return sorted({1, (total + 1) // 2, total})


def _sample(descriptor, document_id, total, source_hash, *, image=False):
    page = descriptor["page"]
    descriptor = descriptor | {
        "source_id": document_id,
        "source_sha256": source_hash,
        "provenance": "style_reference_image" if image else "style_reference_page",
    }
    reasons = []
    if page == 1:
        reasons.append("first_frame" if image else "cover")
    if page == total:
        reasons.append("end")
    if page not in {1, total}:
        reasons.append("middle")
    return {
        "sample_id": f"{document_id}-P{page}",
        "document_id": document_id,
        "locator": f"frame {page}" if image else f"page {page}",
        "kind": "page_image",
        "characters": 0,
        "text": f"Original {'frame' if image else 'page'} {page} image. Visual reference only; not project facts.",
        "truncated": False,
        "reasons": reasons,
        "page_image": descriptor,
    }


def _pdf_preview(path, directory):
    tools = {name: shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm")}
    if not all(tools.values()):
        return {"status": "skipped", "reason": "requires_poppler"}
    info = subprocess.run(
        [tools["pdfinfo"], str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout
    count = re.search(r"^Pages:\s*(\d+)", info, re.MULTILINE)
    if not count or int(count[1]) < 1:
        raise ValueError("Cannot determine original PDF page count")
    return {
        "status": "rendered",
        "total_pages": int(count[1]),
        "pdf": str(path),
        "image_directory": str(directory),
    }


def _images(path, directory, output, document_id, limit, source_hash, result):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError("Original frame exceeds the 40 million pixel limit")
            # Animated GIF/WebP frames require decoding preceding frames. For
            # these formats, use at most the first three frames instead of
            # seeking through an arbitrarily long animation to reach its end.
            sequential = source.format in {"GIF", "WEBP", "PNG"} and getattr(
                source, "is_animated", False
            )
            total = None if sequential else getattr(source, "n_frames", 1)
            result["total_pages"] = total
            for page in range(1, limit + 1) if sequential else _selected(total, limit):
                try:
                    source.seek(page - 1)
                    if source.width * source.height > MAX_PIXELS:
                        raise ValueError("Original frame exceeds the 40 million pixel limit")
                    # EXIF orientation is visual layout. Render transparent image
                    # backgrounds on white, without retaining source metadata.
                    frame = ImageOps.exif_transpose(source).convert("RGBA")
                    frame.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
                    background = Image.new("RGBA", frame.size, "white")
                    background.alpha_composite(frame)
                    rendered = directory / f"frame-{page}.png"
                    background.convert("RGB").save(rendered, format="PNG")
                    if rendered.stat().st_size > MAX_IMAGE_BYTES:
                        raise ValueError("Prepared frame exceeds the image input byte limit")
                    descriptor = immutable_page_images(
                        [{"page": page, "path": str(rendered)}], output / "pages"
                    )[0]
                    result["samples"].append(
                        _sample(descriptor, document_id, total or 0, source_hash, image=True)
                    )
                except EOFError:
                    result["total_pages"] = page - 1
                    break
                except (OSError, ValueError, Image.DecompressionBombError) as exc:
                    result["warnings"].append(
                        f"{document_id} 第 {page} 帧未生成视觉样本：{_reason(exc)}。"
                    )
            if sequential and result["total_pages"] is None:
                result["warnings"].append(f"{document_id} 仅抽样前 {limit} 帧，未遍历动画全部帧。")


def style_page_samples(path, output_dir, *, document_id="D1", max_pages=MAX_PAGES):
    """Prepare at most three representative pages/frames, with immutable hashes.

    DOCX pagination uses the existing private-copy Office worker with bounded
    subprocess timeouts. Only selected PDF pages are rasterized, one at a time.
    The caller owns cancellation shielding around this synchronous operation.
    """
    result = {"samples": [], "warnings": []}
    if type(max_pages) is not int or max_pages < 0:
        result["warnings"].append("视觉抽样页数必须为非负整数；未生成视觉样本。")
        return result
    limit = min(max_pages, MAX_PAGES)
    if not limit:
        return result
    path, output = Path(path), Path(output_dir)
    try:
        if not path.is_file():
            result["warnings"].append(f"{document_id} 文件不存在，未生成视觉样本。")
            return result
        if any(part.is_symlink() for part in (output / "pages", output, *output.parents)):
            raise ValueError("Style visual output cannot traverse symlinks")
        output.mkdir(parents=True, exist_ok=True)
        source_hash = _hash(path)
        result["source_sha256"] = source_hash
        with tempfile.TemporaryDirectory(prefix="render-", dir=output) as working:
            directory = Path(working)
            suffix = path.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                _images(path, directory, output, document_id, limit, source_hash, result)
            elif suffix in {".docx", ".pdf"}:
                preview = (
                    prepare_page_preview(path, directory, max_pages=limit)
                    if suffix == ".docx"
                    else _pdf_preview(path, directory)
                )
                if preview["status"] != "rendered":
                    result["warnings"].append(
                        f"{document_id} 未生成页面视觉样本：{preview.get('reason', 'preview_unavailable')}。"
                    )
                    return result
                total = preview["total_pages"]
                result["total_pages"] = total
                for page in _selected(total, limit):
                    try:
                        # Nonconsecutive pages must never become a single wide
                        # Poppler range (which would rasterize hundreds of pages).
                        rendered = materialize_page_images(preview, [{"page": page}])
                        if Path(rendered[0]["path"]).stat().st_size > MAX_IMAGE_BYTES:
                            raise ValueError("Prepared page exceeds the image input byte limit")
                        descriptor = immutable_page_images(rendered, output / "pages")[0]
                        result["samples"].append(
                            _sample(descriptor, document_id, total, source_hash)
                        )
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        result["warnings"].append(
                            f"{document_id} 第 {page} 页未生成视觉样本：{_reason(exc)}。"
                        )
            else:
                result["warnings"].append(f"{document_id} 当前格式不支持页面视觉抽样。")
        if _hash(path) != source_hash:
            result["samples"] = []
            result["warnings"].append(f"{document_id} 抽样期间原文件发生变化，视觉样本已弃用。")
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        result["samples"] = []
        result["warnings"].append(f"{document_id} 未完成视觉抽样：{_reason(exc)}。")
    return result
