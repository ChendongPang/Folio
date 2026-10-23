"""OCR upgrades preserve provenance and publish body/structure as one generation."""

import json
from pathlib import Path

import pytest
from docx import Document
from PIL import Image
from pypdf import PdfWriter

from folio.models import digest
from folio.research.formats import READER_VERSION, SourceBlock, SourceDocument
from folio.research.workspace import WorkspaceError, WorkspaceManager, evidence_for_block


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    Image.new("RGB", (20, 20), "white").save(source / "scan.png")
    return WorkspaceManager(source, tmp_path / "task")


def register(workspace, text="first", execution="one"):
    return workspace.register_extraction(
        "scan.png", {"blocks": [{"text": text, "metadata": {"page": 1}}]},
        execution_id=execution, image_digest="sha256:environment",
    )


def texts(workspace, **kwargs):
    return [item["text"] for item in workspace.read("scan.png", **kwargs)["evidence"]]


def legacy(workspace, name="scan.png", reader="source-reader-v2.1"):
    current = workspace.source_index.load(name)
    saved = SourceDocument.from_dict(current.model_dump())
    saved.metadata.update(reader_version=reader, page_count=1, complete=True, needs_ocr=False, unread_pages=[])
    office = saved.metadata["format"] in {"docx", "xlsx", "pptx"}
    block = SourceBlock(
        f"page 1/ocr {digest('one' + 'sha256:environment')}/block 1", "retained OCR",
        metadata={"page": 1, "extraction": {
            "method": "ocr", "operation": "office_ocr" if office else "ocr",
            "execution_id": "one", "image_digest": "sha256:environment",
            "source_version": current.source_version,
        }},
    )
    saved.blocks.append(block)
    if office:
        saved.blocks[0].text = "OBSOLETE NATIVE PARSE MUST NOT SURVIVE"
    path = workspace._cache / (digest(name + current.source_version + reader) + ".json")
    path.write_text(json.dumps(saved.model_dump()), encoding="utf-8")
    return path, evidence_for_block(name, saved, block).model_dump()


@pytest.mark.parametrize("reader", ["source-reader-v2.1", READER_VERSION])
def test_legacy_ocr_rebases_native_body_preserving_exact_evidence(tmp_path, reader):
    source = tmp_path / "sources"
    source.mkdir()
    document = Document()
    document.add_paragraph("CURRENT NATIVE BODY")
    document.save(source / "office.docx")
    workspace = WorkspaceManager(source, tmp_path / "task")
    _, evidence = legacy(workspace, "office.docx", reader)
    restored = WorkspaceManager(source, workspace.task_root)
    body = restored.read("office.docx")
    assert [item["text"] for item in body["evidence"]] == ["CURRENT NATIVE BODY", "retained OCR"]
    assert restored.resolve_evidence(evidence).model_dump() == evidence
    assert body["evidence"][-1]["metadata"]["requires_extraction_review"]
    assert restored._overlay_pointer("office.docx").is_file()
    assert [block.text for block in restored.source_index.load("office.docx").blocks] == ["CURRENT NATIVE BODY"]


@pytest.mark.parametrize("field", ["source_version", "execution_id", "image_digest", "locator"])
def test_legacy_ocr_rejects_changed_identity(workspace, field):
    path, _ = legacy(workspace)
    saved = json.loads(path.read_text())
    block = saved["blocks"][-1]
    if field == "locator":
        block["locator"] = "page 1/ocr forged/block 1"
    else:
        block["metadata"]["extraction"][field] = "changed"
    path.write_text(json.dumps(saved))
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    with pytest.raises(WorkspaceError, match='identity|version'):
        restored.read("scan.png")
    assert not restored._overlay_pointer("scan.png").exists()


def test_legacy_migrates_on_first_inspection_then_inspect_is_structure_only(workspace, monkeypatch):
    _, evidence = legacy(workspace)
    assert workspace.inspect("scan.png")["complete"]
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    original = Path.open

    def structure_only(path, *args, **kwargs):
        if path.name.endswith(".content.json") or "content" in path.parts:
            pytest.fail("inspection read body data after migration")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", structure_only)
        assert restored.inspect("scan.png")["node"]["range"]["end_block"] == 1
    assert restored.resolve_evidence(evidence).text == "retained OCR"


