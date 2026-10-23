"""The same source reader serves chat, research and cancellable job startup."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock
from openpyxl import Workbook
from PIL import Image
from test_source_structure import pdf

from folio.api.workspace import material_context, source_material_context
from folio.knowledge.local import LocalKnowledge
from folio.models import JobInput, Limits
from folio.orchestration.engine import Engine
from folio.research import source_index
from folio.research.source_index import SourceIndex, SourceIndexError, cached_source
from folio.runtime.demo import DemoRuntime


def test_binary_chat_materials_share_upload_reader_and_report_image_limit(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("rules.xlsx", "rules.pdf", "diagram.png")]
    book = Workbook()
    book.active.title = "审批规则"
    book.active.append(["金额", "审批人"])
    book.active.append([1000, "主管"])
    book.save(paths[0])
    pdf(paths[1], [], pages=1)
    Image.new("RGB", (100, 100), "white").save(paths[2])
    cache = tmp_path / "cache"
    for path in paths:
        cached_source(path, cache)
    monkeypatch.setattr(source_index, "read_source", lambda *_: pytest.fail("chat reparsed uploaded bytes"))
    materials = material_context([
        {"id": str(number), "name": path.name, "role": "source", "path": str(path)}
        for number, path in enumerate(paths)
    ], cache_root=cache)
    assert [item["parsed"]["format"] for item in materials] == ["xlsx", "pdf", "png"]
    assert "主管" in materials[0]["parsed"]["excerpt"]
    assert "Page 1 body" in materials[1]["parsed"]["excerpt"]
    assert materials[2]["parsed"]["needs_ocr"]
    assert not materials[2]["parsed"]["source_complete"]
    assert not materials[2]["parsed"]["excerpt"]
    assert all("path" not in item for item in materials)


def test_chat_source_preview_is_bounded_and_does_not_claim_full_load(tmp_path):
    path = tmp_path / "long.md"
    path.write_text("\n".join(f"# Chapter {i}\n" + "details " * 1000 for i in range(30)))
    value = source_material_context(path, max_chars=1200)
    assert len(json.dumps(value, ensure_ascii=False)) <= 1200
    assert value["source_complete"]
    assert value["excerpt_truncated"] and value["structure_truncated"]
    assert source_material_context(path, max_chars=20) is None


def test_unchanged_index_preparation_and_navigation_do_not_rehash_sources(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "one.md").write_text("# Rules\nNeeds approval.")
    (sources / "two.md").write_text("# Other rules\nNeeds review.")
    first = SourceIndex(sources)
    first.prepare()
    calls = []
    original = source_index.source_hash

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(source_index, "source_hash", counted)
    second = SourceIndex(sources)
    second.prepare()
    for name in first.files:
        assert second.validate(name) == first.validate(name)
        assert second.structure(name) == first.structure(name)
        assert second.load(name) == first.load(name)
    assert not calls


def test_source_identity_detects_equal_size_edit_with_restored_mtime(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    path = root / "rules.md"
    path.write_text("# Original\nA")
    index = SourceIndex(root)
    index.prepare()
    before = path.stat()
    path.write_text("# Original\nB")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(SourceIndexError, match='changed'):
        index.validate("rules.md")
    with pytest.raises(SourceIndexError, match='changed'):
        index.load("rules.md")


def test_preparation_can_cancel_while_waiting_for_another_index_writer(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "rules.md").write_text("# Rules\nNeeds approval.")
    index = SourceIndex(sources)
    cancelled = Event()
    with FileLock(index.index_root / ".prepare.lock"), ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(index.prepare, cancelled=cancelled)
        cancelled.set()
        with pytest.raises(SourceIndexError, match='cancelled'):
            waiting.result(timeout=1)
    assert not index.prepared


def test_prepared_knowledge_reuses_lexical_work_without_shared_mutable_evidence(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "rules.md").write_text("# Rules\nNeeds approval.")
    index = SourceIndex(root)
    index.prepare()
    monkeypatch.setattr(index, "prepare", lambda **_: pytest.fail("prepared index prepared again"))
    initial = LocalKnowledge(root, index=index)
    expected = initial.chunks[0].text
    initial.chunks[0].text = "mutated by one task"
    import folio.knowledge.local as local

    monkeypatch.setattr(local, "terms", lambda *_: pytest.fail("warm lexical index tokenized again"))
    warm = LocalKnowledge(root, index=index)
    assert warm.chunks[0].text == expected
    warm.chunks[0].text = "mutated by another task"
    assert LocalKnowledge(root, index=index).chunks[0].text == expected


@pytest.mark.parametrize("cancel_directly", [False, True])
async def test_job_preparation_runs_off_loop_and_stops_before_execution(store, inputs, monkeypatch, cancel_directly):
    started, stopped = Event(), Event()
    import folio.orchestration.engine as module

    def slow_knowledge(directory, *, cache_root, cancelled):
        started.set()
        try:
            assert cancelled.wait(2), "preparation was not cancelled"
            raise SourceIndexError("资料预处理已取消")
        finally:
            stopped.set()

    monkeypatch.setattr(module, "LocalKnowledge", slow_knowledge)
    engine = Engine(store, DemoRuntime())
    engine._execute = AsyncMock()
    job_id = store.create(JobInput(
        **inputs, runtime="demo", limits=Limits(run_timeout_seconds=2 if cancel_directly else 0.1),
    ))
    run = asyncio.create_task(engine.run(job_id))
    # This coroutine must run while synchronous preparation is still waiting.
    async with asyncio.timeout(1):
        while not started.is_set():
            await asyncio.sleep(0.001)
    assert not stopped.is_set()
    if cancel_directly:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        with pytest.raises(TimeoutError):
            await run
        assert store.get(job_id)["status"] == "failed"
    assert stopped.is_set()
    engine._execute.assert_not_awaited()
    assert not hasattr(engine, "provider")
