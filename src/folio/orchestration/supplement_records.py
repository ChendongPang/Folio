"""Persist adopted supplement corrections without replacing concurrent work state."""

import asyncio

from folio.orchestration.work import GapProposal


async def persist_corrected_supplement(engine, source, original, corrected) -> bool:
    """Compare the saved proposal version under its normal business-task lock.

    The caller must first validate the proposed document patch. A False result
    means the patch must not be adopted: its original proposal is no longer the
    current version. Research progress and unrelated proposal fields are retained.
    """
    gap_id = original.get("gap_id")
    if not gap_id or corrected.get("gap_id") != gap_id:
        return False
    task_id = source.get("work_task", {}).get("task_id")

    def archive(status):
        engine.archive_context(
            "supplement_record_updates",
            {
                "task_id": task_id,
                "gap_id": gap_id,
                "status": status,
                "original": original,
                "corrected": corrected,
            },
        )

    if not task_id:
        # Standalone integrations have no shared business record to update.
        archive("standalone")
        return True
    async with engine._work_locks.setdefault(task_id, asyncio.Lock()):
        work = engine.store.work_states(engine.job_id).get(task_id)
        matches = [] if work is None else [p for p in work.proposals if p.gap_id == gap_id]
        if len(matches) != 1:
            archive("missing_or_ambiguous")
            return False
        current = matches[0]
        if current.content == corrected.get("content"):
            archive("already_current")
            return True
        if current.content != original.get("content"):
            archive("concurrent_version")
            return False
        replacement = GapProposal.model_validate(
            current.model_dump() | {"content": corrected.get("content")}
        )
        work.proposals = [replacement if p is current else p for p in work.proposals]
        # The content-dependent review must run again; old findings remain audit.
        work.review_key = ""
        archive("adopted")
        engine.store.save_work_state(engine.job_id, work)
        return True
