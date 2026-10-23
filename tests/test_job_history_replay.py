"""Restoring persisted job history never masquerades as fresh execution."""

import json

import pytest

from folio.api import workspace


def frame(value):
    lines = value.splitlines()
    kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
    data = next((line[6:] for line in lines if line.startswith("data: ")), None)
    return kind, json.loads(data) if data else None


def work(index, *, kind="work_update", data=None):
    return {
        "id": index,
        "kind": kind,
        "data": data if data is not None else {"text": f"Saved step {index}"},
    }


class HistoryStore:
    def __init__(self, artifact, *, status="needs_confirmation", events=()):
        self.job = {
            "id": "job",
            "status": status,
            "stage": "complete" if status != "running" else "generation",
            "artifact": str(artifact),
            "snapshot": {"blocking_issues": [{"description": "Company name is pending"}]},
        }
        self.saved = list(events)
        self.get_calls = 0
        self.events_calls = 0

    def get(self, job_id):
        assert job_id == "job"
        self.get_calls += 1
        return self.job.copy()

    def events(self, job_id, after):
        assert job_id == "job"
        self.events_calls += 1
        return [event for event in self.saved if event["id"] > after][:500]

    def states(self, job_id):
        return []


@pytest.fixture
def history(tmp_path, monkeypatch):
    artifact = tmp_path / "result.docx"
    artifact.write_bytes(b"Saved artifact")
    monkeypatch.setattr(workspace, "artifact_filename", lambda job: "result.docx")
    monkeypatch.setattr(workspace, "confirmation_history", lambda store, job_id: [])
    return artifact


@pytest.mark.parametrize("status", ["published", "draft", "needs_confirmation", "failed", "paused"])
async def test_snapshot_precedes_any_history_access(history, status):
    store = HistoryStore(history, status=status, events=[work(1)])
    stream = workspace.job_events(store, "job")
    assert frame(await anext(stream)) == (
        "job.snapshot", {"job_id": "job", "status": status, "stage": "complete"},
    )
    assert store.get_calls == 1
    assert store.events_calls == 0
    await stream.aclose()


@pytest.mark.parametrize("after", [0, 499])
async def test_all_history_batches_and_cursor_drain_without_sleep(history, monkeypatch, after):
    store = HistoryStore(history, events=[work(index) for index in range(1, 1202)])
    review = {"status": "completed", "round_id": "round-1", "unit_id": "unit-1"}
    store.saved[800]["data"]["review"] = review

    async def no_sleep(delay):
        pytest.fail("Persisted history must drain without polling delays")

    monkeypatch.setattr(workspace.asyncio, "sleep", no_sleep)
    events = [frame(value) async for value in workspace.job_events(store, "job", after)]
    assert events[0][0] == "job.snapshot"
    updates = [data for kind, data in events if kind == "work.update"]
    assert [data["event_id"] for data in updates] == list(range(after + 1, 1202))
    assert next(data["review"] for data in updates if data["event_id"] == 801) == review
    assert events[-1][0] == "artifact.ready"
    assert events[-1][1]["status"] == "needs_confirmation"
    assert events[-1][1]["confirmation_url"] == "/jobs/job/confirmation"
    assert events[-1][1]["pending_count"] == 1
    assert store.get_calls == 1 + (1201 - after + 499) // 500


async def test_retry_diagnostics_share_each_batch_job_snapshot(history, monkeypatch):
    saved = [
        work(index, kind="task_failed" if index % 2 else "task_started", data={"attempt": 2})
        for index in range(1, 501)
    ]
    store = HistoryStore(history, status="failed", events=saved)
    snapshots = []

    def diagnostic(store, job, event):
        snapshots.append(job)
        return "Saved retry", {"code": "test"}

    monkeypatch.setattr(workspace, "retry_details", diagnostic)
    monkeypatch.setattr(workspace, "retry_started_details", diagnostic)
    monkeypatch.setattr(workspace, "job_failure", lambda store, job: {"code": "test"})
    monkeypatch.setattr(workspace, "failure_text", lambda failure: "Saved failure")
    events = [frame(value) async for value in workspace.job_events(store, "job")]
    assert len(snapshots) == 500
    assert all(job is snapshots[0] for job in snapshots)
    assert store.get_calls == 2
    assert events[-1][0] == "job.failed"
    assert len([kind for kind, _ in events if kind == "work.update"]) == 500


async def test_live_completion_drains_last_update_before_terminal(history, monkeypatch):
    store = HistoryStore(history, status="running", events=[work(1)])

    async def no_sleep(delay):
        pytest.fail("Newly persisted final progress must drain before waiting")

    monkeypatch.setattr(workspace.asyncio, "sleep", no_sleep)
    stream = workspace.job_events(store, "job")
    assert frame(await anext(stream))[1]["status"] == "running"
    assert frame(await anext(stream))[1]["event_id"] == 1
    store.saved.append(work(2))
    store.job.update(status="needs_confirmation", stage="complete")
    remaining = [frame(value) async for value in stream]
    assert [kind for kind, _ in remaining] == ["work.update", "artifact.ready"]
    assert remaining[0][1]["event_id"] == 2


async def test_polling_waits_only_for_future_events(history, monkeypatch):
    store = HistoryStore(history, status="running")
    sleeps = []

    async def finish_while_waiting(delay):
        sleeps.append(delay)
        store.saved.append(work(1))
        store.job.update(status="draft", stage="complete")

    monkeypatch.setattr(workspace.asyncio, "sleep", finish_while_waiting)
    events = [frame(value) async for value in workspace.job_events(store, "job")]
    assert sleeps == [0.6]
    assert [kind for kind, _ in events] == ["job.snapshot", None, "work.update", "artifact.ready"]
    assert events[-2][1]["event_id"] == 1


async def test_paused_history_still_ends_with_paused_event(history):
    store = HistoryStore(history, status="paused", events=[work(1)])
    events = [frame(value) async for value in workspace.job_events(store, "job")]
    assert [kind for kind, _ in events] == ["job.snapshot", "work.update", "job.paused"]


async def test_completed_result_without_loading_history(history):
    store = HistoryStore(history, events=[work(i) for i in range(1, 2001)])
    events = [frame(value) async for value in workspace.job_events(store, "job", replay_history=False)]
    assert [kind for kind, _ in events] == ["job.snapshot", "artifact.ready"]
    assert store.events_calls == 0
    assert events[-1][1]["download_url"] == "/jobs/job/artifact"


def test_job_elapsed_is_wall_time_not_parallel_task_sum():
    assert workspace.job_elapsed_ms({
        'created_at': '2026-10-23 09:10:41',
        'updated_at': '2026-10-23 09:27:42',
    }) == 1021000
    assert workspace.job_elapsed_ms({}) is None


async def test_result_includes_actual_job_duration(history):
    store = HistoryStore(history)
    store.job.update(created_at='2026-10-23 09:10:41', updated_at='2026-10-23 09:27:42')
    events = [frame(value) async for value in workspace.job_events(store, 'job', replay_history=False)]
    assert events[-1][1]['elapsed_ms'] == 1021000
