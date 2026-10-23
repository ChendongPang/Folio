"""Repeated cancellation cannot remove a snapshot under an active preparer."""

import asyncio
import threading

import pytest

from folio.previews import PreviewLibrary
from folio.source_documents import SourceDirectoryDrafts


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancel_joins_source_preparation_before_cleanup(tmp_path, monkeypatch, worker_fails):
    import folio.source_documents as source_documents

    root = tmp_path / "state"
    drafts = SourceDirectoryDrafts(root, PreviewLibrary(root))
    original = source_documents._prepare_inputs
    source = tmp_path / "rules.md"
    source.write_text("# 订单规则\n付款前允许撤销。", encoding="utf-8")
    attachments = [{"id": "a" * 32, "name": "rules.md", "path": str(source)}]
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    folders = []

    def blocked_prepare(folder, files, **kwargs):
        folders.append(folder)
        (folder / "copying.marker").write_text("active", encoding="utf-8")
        started.set()
        assert release.wait(5), "test must release the preparation worker"
        try:
            # Cleanup may only happen after this worker has completed.
            assert (folder / "copying.marker").is_file()
            if worker_fails:
                raise ValueError("preparation failed while cancellation was pending")
            return original(folder, files, **kwargs)
        finally:
            finished.set()

    async def must_not_create(*args, **kwargs):
        pytest.fail("cancelled preprocessing must never continue to model planning")

    monkeypatch.setattr(source_documents, "_prepare_inputs", blocked_prepare)
    monkeypatch.setattr(drafts, "create", must_not_create)
    task = asyncio.create_task(drafts.create_from_sources(None, "conversation", attachments, "编制订单文档"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
        assert folders[0].is_dir()
        assert (folders[0] / "copying.marker").is_file()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert not folders[0].exists()
    assert not list(drafts.root.glob("source-input-*"))
    assert source.read_text(encoding="utf-8") == "# 订单规则\n付款前允许撤销。"
    if not worker_fails:
        # Joining does not undo a completed shared preprocessing cache write.
        assert list((root / "source-cache" / "objects").glob("*.json"))
