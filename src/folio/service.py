"""Composition root used by both CLI and workers."""

import asyncio
import os
from pathlib import Path

from filelock import FileLock

from folio.model_settings import ModelSettingsStore
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime
from folio.store import Store

PAUSE_POLL_SECONDS = 0.2


async def _wait_for_pause(store: Store, job_id: str) -> None:
    # Read durable state so controls work even when a separate CLI worker owns the job.
    while store.job_status(job_id) not in {"pausing", "paused"}:
        await asyncio.sleep(PAUSE_POLL_SECONDS)


async def _drain(task: asyncio.Task) -> None:
    """Finish cancellation cleanup before releasing the job's ownership lock."""
    if not task.done() and not task.cancelling():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break  # Retrieved below; Engine already records actual execution errors.
    if not task.cancelled():
        task.exception()


def resolve_model_config(root: Path, config: Path | None = None) -> Path:
    configured = config or os.environ.get("FOLIO_MODEL_CONFIG")
    if not configured:
        saved = ModelSettingsStore(root).path
        configured = saved if saved.is_file() else None
    if not configured:
        raise ValueError("请先在模型设置中保存配置。")
    return Path(configured).expanduser()


async def execute(store: Store, job_id: str, config: Path | None = None) -> dict:
    # Acquire ownership before constructing a runtime or changing job status.
    store.get(job_id)
    with FileLock(store.root / f"{job_id}.lock").acquire(timeout=0):
        job = store.get(job_id)
        if job["status"] in {"published", "draft", "needs_confirmation", "paused"}:
            return job
        if job["status"] == "pausing":
            return store.acknowledge_pause(job_id)
        try:
            if job["input"]["runtime"] not in {"llm", "demo"}:
                raise ValueError("此任务属于旧引擎，请使用原材料创建新任务。")
            if job["input"]["runtime"] == "demo":
                runtime = DemoRuntime()
            else:
                from folio.runtime.llm import LLMRuntime

                configured = resolve_model_config(store.root, config)
                runtime = LLMRuntime(Path(configured))
        except Exception as exc:
            store.update(job_id, status="failed", error=f"Runtime initialization failed: {exc}")
            raise
        work = pause = None
        try:
            work = asyncio.create_task(Engine(store, runtime).run(job_id, already_locked=True))
            pause = asyncio.create_task(_wait_for_pause(store, job_id))
            done, _ = await asyncio.wait({work, pause}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await pause
                await _drain(work)
        finally:
            if pause is not None:
                await _drain(pause)
            if work is not None:
                await _drain(work)
            # Keep the lock and pausing state until provider connections have closed.
            close = asyncio.create_task(runtime.aclose())
            interrupted = False
            try:
                while not close.done():
                    try:
                        await asyncio.shield(close)
                    except asyncio.CancelledError:
                        interrupted = True
                await close
            finally:
                store.acknowledge_pause(job_id)
            if interrupted:
                raise asyncio.CancelledError
        return store.get(job_id)
