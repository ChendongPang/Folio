import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from PIL import Image

from folio.document import page_preview, style_visuals
from folio.document.style_visuals import style_page_samples


@pytest.fixture
def pdf_tools(tmp_path, monkeypatch):
    source = tmp_path / "reference.pdf"
    source.write_bytes(b"fake PDF; tools are mocked")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] <= 90
        if Path(command[0]).name == "pdfinfo":
            return SimpleNamespace(stdout="Pages: 500\n")
        first = int(command[command.index("-f") + 1])
        last = int(command[command.index("-l") + 1])
        assert first == last, "never rasterize a wide first-to-last PDF range"
        if command[0] == "pdftotext":
            return SimpleNamespace(stdout=f"原始第 {first} 页内容\f")
        assert command[0] == "pdftoppm"
        Image.new("RGB", (50, 60), "white").save(f"{command[-1]}-{first:03}.png")
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(style_visuals.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(style_visuals.subprocess, "run", run)
    return source, commands


def test_large_pdf_rasterizes_only_three_selected_pages_and_hashes_saved_images(
    pdf_tools, tmp_path
):
    source, commands = pdf_tools
    output = tmp_path / "visuals"
    result = style_page_samples(source, output, document_id="D3", max_pages=99)
    assert not result["warnings"]
    assert result["total_pages"] == 500
    assert [sample["sample_id"] for sample in result["samples"]] == ["D3-P1", "D3-P250", "D3-P500"]
    assert [sample["reasons"] for sample in result["samples"]] == [["cover"], ["middle"], ["end"]]
    assert len(commands) == 7  # pdfinfo + one text/image subprocess for each selected page
    assert not list(output.glob("render-*"))
    for sample in result["samples"]:
        descriptor = sample["page_image"]
        assert sample["characters"] == 0 and not sample["truncated"]
        assert "Original page" in sample["text"]
        assert (
            hashlib.sha256(Path(descriptor["path"]).read_bytes()).hexdigest()
            == descriptor["sha256"]
        )
        assert descriptor["source_sha256"] == result["source_sha256"]
        assert Path(descriptor["path"]).parent == (output / "pages").resolve()


def test_two_page_budget_uses_first_and_last_pages(pdf_tools, tmp_path):
    result = style_page_samples(pdf_tools[0], tmp_path / "visuals", max_pages=2)
    assert [item["locator"] for item in result["samples"]] == ["page 1", "page 500"]


def test_docx_conversion_uses_private_copy_and_only_selected_pdf_images(
    pdf_tools, tmp_path, monkeypatch
):
    source = tmp_path / "example.docx"
    doc = Document()
    doc.add_paragraph("示例不得被修改")
    doc.save(source)
    original = source.read_bytes()
    conversions = []

    def paginate(path, *, pdf):
        conversions.append(path)
        assert path != source and path.read_bytes() == original
        path.write_bytes(b"private converted copy")
        pdf.write_bytes(b"private pdf")
        return {"status": "paginated"}

    monkeypatch.setattr(page_preview, "paginate", paginate)
    result = style_page_samples(source, tmp_path / "visuals", max_pages=3)
    assert not result["warnings"]
    assert source.read_bytes() == original
    assert len(conversions) == 1
    assert [item["locator"] for item in result["samples"]] == ["page 1", "page 250", "page 500"]


def test_missing_office_returns_clear_warning_without_claiming_a_page(tmp_path, monkeypatch):
    source = tmp_path / "example.docx"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        style_visuals,
        "prepare_page_preview",
        lambda *args, **kwargs: {"status": "skipped", "reason": "requires_office"},
    )
    result = style_page_samples(source, tmp_path / "visuals")
    assert not result["samples"] and "requires_office" in result["warnings"][0]


