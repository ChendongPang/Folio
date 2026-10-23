"""Pausing must never release a job lock while a render thread still owns its file."""

import asyncio
import threading

import pytest
from filelock import FileLock, Timeout

from folio.orchestration.engine import Engine, _run_local_export
from folio.runtime.demo import DemoRuntime


@pytest.mark.parametrize("fails", [False, True])
async def test_local_export_drains_thread_even_after_repeated_cancellation(fails):
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def write():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Test did not release the render thread"
        finished.set()
        if fails:
            raise ValueError("local export failed during cancellation")
        return "saved"

    task = asyncio.create_task(_run_local_export(write))
    try:
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_export_returns_results_and_real_failures():
    assert await _run_local_export(lambda value, *, suffix: value + suffix, "a", suffix="b") == "ab"

    def fail():
        raise ValueError("render failed")

    with pytest.raises(ValueError, match="render failed"):
        await _run_local_export(fail)


@pytest.mark.parametrize("stage", ["render", "paginate", "pagination_fallback"])
async def test_export_cancellation_keeps_job_lock_and_resumes_saved_body(
    store, demo_job, monkeypatch, stage
):
    from folio.orchestration import engine as engine_module

    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    real_render = engine_module.render
    blocking = True
    pagination_calls = 0

    def block():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "Test did not release the export thread"
        finished.set()

    def render(*args, **kwargs):
        if blocking and stage == "render":
            block()
        real_render(*args, **kwargs)
        # The fixture is a marker template; exercise the structured-template
        # pagination branch while still writing a real DOCX in the worker.
        return {"status": "deferred"}

    def paginate(path):
        nonlocal pagination_calls
        pagination_calls += 1
        if blocking and stage == "pagination_fallback" and pagination_calls == 1:
            raise ValueError("Adjusted layout failed pagination")
        if blocking and stage in {"paginate", "pagination_fallback"}:
            block()
        return {"status": "paginated"}

    async def formatting(*args, **kwargs):
        return {"status": "complete", "changed": stage == "pagination_fallback"}

    monkeypatch.setattr(engine_module, "render", render)
    monkeypatch.setattr("folio.document.pagination.paginate", paginate)
    monkeypatch.setattr("folio.orchestration.formatting.format_document", formatting)
    runner = asyncio.create_task(Engine(store, DemoRuntime()).run(demo_job))
    try:
        await asyncio.wait_for(started.wait(), 10)
        saved = [state.model_dump() for state in store.states(demo_job)]
        assert saved
        calls = store.get(demo_job)["calls"]
        for _ in range(2):
            runner.cancel()
            await asyncio.sleep(0)
            assert not runner.done()
            with pytest.raises(Timeout):
                with FileLock(store.root / f"{demo_job}.lock").acquire(timeout=0):
                    pass
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert finished.is_set()
        assert store.get(demo_job)["artifact"] is None
        assert [state.model_dump() for state in store.states(demo_job)] == saved
        with FileLock(store.root / f"{demo_job}.lock").acquire(timeout=0):
            pass
        blocking = False
        result = await Engine(store, DemoRuntime()).run(demo_job)
        assert result["status"] == "draft"
        assert result["calls"] == calls
        assert [state.model_dump() for state in store.states(demo_job)] == saved
    finally:
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
