"""Adopted corrections update only the current saved version of a proposal."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from folio.models import Finding
from folio.orchestration.supplement_records import persist_corrected_supplement
from folio.orchestration.work import GapProposal, WorkState


def proposal(gap="gap", content="补写方案规定交易应经过主管审批，并保留完整业务处理记录。"):
    return GapProposal(gap_id=gap, section_ids=["s1"], content=content)


def setup_engine(store, job_id):
    work = WorkState(
        task_id="task",
        context_key="context-version",
        dependency_key="dependency-version",
        research_instruction="原研究要求",
        record_key="record-version",
        searches=[["first"], ["second"], ["third"]],
        web_searches=[{"query": "web", "status": "completed"}],
        missing=["原始证据缺口"],
        proposals=[proposal(), proposal("sibling")],
        researched=True,
        writable=True,
        review_key="old-review",
        findings=[Finding(kind="pending", key="gap", description="历史疑点", section_ids=["s1"])],
    )
    store.save_work_state(job_id, work)
    engine = SimpleNamespace(
        store=store, job_id=job_id, _work_locks={}, archive_context=Mock(return_value="audit")
    )
    return engine, work


SOURCE = {"work_task": {"task_id": "task"}}
NEW = "修正后的补写要求交易完成主管审批后方可交割，并保留完整审计记录。"


async def test_update_reads_latest_metadata_and_preserves_research_progress(store, demo_job):
    engine, initial = setup_engine(store, demo_job)
    original = initial.proposals[0].model_dump()
    latest = store.work_states(demo_job)["task"]
    latest.proposals[0].section_ids = ["s1", "s2"]
    latest.proposals[0].disposition = "record_only"
    latest.proposals.append(proposal("concurrent-sibling"))
    latest.missing.append("并发发现的缺口")
    store.save_work_state(demo_job, latest)
    before = latest.model_dump()
    assert await persist_corrected_supplement(engine, SOURCE, original, original | {"content": NEW})
    after = store.work_states(demo_job)["task"].model_dump()
    expected = deepcopy(before)
    expected["proposals"][0]["content"] = NEW
    expected["review_key"] = ""
    assert after == expected
    assert engine.archive_context.call_args.args[1]["status"] == "adopted"


async def test_replay_accepts_current_correction_without_invalidating_new_review(store, demo_job):
    engine, initial = setup_engine(store, demo_job)
    original = initial.proposals[0].model_dump()
    corrected = original | {"content": NEW}
    assert await persist_corrected_supplement(engine, SOURCE, original, corrected)
    reviewed = store.work_states(demo_job)["task"]
    reviewed.review_key = "review-of-corrected-content"
    store.save_work_state(demo_job, reviewed)
    engine.store.save_work_state = Mock(wraps=engine.store.save_work_state)
    assert await persist_corrected_supplement(engine, SOURCE, original, corrected)
    assert store.work_states(demo_job)["task"] == reviewed
    engine.store.save_work_state.assert_not_called()
    assert engine.archive_context.call_args.args[1]["status"] == "already_current"


async def test_conflicting_concurrent_version_is_not_overwritten(store, demo_job):
    engine, initial = setup_engine(store, demo_job)
    original = initial.proposals[0].model_dump()
    latest = store.work_states(demo_job)["task"]
    latest.proposals[0].content = "另一个已采用版本规定交易必须由指定审批人完成审核后交割。"
    store.save_work_state(demo_job, latest)
    assert not await persist_corrected_supplement(
        engine, SOURCE, original, original | {"content": NEW}
    )
    assert store.work_states(demo_job)["task"] == latest
    assert engine.archive_context.call_args.args[1]["status"] == "concurrent_version"


async def test_sibling_corrections_wait_on_existing_lock_then_merge(store, demo_job):
    engine, initial = setup_engine(store, demo_job)
    lock = engine._work_locks["task"] = asyncio.Lock()
    await lock.acquire()
    originals = [p.model_dump() for p in initial.proposals]
    tasks = [
        asyncio.create_task(
            persist_corrected_supplement(engine, SOURCE, p, p | {"content": NEW + str(i)})
        )
        for i, p in enumerate(originals)
    ]
    try:
        await asyncio.sleep(0)
        assert not any(t.done() for t in tasks)
        latest = store.work_states(demo_job)["task"]
        latest.proposals.append(proposal("third-sibling"))
        store.save_work_state(demo_job, latest)
    finally:
        lock.release()
    assert await asyncio.gather(*tasks) == [True, True]
    after = store.work_states(demo_job)["task"]
    assert [p.content for p in after.proposals[:2]] == [NEW + "0", NEW + "1"]
    assert after.proposals[2] == proposal("third-sibling")
    assert after.searches == initial.searches and after.missing == initial.missing


@pytest.mark.parametrize("case", ["missing_task", "missing_proposal", "duplicate_proposal"])
async def test_missing_or_ambiguous_saved_target_rejected(store, demo_job, case):
    engine, initial = setup_engine(store, demo_job)
    original = initial.proposals[0].model_dump()
    source = SOURCE
    if case == "missing_task":
        source = {"work_task": {"task_id": "absent"}}
    else:
        latest = store.work_states(demo_job)["task"]
        latest.proposals = (
            latest.proposals[1:] if case == "missing_proposal" else [*latest.proposals, proposal()]
        )
        store.save_work_state(demo_job, latest)
    before = store.work_states(demo_job)
    assert not await persist_corrected_supplement(
        engine, source, original, original | {"content": NEW}
    )
    assert store.work_states(demo_job) == before


async def test_standalone_only_archives_and_never_reads_business_store():
    engine = SimpleNamespace(archive_context=Mock(), store=Mock())
    original = proposal().model_dump()
    assert await persist_corrected_supplement(engine, {}, original, original | {"content": NEW})
    engine.store.work_states.assert_not_called()
    engine.store.save_work_state.assert_not_called()
    assert engine.archive_context.call_args.args[1]["status"] == "standalone"


async def test_invalid_correction_cannot_save_or_change_target_identity(store, demo_job):
    engine, initial = setup_engine(store, demo_job)
    original = initial.proposals[0].model_dump()
    assert not await persist_corrected_supplement(
        engine, SOURCE, original, original | {"gap_id": "other", "content": NEW}
    )
    with pytest.raises(ValueError):
        await persist_corrected_supplement(engine, SOURCE, original, original | {"content": "短"})
    assert store.work_states(demo_job)["task"] == initial
