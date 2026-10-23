"""Shared index views skip repeated initialization without adopting changed sources."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from folio.research import source_index
from folio.research.source_index import SourceIndexError, get_prepared_index
from folio.research.workspace import WorkspaceManager


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "rules.md").write_text("# Rules\nRequires approval.")
    return root


def test_repeated_workspaces_reuse_preparation_without_opening_sources_or_headers(source_root, tmp_path, monkeypatch):
    first = WorkspaceManager(source_root, tmp_path / "one")
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("reopened immutable source/index"))
    monkeypatch.setattr(source_index, "source_hash", lambda *_: pytest.fail("rehashed immutable source"))
    second = WorkspaceManager(source_root, tmp_path / "two")
    assert second.source_index is first.source_index
    assert second.source_snapshot_id == first.source_snapshot_id
    with pytest.raises(SourceIndexError, match='shared index'):
        first.source_index.prepare()


def test_concurrent_tasks_share_one_prepared_view(source_root):
    with ThreadPoolExecutor(max_workers=8) as pool:
        indexes = list(pool.map(lambda _: get_prepared_index(source_root), range(20)))
    assert all(index is indexes[0] for index in indexes)


def test_cache_root_variants_do_not_invalidate_each_others_unchanged_manifest(source_root, tmp_path, monkeypatch):
    shared = get_prepared_index(source_root, cache_root=tmp_path / "cache")
    standalone = get_prepared_index(source_root)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("cache root switch reread headers"))
    assert get_prepared_index(source_root, cache_root=tmp_path / "cache") is shared
    assert get_prepared_index(source_root) is standalone


def test_new_and_removed_sources_create_new_views_without_mutating_prior_views(source_root):
    first = get_prepared_index(source_root)
    extra = source_root / "extra.md"
    extra.write_text("# New rules\nRequires review.")
    second = get_prepared_index(source_root)
    assert second is not first
    assert "extra.md" not in first.files
    assert "extra.md" in second.files
    extra.unlink()
    third = get_prepared_index(source_root)
    assert third is not second
    assert "extra.md" not in third.files
    assert "extra.md" in second.files


def test_changed_same_size_file_invalidates_reuse_even_when_mtime_restored(source_root):
    first = get_prepared_index(source_root)
    path = source_root / "rules.md"
    stat = path.stat()
    path.write_text(path.read_text().replace("approval", "rechecks"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    current = get_prepared_index(source_root)
    assert current is not first
    assert current.snapshot_id != first.snapshot_id
    with pytest.raises(SourceIndexError, match='changed'):
        first.load("rules.md")
    assert "rechecks" in current.load("rules.md").blocks[-1].text


def test_new_symlink_is_rejected_before_reusing_prepared_view(source_root, tmp_path):
    get_prepared_index(source_root)
    outside = tmp_path / "outside.md"
    outside.write_text("External text")
    (source_root / "link.md").symlink_to(outside)
    with pytest.raises(SourceIndexError, match='symbolic link'):
        get_prepared_index(source_root)


def test_changed_structure_is_repaired_before_reusing_view(source_root):
    original = get_prepared_index(source_root)
    expected = original.structure("rules.md")
    structure = original.index_root / original.files["rules.md"]["structure_ref"]
    structure.write_text("{}")
    current = get_prepared_index(source_root)
    assert current is not original
    assert current.structure("rules.md") == expected


def test_changed_content_is_checked_on_load_and_repaired(source_root):
    original = get_prepared_index(source_root)
    expected = original.load("rules.md")
    content = original.index_root / original.files["rules.md"]["content_ref"]
    content.write_text("{}")
    current = get_prepared_index(source_root)
    assert current is not original
    assert current.load("rules.md") == expected
