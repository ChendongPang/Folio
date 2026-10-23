from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from folio.models import Extraction, SectionContent, SectionState
from folio.orchestration.work import WorkPlan, WorkState, WorkTask
from folio.store import SectionRevisionConflict, Store


def section(section_id, revision=1):
    return SectionState(
        section_id=section_id,
        revision=revision,
        content=SectionContent(),
        extraction=Extraction(summary=f"版本 {revision}"),
        evidence=[],
    )


def install_reviewed_tasks(store, job_id):
    store.save_work_plan(
        job_id,
        WorkPlan(
            tasks=[
                WorkTask(
                    task_id=task_id,
                    title=task_id,
                    objective="业务目标",
                    section_ids=[sid],
                    questions=["业务问题"],
                    completion_criteria=["业务完成"],
                    rationale="依赖新增产物",
                    depends_on=dependencies,
                )
                for task_id, sid, dependencies in [
                    ("a", "a", []),
                    ("b", "b", ["a"]),
                    ("independent", "c", []),
                ]
            ]
        ),
    )
    for task_id in ("a", "b", "independent"):
        store.save_work_state(job_id, WorkState(task_id=task_id, review_key="passed"))


def test_transaction_failure_rolls_back_all_sections_reviews_artifact_and_events(
    store,
    demo_job,
    monkeypatch,
):
    store.save_sections_batch(demo_job, [section("a"), section("b")])
    install_reviewed_tasks(store, demo_job)
    store.update(demo_job, snapshot={"review_passed": True}, artifact=Path("before.docx"))
    before_events = store.events(demo_job)
    real_event = store._event

    def fail_second_save(conn, job_id, kind, data):
        if kind == "section_saved" and data["section_id"] == "b":
            raise RuntimeError("disk failure during second section")
        real_event(conn, job_id, kind, data)

    monkeypatch.setattr(store, "_event", fail_second_save)
    with pytest.raises(RuntimeError, match="disk failure"):
        store.save_sections_batch(
            demo_job,
            [section("a", 2), section("b", 2)],
            expected_revisions={"a": 1, "b": 1},
        )
    assert {s.revision for s in store.states(demo_job)} == {1}
    assert all(w.review_key == "passed" for w in store.work_states(demo_job).values())
    assert store.get(demo_job)["snapshot"] == {"review_passed": True}
    assert store.get(demo_job)["artifact"] == "before.docx"
    assert store.events(demo_job) == before_events


def test_batch_checks_read_only_baseline_and_pause_resume_preserves_commit(store, demo_job):
    store.save_sections_batch(demo_job, [section("a"), section("b")])
    install_reviewed_tasks(store, demo_job)
    with pytest.raises(SectionRevisionConflict):
        store.save_sections_batch(
            demo_job,
            [section("a", 2)],
            expected_revisions={"a": 1, "b": 0},
        )
    assert store.states(demo_job)[0].revision == 1
    store.request_pause(demo_job)
    store.save_sections_batch(
        demo_job,
        [section("a", 2), section("b", 2)],
        expected_revisions={"a": 1, "b": 1, "not_written": None},
    )
    assert store.work_states(demo_job)["a"].review_key == ""
    assert store.work_states(demo_job)["b"].review_key == ""
    assert store.work_states(demo_job)["independent"].review_key == "passed"
    reopened = Store(store.root)
    reopened.resume_job(demo_job)
    assert {s.revision for s in reopened.states(demo_job)} == {2}


def test_competing_batches_commit_exactly_one_version(store, demo_job):
    store.save_sections_batch(demo_job, [section("a"), section("b")])
    barrier = Barrier(2)

    def submit(revision):
        barrier.wait(timeout=5)
        try:
            store.save_sections_batch(
                demo_job,
                [section("a", revision), section("b", revision)],
                expected_revisions={"a": 1, "b": 1},
            )
        except SectionRevisionConflict:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [2, 3]))
    assert sorted(results) == ["committed", "conflict"]
    assert len({s.revision for s in store.states(demo_job)}) == 1


def test_duplicate_targets_and_missing_baseline_reject_without_writes(store, demo_job):
    with pytest.raises(ValueError, match="Duplicate"):
        store.save_sections_batch(demo_job, [section("a"), section("a")])
    with pytest.raises(SectionRevisionConflict):
        store.save_sections_batch(demo_job, [section("a", 2)], expected_revisions={"a": 1})
    with pytest.raises(ValueError, match="expected revision"):
        store.save_sections_batch(demo_job, [section("a")], expected_revisions={})
    with pytest.raises(ValueError, match="advance"):
        store.save_sections_batch(demo_job, [section("a")], expected_revisions={"a": 1})
    assert not store.states(demo_job)