def test_timed_out_page_is_not_in_samples_and_other_pages_can_survive(
    pdf_tools, tmp_path, monkeypatch
):
    original = style_visuals.materialize_page_images

    def materialize(preview, pages):
        if pages[0]["page"] == 250:
            raise subprocess.TimeoutExpired("pdftoppm", 90)
        return original(preview, pages)

    monkeypatch.setattr(style_visuals, "materialize_page_images", materialize)
    result = style_page_samples(pdf_tools[0], tmp_path / "visuals")
    assert [sample["locator"] for sample in result["samples"]] == ["page 1", "page 500"]
    assert "250" in result["warnings"][0] and "超时" in result["warnings"][0]


def test_source_mutation_discards_all_prepared_pages(pdf_tools, tmp_path, monkeypatch):
    source, _ = pdf_tools
    original = style_visuals.materialize_page_images

    def materialize(preview, pages):
        result = original(preview, pages)
        source.write_bytes(b"changed")
        return result

    monkeypatch.setattr(style_visuals, "materialize_page_images", materialize)
    result = style_page_samples(source, tmp_path / "visuals")
    assert not result["samples"] and "原文件发生变化" in result["warnings"][0]


def test_images_are_bounded_oriented_and_stored_as_immutable_png(tmp_path):
    source = tmp_path / "reference.jpg"
    Image.new("RGB", (3000, 1000), "white").save(source)
    result = style_page_samples(source, tmp_path / "visuals", document_id="D2")
    assert not result["warnings"]
    assert result["total_pages"] == 1
    assert len(result["samples"]) == 1
    sample = result["samples"][0]
    assert sample["locator"] == "frame 1"
    with Image.open(sample["page_image"]["path"]) as image:
        assert image.size == (1500, 500) and image.format == "PNG"


def test_visual_sampling_accepts_original_larger_than_previous_byte_limit(tmp_path):
    source = tmp_path / "large-original.png"
    Image.new("RGB", (20, 30), "white").save(source)
    with source.open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    result = style_page_samples(source, tmp_path / "visuals", document_id="D2")
    assert not result["warnings"]
    assert result["total_pages"] == 1
    assert len(result["samples"]) == 1
    descriptor = result["samples"][0]["page_image"]
    with Image.open(descriptor["path"]) as image:
        assert image.size == (20, 30)
    assert Path(descriptor["path"]).stat().st_size < style_visuals.MAX_IMAGE_BYTES
    assert descriptor["source_sha256"] == result["source_sha256"]


def test_missing_original_still_has_a_clear_visual_warning(tmp_path):
    result = style_page_samples(tmp_path / "missing.png", tmp_path / "visuals")
    assert result["samples"] == []
    assert result["warnings"] == ["D1 文件不存在，未生成视觉样本。"]


def test_animated_image_reads_only_three_initial_frames(tmp_path):
    source = tmp_path / "example.gif"
    frames = [
        Image.new("RGB", (20, 30), color) for color in ("red", "green", "blue", "yellow", "black")
    ]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=50, loop=0)
    result = style_page_samples(source, tmp_path / "visuals", max_pages=10)
    assert len(result["samples"]) == 3
    assert [sample["locator"] for sample in result["samples"]] == ["frame 1", "frame 2", "frame 3"]
    assert result["total_pages"] is None
    assert "未遍历动画全部帧" in result["warnings"][0]


def test_oversized_image_does_not_decode_into_a_model_sample(tmp_path, monkeypatch):
    source = tmp_path / "example.png"
    Image.new("RGB", (11, 10), "white").save(source)
    monkeypatch.setattr(style_visuals, "MAX_PIXELS", 100)
    result = style_page_samples(source, tmp_path / "visuals")
    assert not result["samples"]
    assert "像素" in result["warnings"][0]


def test_disabled_visual_budget_does_not_open_source(tmp_path):
    result = style_page_samples(tmp_path / "missing.pdf", tmp_path / "visuals", max_pages=0)
    assert result == {"samples": [], "warnings": []}
    assert not (tmp_path / "visuals").exists()
