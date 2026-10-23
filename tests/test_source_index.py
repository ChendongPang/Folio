"""Shared preprocessing, immutable source identity and separate structure I/O."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from folio.research import source_index
from folio.research.formats import READER_VERSION, SourceFormatError
from folio.research.source_index import SourceIndex, SourceIndexError, cached_source


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "rules.md").write_text("# 交易规则\n" + "金额必须大于零。" * 1000, encoding="utf-8")
    return root


def count_reads(monkeypatch):
    original = source_index.read_source
    seen = []
    lock = threading.Lock()

    def read(path):
        with lock:
            seen.append(path)
        time.sleep(0.01)
        return original(path)

    monkeypatch.setattr(source_index, "read_source", read)
    return seen


def test_prepare_persists_separate_manifest_structure_and_content(sources):
    index = SourceIndex(sources)
    manifest = index.prepare()
    assert index.index_root == sources.with_name("sources.index")
    assert set(path.name for path in sources.iterdir()) == {"rules.md"}
    entry = manifest["files"]["rules.md"]
    assert entry["name"] == "rules.md"
    assert entry["format"] == "md"
    assert entry["status"] == "ready"
    assert manifest["reader_version"] == READER_VERSION
    assert manifest["snapshot_id"] == index.snapshot_id == index.source_snapshot_id
    assert json.loads((index.index_root / "manifest.json").read_text()) == manifest
    structure = index.structure("rules.md")
    assert any(node["title"] == "# 交易规则" or node["title"] == "交易规则" for node in structure["nodes"])
    assert "金额必须大于零。" * 100 not in json.dumps(structure, ensure_ascii=False)
    document = index.load("rules.md")
    assert document.blocks[-1].text == "金额必须大于零。" * 1000
    assert document.source_version == entry["version"] == structure["source_version"]
    assert entry["structure_ref"].startswith("structures/")
    assert entry["content_ref"].startswith("content/")
    assert not (sources / entry["content_ref"]).exists()


def test_structure_does_not_open_content_or_shared_objects(sources, tmp_path, monkeypatch):
    index = SourceIndex(sources, cache_root=tmp_path / "shared")
    index.prepare()
    original = Path.open

    def reject_content(path, *args, **kwargs):
        if "content" in path.parts or "objects" in path.parts:
            pytest.fail("structure query opened a text cache")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_content)
    assert index.structure("rules.md")["nodes"]


def test_project_index_reused_across_managers_and_concurrent_prepare(sources, monkeypatch):
    seen = count_reads(monkeypatch)

    def prepare(_):
        index = SourceIndex(sources)
        index.prepare()
        return index.load("rules.md").model_dump(), index.snapshot_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(prepare, range(12)))
    assert len(seen) == 1
    assert all(result == results[0] for result in results)


def test_content_cache_shared_by_version_without_project_metadata(sources, tmp_path, monkeypatch):
    seen = count_reads(monkeypatch)
    other = tmp_path / "project-two"
    other.mkdir()
    (other / "renamed.md").write_bytes((sources / "rules.md").read_bytes())
    cache = tmp_path / "shared"

    def prepare(root):
        index = SourceIndex(root, cache_root=cache)
        index.prepare()
        return index

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(prepare, [sources, other]))
    assert len(seen) == 1
    assert first.load("rules.md").model_dump() == second.load("renamed.md").model_dump()
    assert set(second.files) == {"renamed.md"}
    objects = list((cache / "objects").glob("*.json"))
    assert len(objects) == 1
    text = objects[0].read_text()
    assert "rules.md" not in text and "renamed.md" not in text
    assert str(sources) not in text and str(other) not in text


def test_object_cache_separates_formats_with_identical_bytes(tmp_path, monkeypatch):
    seen = count_reads(monkeypatch)
    markdown, plain = tmp_path / "a.md", tmp_path / "a.txt"
    markdown.write_text("# 交易规则\n具体要求")
    plain.write_bytes(markdown.read_bytes())
    cache = tmp_path / "shared"
    md = cached_source(markdown, cache)
    txt = cached_source(plain, cache)
    assert md.blocks[0].kind == "heading"
    assert txt.blocks[0].kind == "paragraph"
    assert len(seen) == 2
    assert len(list((cache / "objects").glob("*.json"))) == 2


def test_changed_source_never_silently_advances_old_snapshot(sources):
    original = SourceIndex(sources)
    original.prepare()
    old_id = original.snapshot_id
    (sources / "rules.md").write_text("# 新规则\n必须复核")
    with pytest.raises(SourceIndexError, match='changed'):
        original.load("rules.md")
    with pytest.raises(SourceIndexError, match='changed'):
        original.structure("rules.md")
    current = SourceIndex(sources)
    current.prepare()
    assert current.snapshot_id != old_id
    assert current.load("rules.md").blocks[-1].text == "必须复核"
    with pytest.raises(SourceIndexError, match='changed'):
        original.load("rules.md")


def test_corrupt_content_rebuilt_without_changing_source_identity(sources, monkeypatch):
    seen = count_reads(monkeypatch)
    index = SourceIndex(sources)
    index.prepare()
    snapshot = index.snapshot_id
    expected = index.load("rules.md").model_dump()
    content = index.index_root / index.files["rules.md"]["content_ref"]
    payload = json.loads(content.read_text())
    payload["blocks"][-1]["text"] = "伪造的审批结论"
    content.write_text(json.dumps(payload))
    assert index.load("rules.md").model_dump() == expected
    assert index.snapshot_id == snapshot
    assert len(seen) == 2
    content.unlink()
    assert index.load("rules.md").model_dump() == expected
    assert len(seen) == 3


def test_corrupt_shared_cache_reparsed(sources, tmp_path, monkeypatch):
    seen = count_reads(monkeypatch)
    cache = tmp_path / "shared"
    expected = cached_source(sources / "rules.md", cache).model_dump()
    object_path = next((cache / "objects").glob("*.json"))
    value = json.loads(object_path.read_text())
    value["document"]["blocks"][-1]["text"] = "错误内容"
    object_path.write_text(json.dumps(value))
    assert cached_source(sources / "rules.md", cache).model_dump() == expected
    assert len(seen) == 2


def test_concurrent_content_repair_rechecks_inside_lock(sources, monkeypatch):
    seen = count_reads(monkeypatch)
    index = SourceIndex(sources)
    index.prepare()
    content = index.index_root / index.files["rules.md"]["content_ref"]
    content.write_text("corrupt")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: index.load("rules.md").model_dump(), range(12)))
    assert len(seen) == 2
    assert all(result == results[0] for result in results)


def test_corrupt_structure_requires_prepare_and_cannot_fall_back_to_body(sources):
    index = SourceIndex(sources)
    index.prepare()
    expected = index.structure("rules.md")
    structure = index.index_root / index.files["rules.md"]["structure_ref"]
    structure.write_text("{}")
    with pytest.raises(SourceIndexError, match='structure index'):
        index.structure("rules.md")
    index.prepare()
    assert index.structure("rules.md") == expected


def test_reader_version_invalidates_index_without_changing_snapshot(sources, monkeypatch):
    seen = count_reads(monkeypatch)
    index = SourceIndex(sources)
    index.prepare()
    snapshot = index.snapshot_id
    path = index.index_root / "manifest.json"
    value = json.loads(path.read_text())
    value["reader_version"] = "obsolete"
    path.write_text(json.dumps(value))
    current = SourceIndex(sources)
    current.prepare()
    assert len(seen) == 2
    assert current.snapshot_id == snapshot
    assert current.manifest["reader_version"] == READER_VERSION


def test_unreadable_source_is_explicit_in_manifest_and_load(sources):
    (sources / "broken.docx").write_bytes(b"not a valid docx")
    index = SourceIndex(sources)
    index.prepare()
    entry = index.files["broken.docx"]
    assert entry["status"] == "unreadable"
    assert entry["warnings"]
    assert not index.structure("broken.docx")["complete"]
    assert index.load("rules.md").blocks
    with pytest.raises(SourceFormatError):
        index.load("broken.docx")


@pytest.mark.parametrize("relative", ["../rules.md", "/etc/passwd", "a/../../rules.md", "a\\b", "\x00", "missing.md"])
def test_source_paths_must_belong_to_frozen_manifest(sources, relative):
    index = SourceIndex(sources)
    index.prepare()
    with pytest.raises(SourceIndexError):
        index.load(relative)
    with pytest.raises(SourceIndexError):
        index.structure(relative)


def test_rejects_symlink_sources_and_cache_entries(sources, tmp_path):
    external = tmp_path / "outside.md"
    external.write_text("外部内容")
    (sources / "linked.md").symlink_to(external)
    index = SourceIndex(sources)
    with pytest.raises(SourceIndexError, match='symbolic link'):
        index.prepare()
    (sources / "linked.md").unlink()
    index.prepare()
    content = index.index_root / index.files["rules.md"]["content_ref"]
    content.unlink()
    content.symlink_to(external)
    with pytest.raises(SourceIndexError, match='symbolic link'):
        index.load("rules.md")
    assert external.read_text() == "外部内容"


def test_rejects_symlink_roots_and_writing_under_source(sources, tmp_path):
    linked = tmp_path / "linked"
    linked.symlink_to(sources, target_is_directory=True)
    with pytest.raises(SourceIndexError, match='symbolic link'):
        SourceIndex(linked)
    with pytest.raises(SourceIndexError, match='read-only source'):
        SourceIndex(sources, sources / "index")
    with pytest.raises(SourceIndexError, match='read-only source'):
        SourceIndex(sources, cache_root=sources / "cache")


def test_index_movable_and_manifest_cannot_be_mutated_by_caller(sources, tmp_path):
    index = SourceIndex(sources)
    index.prepare()
    snapshot = index.snapshot_id
    index.manifest["files"].clear()
    assert index.files
    moved_sources = tmp_path / "moved-sources"
    moved_index = tmp_path / "moved-index"
    sources.rename(moved_sources)
    index.index_root.rename(moved_index)
    current = SourceIndex(moved_sources, moved_index)
    current.prepare()
    assert current.snapshot_id == snapshot
    assert current.load("rules.md").blocks


def test_requires_explicit_prepare(sources):
    index = SourceIndex(sources)
    with pytest.raises(SourceIndexError, match="[Pp]repare"):
        index.load("rules.md")
    with pytest.raises(SourceIndexError, match="[Pp]repare"):
        _ = index.manifest
