"""Review progress is attributable across concurrent groups and durable diagnostics."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from folio.orchestration.review_trace import (
    record_cache_hit,
    record_call_failure,
    record_model_call,
    review_calls,
)


def counts(model=0, cache=0, failed=0):
    return {"model_calls": model, "cache_hits": cache, "failed_calls": failed}


async def test_parallel_scopes_separate_siblings_and_inherit_into_child_tasks():
    ready = [asyncio.Event(), asyncio.Event()]

    async def group(index, task, repetitions):
        with review_calls() as stats:
            ready[index].set()
            await ready[1 - index].wait()

            async def child():
                for _ in range(repetitions):
                    record_model_call(task)
                    await asyncio.sleep(0)
                record_cache_hit(task)
                if index:
                    record_call_failure(task)

            await asyncio.create_task(child())
        return stats.snapshot()

    with review_calls() as total:
        first, second = await asyncio.gather(group(0, "review", 2), group(1, "drill_down", 3))
    assert first == counts(2, 1) | {"tasks": {"review": counts(2, 1)}}
    assert second == counts(3, 1, 1) | {"tasks": {"drill_down": counts(3, 1, 1)}}
    assert total.snapshot() == counts(5, 2, 1) | {
        "tasks": {"review": counts(2, 1), "drill_down": counts(3, 1, 1)}
    }


def test_nested_scope_restores_after_exception_and_snapshots_are_independent():
    record_model_call("outside")
    with review_calls() as outer:
        with pytest.raises(RuntimeError):
            with review_calls() as inner:
                record_model_call("review")
                record_call_failure("review")
                raise RuntimeError("stop inner scope")
        record_cache_hit("drill_down")
    record_model_call("outside")
    assert inner.snapshot() == counts(1, 0, 1) | {"tasks": {"review": counts(1, 0, 1)}}
    snapshot = outer.snapshot()
    assert snapshot == counts(1, 1, 1) | {
        "tasks": {"review": counts(1, 0, 1), "drill_down": counts(0, 1)}
    }
    snapshot["tasks"]["review"]["model_calls"] = 999
    assert outer.snapshot()["tasks"]["review"]["model_calls"] == 1
    with review_calls() as fresh:
        assert fresh.snapshot() == counts() | {"tasks": {}}


async def test_cancelled_scope_does_not_leak_into_following_work():
    entered = asyncio.Event()

    async def cancel_me():
        with review_calls() as cancelled:
            record_model_call("review")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                record_call_failure("review")
                raise
            finally:
                snapshots.append(cancelled.snapshot())

    snapshots = []
    with review_calls() as outer:
        child = asyncio.create_task(cancel_me())
        await entered.wait()
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        record_cache_hit("drill_down")
    assert snapshots == [counts(1, 0, 1) | {"tasks": {"review": counts(1, 0, 1)}}]
    assert outer.snapshot()["model_calls"] == 1 and outer.snapshot()["cache_hits"] == 1
    with review_calls() as fresh:
        record_model_call("review")
    assert fresh.snapshot()["model_calls"] == 1


async def test_inherited_thread_context_records_without_lost_updates():
    def worker():
        for _ in range(100):
            record_model_call("select_context")

    with review_calls() as stats:
        await asyncio.gather(*(asyncio.to_thread(worker) for _ in range(4)))
    assert stats.snapshot() == counts(400) | {"tasks": {"select_context": counts(400)}}


def test_review_event_is_persisted_and_logged_as_single_line_json(store, demo_job, caplog):
    review = {"phase": "global_review", "group": 2, "section_ids": ["s1", "s2"], **counts(1, 2)}
    with caplog.at_level(logging.INFO, logger="folio.review"):
        store.work_event(demo_job, "一致性检查完成\n已保留统计", review=review)
        store.work_event(demo_job, "普通进度消息")
    review["model_calls"] = 999
    event = next(event for event in store.events(demo_job) if "review" in event["data"])
    assert event["data"]["review"]["model_calls"] == 1
    lines = (store.root / "jobs" / demo_job / "review.jsonl").read_text().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["event_id"] == event["id"] and logged["created_at"] == event["created_at"]
    assert logged["job_id"] == demo_job
    assert {key: logged[key] for key in ("text", "review")} == event["data"]
    messages = [r.getMessage() for r in caplog.records if r.name == "folio.review"]
    assert messages == lines
    assert "\n" not in messages[0]
    assert store.events(demo_job)[-1]["data"] == {"text": "普通进度消息"}


async def test_concurrent_review_logs_remain_complete_and_queryable(store, demo_job):
    await asyncio.gather(
        *(
            asyncio.to_thread(
                store.work_event, demo_job, "分组完成", review={"group": group, **counts(1)}
            )
            for group in range(12)
        )
    )
    records = [
        json.loads(line)
        for line in (store.root / "jobs" / demo_job / "review.jsonl").read_text().splitlines()
    ]
    assert len(records) == 12 and {r["review"]["group"] for r in records} == set(range(12))
    assert len({r["event_id"] for r in records}) == 12
    events = {event["id"]: event["data"] for event in store.events(demo_job)}
    assert all(events[r["event_id"]] == {"text": r["text"], "review": r["review"]} for r in records)


def test_review_log_file_failure_keeps_durable_event(store, demo_job, monkeypatch, caplog):
    original = Path.open

    def fail_review_log(path, *args, **kwargs):
        if path.name == "review.jsonl":
            raise OSError("sensitive filesystem diagnostic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_review_log)
    with caplog.at_level(logging.INFO, logger="folio.review"):
        store.work_event(demo_job, "检查完成", review=counts(1))
    assert store.events(demo_job)[-1]["data"] == {"text": "检查完成", "review": counts(1)}
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert json.loads(warning.getMessage())["diagnostic"] == "review_log_write_failed"
    assert "sensitive" not in warning.getMessage()
