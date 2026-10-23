from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

from folio.document.style_pdf import measure_pdf_layout
from folio.document.style_sampling import measure_source
from folio.research.formats import SourceBlock, SourceDocument


def native_pdf(path: Path, contents: bytes, *, rotation=0, unit=1, crop=None, font_name="/ABCDEF+Helvetica", icc_profile=None, descriptor=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject(font_name),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    if descriptor:
        font[NameObject("/FontDescriptor")] = DictionaryObject({NameObject(key): NumberObject(value) for key, value in descriptor.items()})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    if icc_profile is not None:
        profile = DecodedStreamObject()
        profile.set_data(icc_profile)
        profile[NameObject("/N")] = NumberObject(3)
        page["/Resources"][NameObject("/ColorSpace")] = DictionaryObject({
            NameObject("/Cs1"): ArrayObject([NameObject("/ICCBased"), writer._add_object(profile)]),
        })
    stream = DecodedStreamObject()
    stream.set_data(contents)
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Rotate")] = NumberObject(rotation)
    page[NameObject("/UserUnit")] = FloatObject(unit)
    if crop:
        page[NameObject("/CropBox")] = ArrayObject([FloatObject(value) for value in crop])
    writer.write(path)


def test_native_pdf_font_size_color_and_page_dimensions(tmp_path):
    path = tmp_path / "native.pdf"
    native_pdf(path, b"0 0 1 rg BT /F1 12 Tf 1 0 0 1 72 700 Tm (Readable native text) Tj ET")
    before = path.read_bytes()
    layout, coverage = measure_pdf_layout(path)
    assert layout["body"] == {"font_name": "Helvetica", "font_size_pt": 12, "color": "0000FF"}
    assert layout["page"] == {"width_mm": 215.9, "height_mm": 279.4}
    assert coverage["pages_measured"] == 1
    assert coverage["characters_measured"] == len("Readablenativetext")
    assert {item["dimension"] for item in coverage["observations"]} == {"typography", "page", "visual"}
    assert not any("margin" in key for key in layout["page"])
    assert path.read_bytes() == before


def test_pdf_crop_rotation_unit_and_text_matrix_are_respected(tmp_path):
    path = tmp_path / "transformed.pdf"
    native_pdf(
        path,
        b"0 0.5 -0.5 0 600 0 cm BT /F1 1 Tf 24 0 0 24 20 50 Tm (Transformed text) Tj ET",
        rotation=90, unit=2, crop=(10, 20, 310, 420),
    )
    layout, coverage = measure_pdf_layout(path)
    # 1 pt * 24 (text matrix) * 0.5 (page matrix) * 2 (UserUnit).
    assert layout["body"]["font_size_pt"] == 24
    assert layout["page"]["width_mm"] == pytest.approx(400 * 2 * 25.4 / 72, abs=0.01)
    assert layout["page"]["height_mm"] == pytest.approx(300 * 2 * 25.4 / 72, abs=0.01)
    assert coverage["font_sizes"] == [{"size_pt": 24, "characters": len("Transformedtext")}]


@pytest.mark.parametrize("matrix", [b"2 0 0 1", b"1 0 0.5 1"])
def test_stretched_or_skewed_text_is_not_promoted_as_normal_font_size(tmp_path, matrix):
    path = tmp_path / "stretched.pdf"
    native_pdf(path, matrix + b" 0 0 cm BT /F1 12 Tf (Skewed text) Tj ET")
    layout, coverage = measure_pdf_layout(path)
    assert "font_size_pt" not in layout["body"]
    assert coverage["font_sizes"] == []
    assert any("倾斜" in item for item in coverage["limitations"])
    assert coverage["unknown_font_size_characters"] == 0
    transform = coverage["font_transforms"][0]
    assert transform["characters"] == len("Skewedtext")
    assert transform["pages"] == [1]
    if matrix == b"2 0 0 1":
        assert (transform["horizontal_em_pt"], transform["vertical_em_pt"], transform["axis_angle_degrees"]) == (24, 12, 90)
    else:
        assert transform["horizontal_em_pt"] == 12
        assert transform["vertical_em_pt"] == pytest.approx(13.416, abs=0.001)
        assert transform["axis_angle_degrees"] == pytest.approx(63.435, abs=0.001)
    assert any("无需多模态" in item["text"] for item in coverage["observations"])


def test_font_size_is_weighted_by_characters_not_number_of_runs(tmp_path):
    path = tmp_path / "weights.pdf"
    native_pdf(
        path,
        b"BT /F1 30 Tf (Title) Tj /F1 12 Tf (This paragraph has far more characters than the title.) Tj ET",
    )
    layout, _ = measure_pdf_layout(path)
    assert layout["body"]["font_size_pt"] == 12


def test_mixed_text_colors_are_observed_without_claiming_a_global_color(tmp_path):
    path = tmp_path / "colors.pdf"
    native_pdf(path, b"BT /F1 12 Tf 1 0 0 rg (Red) Tj 0 0 1 rg (Blue) Tj ET")
    layout, coverage = measure_pdf_layout(path)
    assert "color" not in layout["body"]
    observation = next(item["text"] for item in coverage["observations"] if item["dimension"] == "visual")
    assert "#FF0000" in observation and "#0000FF" in observation


def test_pdf_text_blocks_and_unrecognized_tables_are_not_reported_as_reliable_counts():
    source = SourceDocument("hash", {"format": "pdf"}, [
        SourceBlock("page 1/block 1", "Title and several paragraphs"),
        SourceBlock("page 2/block 1", "Table cells extracted as plain text"),
    ])
    stats = measure_source(source)
    assert stats["text_block_count"] == 2
    assert stats["paragraph_count_reliable"] is False
    assert stats["table_count_reliable"] is False
    # Legacy numeric fields remain compatible; their reliability must be consulted.
    assert stats["paragraph_count"] == 2 and stats["table_count"] == 0


@pytest.mark.parametrize("font_name,expected", [
    ("/ABCDEF+TimesNewRomanPSMT", "Times New Roman"),
    ("/ABCDEF+CustomInternalGlyphName", None),
])
def test_postscript_font_names_only_become_known_word_families(tmp_path, font_name, expected):
    path = tmp_path / "font.pdf"
    native_pdf(path, b"BT /F1 12 Tf (Readable text) Tj ET", font_name=font_name)
    layout, coverage = measure_pdf_layout(path)
    assert layout["body"].get("font_name") == expected
    assert coverage["fonts"][0]["name"] == font_name.split("+")[1]


def test_embedded_icc_srgb_text_and_stroke_colors_need_no_vision(tmp_path):
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    path = tmp_path / "icc.pdf"
    native_pdf(
        path, b"/Cs1 cs 0 .4392157 .7529412 sc /Cs1 CS 0 0 0 SC 2 Tr BT /F1 12 Tf (Blue outlined text) Tj ET",
        icc_profile=profile,
    )
    layout, coverage = measure_pdf_layout(path)
    assert layout["body"]["color"] == "0070C0"
    assert coverage["text_strokes"] == [{"color": "000000", "draw_operations": 1}]
    assert any("#0070C0" in item["text"] for item in coverage["observations"])
    assert not any("配置不可转换" in item for item in coverage["limitations"])


def test_invalid_icc_is_unknown_instead_of_assumed_rgb(tmp_path):
    path = tmp_path / "invalid-profile.pdf"
    native_pdf(path, b"/Cs1 cs 0 .4 .8 sc BT /F1 12 Tf (Unknown color) Tj ET", icc_profile=b"not an ICC profile")
    layout, coverage = measure_pdf_layout(path)
    assert "color" not in layout["body"]
    assert any("配置不可转换" in item for item in coverage["limitations"])


def test_font_bold_and_italic_are_observations_not_global_body_settings(tmp_path):
    path = tmp_path / "emphasis.pdf"
    native_pdf(
        path, b"BT /F1 12 Tf (Directory title) Tj ET", font_name="/ABCDEF+TimesNewRomanPS-BoldItalicMT",
        descriptor={"/Flags": 68, "/ItalicAngle": -8},
    )
    layout, coverage = measure_pdf_layout(path)
    assert coverage["font_styles"] == [{
        "name": "TimesNewRomanPS-BoldItalicMT", "bold": True, "italic": True,
        "characters": len("Directorytitle"), "pages": [1],
    }]
    assert "bold" not in layout["body"] and "italic" not in layout["body"]
    assert any("粗体斜体" in item["text"] for item in coverage["observations"])
    assert coverage["has_structural_tags"] is False
    assert any("并非必须使用多模态" in item["text"] for item in coverage["observations"])


def test_failed_pdf_page_keeps_partial_measurements_but_is_incomplete(tmp_path, monkeypatch):
    from pypdf import PageObject

    path = tmp_path / "partial.pdf"
    native_pdf(path, b"BT /F1 12 Tf (Partial readable text) Tj ET")
    extract = PageObject.extract_text

    def failing_extract(page, *args, **kwargs):
        extract(page, *args, **kwargs)
        raise ValueError("late text operator failure")

    monkeypatch.setattr(PageObject, "extract_text", failing_extract)
    layout, coverage = measure_pdf_layout(path)
    assert layout["body"]["font_size_pt"] == 12
    assert coverage["failed_pages"] == [1]
    assert coverage["pages_measured"] == 0
    assert coverage["complete"] is False


@pytest.mark.parametrize("rotation,complete,unmeasured", [(45, False, [1]), (0, True, [])])
def test_pdf_geometry_failure_is_distinct_from_a_page_without_native_text(tmp_path, rotation, complete, unmeasured):
    path = tmp_path / "page-without-text.pdf"
    native_pdf(path, b"", rotation=rotation)
    _, coverage = measure_pdf_layout(path)
    assert coverage["geometry_unmeasured_pages"] == unmeasured
    assert coverage["geometry_pages_measured"] == 1 - len(unmeasured)
    assert coverage["pages_without_text"] == [1]
    assert coverage["failed_pages"] == []
    assert coverage["complete"] is complete
