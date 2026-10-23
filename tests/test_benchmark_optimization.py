"""Interrupted benchmarks retain their consumed slot and account for the active stage."""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("benchmark_optimization")


def test_explicit_three_run_allowance_counts_failures_and_cannot_start_fourth(benchmark, tmp_path):
    import json

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "source_job": "source",
                "max_full_runs": 3,
                "max_optimization_rounds": 3,
                "optimization_rounds": [{"round": n} for n in range(1, 4)],
                "full_runs": [],
            }
        )
    )
    for number in range(1, 4):
        index, _, _ = benchmark.reserve_run(tmp_path, "source", number, {"plan": "same"}, {})
        benchmark.finish_ledger(
            tmp_path,
            index,
            {
                "passed": False,
                "finished_at": "2026-10-12T00:00:00+00:00",
                "status": "failed",
            },
        )
    with pytest.raises(ValueError, match="Configured full-run limit"):
        benchmark.reserve_run(tmp_path, "source", 3, {"plan": "same"}, {})
    assert len(json.loads(ledger.read_text())["full_runs"]) == 3
    assert not (tmp_path / "run-04").exists()


def test_stop_time_closes_active_stage_and_unfinished_business_task(benchmark):
    events = [
        {"kind": "progress", "created_at": "2026-10-10 00:00:02", "data": {"stage": "generation"}},
        {
            "kind": "work_update",
            "created_at": "2026-10-10 00:00:03",
            "data": {"text": "开始业务任务“任务一”"},
        },
        {
            "kind": "task_started",
            "created_at": "2026-10-10 00:00:04",
            "data": {"key": "generate:1"},
        },
    ]
    result = benchmark.summarize_events(
        events,
        created_at="2026-10-10 00:00:00",
        updated_at="2026-10-10 00:00:02",
        stopped_at="2026-10-10T00:00:10.500+00:00",
    )
    assert result["stage_seconds"] == {"queued": 2.0, "generation": 8.5}
    assert sum(result["stage_seconds"].values()) == result["observed_duration_seconds"] == 10.5
    assert result["business_tasks"][0]["elapsed_seconds"] == 7.5
    assert not result["business_tasks"][0]["completed"]
    assert result["unfinished_attempts"] == {"generate": 1}


def test_latest_event_prevents_stale_job_timestamp_from_erasing_elapsed_time(benchmark):
    result = benchmark.summarize_events(
        [
            {
                "kind": "progress",
                "created_at": "2026-10-10 00:00:09",
                "data": {"stage": "generation"},
            }
        ],
        created_at="2026-10-10 00:00:00",
        updated_at="2026-10-10 00:00:00",
    )
    assert sum(result["stage_seconds"].values()) == 9
    assert all(seconds >= 0 for seconds in result["stage_seconds"].values())


@pytest.mark.parametrize("status", ["running", "queued", "failed", "draft", "published"])
def test_interruption_marks_only_active_isolated_job_failed(benchmark, status):
    class FakeStore:
        writes = []

        def get(self, job_id):
            assert job_id == "isolated-replay"
            return {"status": status}

        def update(self, job_id, **values):
            self.writes.append((job_id, values))

    store = FakeStore()
    benchmark.mark_interrupted_job(store, "isolated-replay", "CancelledError")
    if status in {"running", "queued"}:
        assert store.writes == [
            (
                "isolated-replay",
                {"status": "failed", "error": "Benchmark interrupted (CancelledError)"},
            )
        ]
    else:
        assert not store.writes
