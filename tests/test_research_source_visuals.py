import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter

from folio.research.sandbox import SandboxError
from folio.research.sandbox_conversions import conversion_script
from folio.research.source_visuals import SourceVisuals
from folio.research.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    Image.new("RGB", (400, 200), "white").save(source / "diagram.png")
    return WorkspaceManager(source, tmp_path / "task")


def test_region_view_is_bounded_and_preserves_original_identity(workspace):
    result = SourceVisuals(workspace).render("diagram.png", region=[0.25, 0, 0.75, 1])
    descriptor = result["page_images"][0]
    assert not result["verified"]
    assert descriptor["source_id"] == "diagram.png"
    assert descriptor["source_version"] == workspace._manifest["diagram.png"]["version"]
    with Image.open(descriptor["path"]) as image:
        assert image.size == (200, 200)
    assert hashlib.sha256(Path(descriptor["path"]).read_bytes()).hexdigest() == descriptor["sha256"]
    assert SourceVisuals(workspace).render("diagram.png", region=[0.25, 0, 0.75, 1]) == result


@pytest.mark.parametrize("arguments", [
    {"page": True}, {"page": 2}, {"region": [0, 0, 2, 1]},
    {"region": [0, 0, float("nan"), 1]}, {"region": [0, 0, 0, 1]},
])
def test_source_views_reject_invalid_page_or_region(workspace, arguments):
    with pytest.raises(WorkspaceError):
        SourceVisuals(workspace).render("diagram.png", **arguments)


def test_source_view_rejects_changed_original(workspace):
    Image.new("RGB", (100, 100), "red").save(workspace.source_root / "diagram.png")
    with pytest.raises(WorkspaceError, match='changed'):
        SourceVisuals(workspace).render("diagram.png")


def test_pdf_view_renders_only_requested_page(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    writer = PdfWriter()
    for _ in range(200):
        writer.add_blank_page(100, 100)
    writer.write(source / "long.pdf")
    workspace = WorkspaceManager(source, tmp_path / "task")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        Image.new("RGB", (100, 100)).save(Path(command[-1]).with_suffix(".png"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("folio.research.source_visuals.shutil.which", lambda name: name)
    monkeypatch.setattr("folio.research.source_visuals.subprocess.run", run)
    result = SourceVisuals(workspace).render("long.pdf", page=180)
    assert result["page_images"][0]["page"] == 180
    assert len(calls) == 1
    assert calls[0][calls[0].index("-f") + 1] == "180"
    assert calls[0][calls[0].index("-l") + 1] == "180"


@pytest.mark.parametrize("pages", [[], [0], [True], [2, 2], list(range(1, 22)), [2001]])
def test_ocr_page_selection_rejects_invalid_ranges(pages):
    with pytest.raises(SandboxError, match='page'):
        conversion_script("ocr", "scan.pdf", pages=pages)


def test_selected_ocr_keeps_original_numbering_on_long_document(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    output = tmp_path / "outputs"
    output.mkdir()
    writer = PdfWriter()
    for _ in range(300):
        writer.add_blank_page(100, 100)
    writer.write(source / "scan.pdf")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "pdftoppm":
            Image.new("RGB", (20, 20)).save(Path(command[-1]).with_suffix(".png"))
        return SimpleNamespace(returncode=0, stdout="approval <= 1000", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("FOLIO_ARTIFACTS", str(output))
    script, _ = conversion_script("ocr", "scan.pdf", pages=[180])
    # Execute only our fixed converter script with mocked native programs.
    script = script.replace("pathlib.Path('/sources')", f"pathlib.Path({str(source)!r})")
    exec(compile(script, "trusted-converter", "exec"), {})
    result = json.loads((output / "extraction.json").read_text())
    assert result["page_count"] == 300 and result["selected_pages"] == [180]
    assert result["blocks"][0]["locator"] == "page:180"
    assert len(calls) == 2


def test_office_view_requires_trusted_converter_with_original_source_identity(tmp_path, monkeypatch):
    from docx import Document

    source = tmp_path / "sources"
    source.mkdir()
    document = Document()
    document.add_paragraph("Approval flow")
    document.save(source / "rules.docx")
    workspace = WorkspaceManager(source, tmp_path / "task")
    output = workspace.task_root / "artifacts" / "ocr-1"
    output.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(output / "source-preview.pdf")
    (output / "extraction.json").write_text(json.dumps({"selected_pages": [1]}))
    script, _ = conversion_script("office_ocr", "rules.docx", pages=[1])
    record = {
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "artifacts": [{"path": f"artifacts/ocr-1/{path.name}",
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                      for path in output.iterdir()],
    }
    sandbox = SimpleNamespace(verify_execution=lambda execution_id: record)

    def run(command, **kwargs):
        Image.new("RGB", (100, 100)).save(Path(command[-1]).with_suffix(".png"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("folio.research.source_visuals.shutil.which", lambda name: name)
    monkeypatch.setattr("folio.research.source_visuals.subprocess.run", run)
    result = SourceVisuals(workspace).render("rules.docx", execution_id="ocr-1", sandbox=sandbox)
    descriptor = result["page_images"][0]
    assert descriptor["source_id"] == "rules.docx"
    assert descriptor["source_version"] == workspace._manifest["rules.docx"]["version"]
    assert descriptor["provenance"] == "converted_source_page"
    record["script_sha256"] = hashlib.sha256(b"arbitrary Python").hexdigest()
    with pytest.raises(WorkspaceError, match='controlled'):
        SourceVisuals(workspace).render("rules.docx", execution_id="ocr-1", sandbox=sandbox)


@pytest.mark.parametrize("task", ["research_step", "verify_visual_extraction"])
async def test_research_images_use_vision_transport_and_capacity_reserve(tmp_path, workspace, task):
    from folio.model_settings import ModelSettingsInput, ModelSettingsStore
    from folio.research.contracts import ResearchStep
    from folio.research.visual_extraction import VisualExtractionReview
    from folio.runtime.llm import LLMRuntime

    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="vision",
                                     no_auth=True, supports_image_input=True))
    runtime = LLMRuntime(settings.path)
    context = SourceVisuals(workspace).render("diagram.png")
    schema = VisualExtractionReview if task == "verify_visual_extraction" else ResearchStep
    try:
        messages, *_ = runtime.request_parts(task, context, schema.model_json_schema())
        parts = messages[1]["content"]
        assert parts[-1]["type"] == "image_url"
        assert "diagram.png" in parts[-2]["text"]
        assert context["page_images"][0]["path"] not in parts[0]["text"]
        measured = await runtime.measure(task, context, schema.model_json_schema(), 20000)
        assert measured["image_token_reserve"] == 4096
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("supported,complete,source_matches", [
    (True, False, False), (True, True, False), (False, True, True), (False, True, False),
])
def test_visual_review_cannot_approve_unsupported_or_mismatched_content(supported, complete, source_matches):
    from folio.research.visual_extraction import VisualExtractionReview

    with pytest.raises(ValueError):
        VisualExtractionReview(supported=supported, complete=complete, source_matches=source_matches,
                               reason="独立原图判断")


def test_visual_review_can_accept_partial_truth_without_claiming_page_complete():
    from folio.research.visual_extraction import VisualExtractionReview

    review = VisualExtractionReview(supported=True, complete=False, source_matches=True,
                                    reason="已核对驳回箭头，页面中的其他分支尚未完整表达")
    assert review.supported and not review.complete