@pytest.mark.parametrize("failed_suffix", [".structure.json", ".overlay.json"])
def test_crash_before_publish_keeps_previous_body_and_structure(workspace, monkeypatch, failed_suffix):
    register(workspace)
    previous = workspace._overlay_record("scan.png")
    atomic = workspace._atomic_json

    def fail(path, value):
        if path.name.endswith(failed_suffix):
            raise OSError("injected crash before publishing")
        return atomic(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(workspace, "_atomic_json", fail)
        with pytest.raises(OSError, match="injected"):
            register(workspace, "second", "two")
    assert workspace._overlay_record("scan.png") == previous
    assert texts(workspace) == ["first"]
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    node = restored.inspect("scan.png")["node"]
    assert texts(restored) == texts(restored, node_id=node["node_id"]) == ["first"]
    register(restored, "second", "two")
    node = restored.inspect("scan.png")["node"]
    assert texts(restored) == texts(restored, node_id=node["node_id"]) == ["first", "second"]


def test_first_commit_crash_never_adopts_orphan_body(workspace, monkeypatch):
    original = workspace._atomic_json

    def fail(path, value):
        if path.name.endswith(".structure.json"):
            raise OSError("injected crash")
        return original(path, value)

    monkeypatch.setattr(workspace, "_atomic_json", fail)
    with pytest.raises(OSError):
        register(workspace)
    assert texts(workspace) == []
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    assert texts(restored) == []
    assert not restored.inspect("scan.png")["complete"]


@pytest.mark.parametrize("kind", ["content", "structure"])
def test_corrupt_committed_generation_is_explicit_not_a_native_fallback(workspace, kind):
    register(workspace)
    record = workspace._overlay_record("scan.png")
    path = workspace._cache / record[kind + "_ref"]
    path.write_text("{}")
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    with pytest.raises(WorkspaceError, match='checksum'):
        (restored.read if kind == "content" else restored.inspect)("scan.png")


def test_pointer_cannot_redirect_to_external_file(workspace):
    register(workspace)
    path = workspace._overlay_pointer("scan.png")
    value = json.loads(path.read_text())
    value["structure_ref"] = "../../outside.json"
    path.write_text(json.dumps(value))
    with pytest.raises(WorkspaceError, match='identity'):
        workspace.inspect("scan.png")


def test_ocr_is_task_local_even_when_project_index_is_shared(workspace, tmp_path):
    register(workspace)
    other = WorkspaceManager(workspace.source_root, tmp_path / "other-task")
    assert other.source_index.index_root == workspace.source_index.index_root
    assert texts(other) == []
    assert not other.inspect("scan.png")["complete"]
    assert other.source_index.load("scan.png").blocks == []


def test_inspect_hides_host_block_indices_but_read_preserves_scope(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=100, height=100)
    writer.write(sources / "scan.pdf")
    workspace = WorkspaceManager(sources, tmp_path / "task")
    workspace.register_extraction("scan.pdf", {"blocks": [
        {"text": "page one first", "metadata": {"page": 1}},
        {"text": "page two", "metadata": {"page": 2}},
        {"text": "page one last", "metadata": {"page": 1}},
    ]}, execution_id="one", image_digest="sha256:environment")
    internal = workspace._structure("scan.pdf")
    assert any("block_indices" in node for node in internal["nodes"])
    inspected = workspace.inspect("scan.pdf")
    assert "block_indices" not in json.dumps(inspected)
    page_one = next(node for node in inspected["structure"] if node["metadata"].get("page") == 1)
    assert "block_indices" not in json.dumps(workspace.inspect("scan.pdf", node_id=page_one["node_id"]))
    assert [item["text"] for item in workspace.read("scan.pdf", node_id=page_one["node_id"])["evidence"]] == ["page one first", "page one last"]
