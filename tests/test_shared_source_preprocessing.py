"""Upload, planning and frozen-job retrieval share versioned parsing results."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_source_structure import pdf
from test_workspace_sources import plan
from test_workspace_sources import source_app as source_app

from folio.api.app import create_app
from folio.knowledge.local import LocalKnowledge
from folio.models import JobInput
from folio.orchestration.engine import Engine
from folio.research.contracts import ResearchConfig
from folio.research.source_index import SourceIndex, cached_source
from folio.research.workspace import WorkspaceManager
from folio.runtime.demo import DemoRuntime
from folio.source_documents import SourceDirectoryDrafts, _prepare_inputs


@pytest.fixture
def parsing_calls(monkeypatch):
    import folio.research.source_index as module

    original = module.read_source
    calls = []

    def counted(path):
        calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(module, "read_source", counted)
    return calls


async def test_upload_planning_and_frozen_job_parse_each_version_once(tmp_path, inputs, parsing_calls):
    app = create_app(tmp_path / "app")
    cache_root = app.state.store.root / "source-cache"
    with TestClient(app) as client:
        content = "# 外汇交易\n交易撤销需要主管审批。"
        response = client.post("/uploads?filename=业务规则.md", content=content.encode())
        assert response.status_code == 201
        upload = response.json()
        with app.state.store.connect() as connection:
            saved = dict(connection.execute("SELECT * FROM web_uploads WHERE id=?", (upload["id"],)).fetchone())
        assert len(parsing_calls) == 1
        folder = tmp_path / "directory-inputs"
        catalog = _prepare_inputs(folder, [saved], cache_root=cache_root)
        assert len(parsing_calls) == 1
        expected_name = upload["id"] + "__业务规则.md"
        assert (folder / "sources" / expected_name).read_text() == content
        assert catalog[0]["source_id"] == upload["id"]
        assert "headings" not in catalog[0] and "excerpt" not in catalog[0]
        workspace = WorkspaceManager(folder / "sources", folder / "research-workspace", cache_root=cache_root)
        structure = workspace.inspect(catalog[0]["path"])
        assert [node["title"] for node in structure["structure"] if node["kind"] == "section"] == ["外汇交易"]
        read = workspace.read(catalog[0]["path"])
        assert any("主管审批" in item["text"] for item in read["evidence"])
        assert len(parsing_calls) == 1
        job_id = app.state.store.create(JobInput(
            **(inputs | {"knowledge_path": str(folder / "sources")}), runtime="demo",
            research=ResearchConfig(enabled=True),
        ))
        engine = Engine(app.state.store, DemoRuntime())
        job = await engine.run(job_id)
        assert job["status"] in {"draft", "needs_confirmation"}
        assert isinstance(engine.provider, LocalKnowledge) and engine.provider.index is not None
        assert len(parsing_calls) == 1
        evidence = await engine.provider.search(["主管审批"], limit=10, max_chars=12000)
        assert evidence and all(item.source_id == expected_name for item in evidence)
        assert evidence[0].source_version == catalog[0]["source_version"]


def test_duplicate_uploads_reuse_content_cache_but_changed_bytes_reparse(tmp_path, parsing_calls):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/uploads?filename=first.md", content=b"# Title\nOriginal.")
        second = client.post("/uploads?filename=second.md", content=b"# Title\nOriginal.")
        changed = client.post("/uploads?filename=third.md", content=b"# Title\nChanged.")
        assert first.status_code == second.status_code == changed.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert len(parsing_calls) == 2


def test_same_bytes_with_different_formats_keep_distinct_structures(tmp_path, parsing_calls):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        for name in ("notes.md", "notes.txt"):
            assert client.post("/uploads?filename=" + name, content=b"# Heading\nText.").status_code == 201
    assert len(parsing_calls) == 2
    assert {path.suffix for path in parsing_calls} == {".md", ".txt"}


def test_explicit_index_and_local_knowledge_preserve_evidence_identity(tmp_path, parsing_calls):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "rules.md").write_text("# 审批\n交易撤销需要主管审批。", encoding="utf-8")
    cache_root = tmp_path / "source-cache"
    index = SourceIndex(sources, tmp_path / "index", cache_root=cache_root)
    index.prepare()
    assert len(parsing_calls) == 1
    indexed = LocalKnowledge(sources, index=index)
    assert len(parsing_calls) == 1
    cached = LocalKnowledge(sources, cache_root=cache_root)
    assert len(parsing_calls) == 1
    legacy = LocalKnowledge(sources)
    assert indexed.chunks == cached.chunks == legacy.chunks
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match='index'):
        LocalKnowledge(other, index=index)


def test_legacy_local_knowledge_does_not_create_sidecar_index(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "rules.txt").write_text("交易撤销需要主管审批。", encoding="utf-8")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert LocalKnowledge(source).chunks
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert before == after


def test_standalone_cache_survives_source_rename_and_copy(tmp_path, parsing_calls):
    cache = tmp_path / "source-cache"
    original = tmp_path / "upload.md"
    original.write_text("# 审批\n需要复核。", encoding="utf-8")
    initial = cached_source(original, cache)
    frozen = tmp_path / "job" / "snapshot.md"
    frozen.parent.mkdir()
    shutil.copyfile(original, frozen)
    restored = cached_source(frozen, cache)
    assert initial.model_dump() == restored.model_dump()
    assert len(parsing_calls) == 1


def test_directory_revision_preserves_prepared_index_when_staging_moves(source_app, parsing_calls):
    with TestClient(source_app) as client:
        cid = client.post("/conversations").json()["id"]
        response = client.post(
            f"/conversations/{cid}/sources?filename=rules.md",
            content="# 审批\n交易撤销需要主管审批。".encode(),
        )
        assert response.status_code == 201
        selected = plan(client, cid)
        store = source_app.state.store
        revision = SourceDirectoryDrafts(store.root, source_app.state.preview_library).load(
            selected["directory"]["revision"],
            cid,
        )
        assert (revision["folder"] / "sources.index" / "manifest.json").is_file()
        index = SourceIndex(revision["knowledge_path"], cache_root=store.root / "source-cache")
        index.prepare()
        assert index.index_root == revision["folder"] / "sources.index"
        assert len(index.files) == 1
        source = next(iter(index.files))
        assert index.structure(source)["source_version"] == index.load(source).source_version
        assert len(parsing_calls) == 1


@pytest.mark.parametrize("bookmarks, expected", [
    ([("rules", "Rules", 0, None), ("inputs", "Inputs", 1, "rules")], ["Rules", "Inputs"]),
    ([("later", "Later", 2, None), ("earlier", "Earlier", 0, "later")], ["Earlier", "Later"]),
    ([], []),
])
def test_directory_catalog_uses_real_pdf_bookmarks_and_preserves_page_fallback(tmp_path, parsing_calls, bookmarks, expected):
    original = tmp_path / "rules.pdf"
    pdf(original, bookmarks, pages=3)
    cache = tmp_path / "source-cache"
    cached_source(original, cache)
    folder = tmp_path / "directory"
    catalog = _prepare_inputs(
        folder, [{"id": "original", "name": "业务规则.pdf", "path": str(original)}],
        cache_root=cache,
    )
    assert len(parsing_calls) == 1
    assert "headings" not in catalog[0] and "excerpt" not in catalog[0]
    index = SourceIndex(folder / "sources", cache_root=cache)
    index.prepare()
    structure = index.structure("original__业务规则.pdf")
    assert [node["title"] for node in structure["nodes"] if node["kind"] in {"section", "bookmark"}] == expected
    workspace = WorkspaceManager(folder / "sources", folder / "research-workspace", cache_root=cache)
    inspected = workspace.inspect(catalog[0]["path"])
    assert [node["title"] for node in inspected["structure"] if node["kind"] in {"section", "bookmark"}] == expected
    read = workspace.read(catalog[0]["path"], page=1)
    assert any("Page 1 body" in item["text"] for item in read["evidence"])
    assert len(parsing_calls) == 1
    if not bookmarks:
        assert structure["structure_status"] == "no_outline"
        assert [node["metadata"]["page"] for node in structure["nodes"] if node["kind"] == "page"] == [1, 2, 3]
        assert any("actual page numbers" in warning for warning in catalog[0]["warnings"])
    else:
        assert any("bookmark" in warning for warning in catalog[0]["warnings"])
