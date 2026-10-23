"""Repair independent findings against one saved document snapshot."""

from folio.models import Finding
from folio.orchestration.agents import BusinessAgent, RepairAssignment
from folio.orchestration.review import parallel_reviews
from folio.orchestration.review_progress import ReviewProgress, finding_detail


def independent_repairs(candidates, work_plan):
    owners = {sid: task.task_id for task in work_plan.tasks for sid in task.section_ids}
    tasks = {task.task_id: task for task in work_plan.tasks}
    selected, written, read = [], set(), set()
    for finding, detail in candidates:
        ids = detail.affected_section_ids
        if (
            not ids
            or not set(ids).issubset(finding.section_ids)
            or not detail.repair_instruction.strip()
            or any(sid not in owners for sid in finding.section_ids)
        ):
            raise ValueError("Invalid repair target or empty repair instruction")
        writes = {owners[sid] for sid in ids}
        reads = {owners[sid] for sid in finding.section_ids} | writes
        # Include transitive dependencies conservatively. Shared read-only
        # dependencies may coexist; changing anything another repair reads may not.
        pending = list(reads)
        while pending:
            for dependency in tasks[pending.pop()].depends_on:
                if dependency not in reads:
                    reads.add(dependency)
                    pending.append(dependency)
        if writes & read or reads & written:
            continue
        selected.append((finding, detail))
        written.update(writes)
        read.update(reads)
    return selected


async def apply_repair_batch(engine, choices, states, snapshot):
    """No section is published until every worker has finished successfully.

    Independent owners may update their own research records. Saved section
    bodies stay immutable throughout the batch, including dependency reads.
    """
    previous = {state.section_id: state for state in states}

    async def revise(choice):
        finding, detail = choice
        ids = detail.affected_section_ids
        for task in engine.work_plan.tasks:
            revisions = {sid: previous[sid].revision for sid in ids if sid in task.section_ids}
            if revisions:
                BusinessAgent(engine).prepare_repair(
                    RepairAssignment(
                        task_id=task.task_id,
                        expected_revisions=revisions,
                        finding=finding,
                        instruction=detail.repair_instruction,
                    )
                )
        progress = ReviewProgress(engine, "repair", states, len(ids))
        progress.emit(
            "正在处理已确认问题："
            + finding.description[:120]
            + "；涉及"
            + progress.titles(ids)
            + "。",
            status="summary",
            step="started",
            sections=progress.sections(ids),
            findings=[finding_detail(finding)],
            reason=detail.reason,
        )
        updates, rejected = [], []
        for section in engine.plan.sections:
            if section.section_id not in ids:
                continue
            old = previous[section.section_id]
            if finding.kind == "extraction":
                revised = await engine.validate_section(
                    section,
                    old.content,
                    old.evidence,
                    old.revision + 1,
                    feedback=detail.repair_instruction,
                )
            else:
                revised = await engine.generate(section, old, finding, detail.repair_instruction)
            if revised.section_id != section.section_id or revised.revision != old.revision + 1:
                raise ValueError("Repair result does not match its assigned section and revision")
            if (
                ids == [section.section_id]
                and finding.kind == "coverage"
                and finding.key == "required-table"
                and finding in old.local_findings
                and any(
                    f.kind == "coverage" and f.key == "required-table"
                    for f in revised.local_findings
                )
            ):
                engine.archive_context(
                    "rejected_repairs",
                    {"finding": finding.model_dump(), "candidate": revised.model_dump()},
                )
                rejected.append(
                    Finding(
                        kind="pending",
                        key="repair-no-progress:" + finding.fingerprint,
                        description='The repaired table still lacks required column headings. This revision batch was not adopted; the original body and issues are preserved pending confirmation.',
                        section_ids=[section.section_id],
                    )
                )
            updates.append(revised)
        return updates, rejected, progress, finding

    results = await parallel_reviews(
        choices, engine.limits.max_parallel_reviews, revise, lambda *_: None
    )
    rejected = [finding for _, problems, _, _ in results for finding in problems]
    if rejected:
        snapshot["blocking_issues"].extend(finding.model_dump() for finding in rejected)
        snapshot["review_passed"] = False
        engine.store.update(engine.job_id, snapshot=snapshot)
        for _, _, progress, _ in results:
            progress.emit(
                "本批修订未通过结构验收，保留全部原正文和问题记录。",
                status="summary",
                step="rejected",
                outcome="needs_confirmation",
                findings=[finding_detail(finding) for finding in rejected],
            )
        return False
    updates = {state.section_id: state for revised, _, _, _ in results for state in revised}
    # The immutable read snapshot and all writes form one atomic transaction.
    # A failure cannot leave half a multi-section repair committed, and a stale
    # reader must not overwrite a more recent chapter.
    engine.store.save_sections_batch(
        engine.job_id,
        [
            updates[section.section_id]
            for section in engine.plan.sections
            if section.section_id in updates
        ],
        expected_revisions={state.section_id: state.revision for state in states},
    )
    if prefetch := getattr(engine, "_visual_prefetch", None):
        await prefetch.refresh_and_drain()
    for revised, _, progress, finding in results:
        progress.states.update({state.section_id: state for state in revised})
        progress.emit(
            "已保存本批独立修订，接下来统一复查；内容与检查条件未变化的请求复用已有结果。",
            status="summary",
            step="completed",
            sections=progress.sections([state.section_id for state in revised]),
            findings=[finding_detail(finding)],
        )
    return True
