"""Task-scoped agents sharing durable services, with no private factual memory.

Agents own the business/review loops. The coordinator owns scheduling and release
barriers. Engine remains the provider of bounded calls, storage and node writing.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
from pydantic import Field

from folio.decisions import ActionDecision
from folio.models import Evidence, Finding, Model, Query, Review, digest
from folio.orchestration.context import dependency_evidence_ids, dependency_research_identity
from folio.orchestration.review import parallel_reviews
from folio.orchestration.review_candidates import deduplicate_review_findings
from folio.orchestration.state import relevant_facts
from folio.orchestration.work import BusinessRecord, GapCompletion, WorkState, WorkTask
from folio.runtime.errors import ModelCallFailed

if TYPE_CHECKING:
    from folio.orchestration.engine import Engine


class BusinessAssignment(Model):
    task: WorkTask
    plan_version: str
    input_version: str
    section_revisions: dict[str, int] = Field(default_factory=dict)
    dependencies: list[dict] = Field(default_factory=list)
    remaining_searches: int


class BusinessResult(Model):
    task_id: str
    section_revisions: dict[str, int]
    review_key: str
    findings: list[Finding] = Field(default_factory=list)
    research: dict = Field(default_factory=dict)


class RepairAssignment(Model):
    task_id: str
    expected_revisions: dict[str, int]
    finding: Finding
    instruction: str = Field(min_length=1)


def validate_repair_assignment(services, assignment: RepairAssignment):
    task = next(t for t in services.work_plan.tasks if t.task_id == assignment.task_id)
    targets = set(assignment.expected_revisions)
    if not targets or not targets <= set(task.section_ids):
        raise ValueError("Repair must target sections owned by its business agent")
    if not targets <= set(assignment.finding.section_ids):
        raise ValueError("Repair target is outside its finding")
    current = {s.section_id: s.revision for s in services.store.states(services.job_id)}
    if any(current.get(sid) != rev for sid, rev in assignment.expected_revisions.items()):
        raise ValueError("Repair assignment refers to stale section revisions")
    return task


class BusinessAgent:
    def __init__(self, services: Engine):
        self.services = services

    def assignment(self, task):
        host = self.services
        work = host.store.work_states(host.job_id).get(
            task.task_id, WorkState(task_id=task.task_id)
        )
        return BusinessAssignment(
            task=task.model_copy(deep=True),
            plan_version=digest(host.plan.model_dump_json()),
            input_version=digest(host.request.model_dump_json()),
            section_revisions={
                s.section_id: s.revision
                for s in host.store.states(host.job_id)
                if s.section_id in task.section_ids
            },
            dependencies=host.dependency_context(task),
            remaining_searches=max(0, host.limits.max_searches - len(work.searches)),
        )

    async def run(self, assignment: BusinessAssignment) -> BusinessResult:
        host = self.services
        task = assignment.task
        canonical = next(t for t in host.work_plan.tasks if t.task_id == task.task_id)
        if (
            task != canonical
            or assignment.plan_version != digest(host.plan.model_dump_json())
            or assignment.input_version != digest(host.request.model_dump_json())
        ):
            raise ValueError("Business assignment does not match the confirmed plan")
        current = {
            s.section_id: s.revision
            for s in host.store.states(host.job_id)
            if s.section_id in task.section_ids
        }
        if current != assignment.section_revisions:
            raise ValueError("Business assignment refers to stale section revisions")
        if host.dependency_context(task) != assignment.dependencies:
            raise ValueError("Business assignment refers to stale dependency results")
        host.archive_context("agent_assignments", assignment.model_dump())
        host.store.work_event(host.job_id, f"开始业务任务“{task.title}”：依赖已满足。")
        await host.research_work(task)
        pending = [s for s in host.task_sections(task) if s.section_id not in current]

        async def write(section):
            state = await host.generate(section)
            if state.section_id != section.section_id:
                raise ValueError("Business writer returned a section outside its assignment")
            host.store.save_section(host.job_id, state)
            if prefetch := getattr(host, "_visual_prefetch", None):
                prefetch.schedule_section(state)

        await parallel_reviews(pending, min(8, len(pending)), write, lambda *_: None)
        findings = await host.review_business(task)
        states = [s for s in host.store.states(host.job_id) if s.section_id in task.section_ids]
        work = host.store.work_states(host.job_id)[task.task_id]
        revisions = {s.section_id: s.revision for s in states}
        if set(revisions) != set(task.section_ids) or not work.review_key:
            raise ValueError("Business result is incomplete or has not been reviewed")
        if work.review_revisions and work.review_revisions != revisions:
            raise ValueError("Business result changed after its task review")
        result = BusinessResult(
            task_id=task.task_id,
            section_revisions=revisions,
            review_key=work.review_key,
            findings=findings,
            research=work.research,
        )
        host.archive_context("agent_results", result.model_dump())
        if prefetch := getattr(host, "_visual_prefetch", None):
            prefetch.schedule(task, findings)
        return result

    def prepare_repair(self, assignment: RepairAssignment):
        task = validate_repair_assignment(self.services, assignment)
        self.services.archive_context("agent_repairs", assignment.model_dump())
        return task

    async def complete_gaps(self, task, work, gaps, section_ids=None):
        work.missing = list(dict.fromkeys(work.missing + gaps))
        existing = {p.gap_id: p for p in work.proposals}
        requested = []
        for gap in dict.fromkeys(gaps):
            gap_id = digest(gap)
            if gap_id not in existing:
                requested.append({"gap_id": gap_id, "question": gap})
            elif section_ids:
                # Reuse the same business suggestion across nodes and on checkpoint recovery.
                proposal = existing[gap_id]
                proposal.section_ids = list(dict.fromkeys(proposal.section_ids + section_ids))
        if not requested:
            self.services.store.save_work_state(self.services.job_id, work)
            return []
        result = await self.services.call(
            "complete_gaps",
            {
                "work_task": task.model_dump(),
                "sections": [
                    s.model_dump()
                    for s in self.services.task_sections(task)
                    if section_ids is None or s.section_id in section_ids
                ],
                "gaps": requested,
                "business_record": work.record.model_dump(),
                "evidence": [e.model_dump() for e in work.evidence],
                "existing_proposals": [p.model_dump() for p in work.proposals],
                "dependencies": self.services.dependency_context(task),
                "global_facts": list(
                    {
                        f["record_id"]: f for facts in work.global_facts.values() for f in facts
                    }.values()
                ),
            },
            GapCompletion,
        )
        # Persist each task separately; never replace another worker's records.
        work.proposals.extend(result.proposals)
        self.services.store.save_work_state(self.services.job_id, work)
        self.services.store.work_event(
            self.services.job_id,
            f"“{task.title}”已生成 {len(result.proposals)} 项模型建议，补全材料缺口。",
        )
        return result.proposals

    async def research_work(self, task, instruction="", *, writing_gaps=()):
        dependencies = self.services.dependency_context(task)
        context_key = digest(
            json.dumps([task.model_dump(), dependency_research_identity(dependencies), instruction],
                       sort_keys=True)
        )
        work = self.services.store.work_states(self.services.job_id).get(
            task.task_id, WorkState(task_id=task.task_id)
        )
        if work.context_key == context_key and work.researched and not writing_gaps:
            research = getattr(getattr(self.services, "request", None), "research", None)
            if not research or not research.enabled:
                return work
            from folio.confirmations import rounds

            confirmations = rounds(self.services.store, self.services.job_id)
            grant_id = confirmations[-1]["request_id"] if confirmations else ""
            quota_task_id = task.task_id + (":" + grant_id if grant_id else "")
            if work.research.get("quota_task_id") == quota_task_id:
                return work
        if work.context_key != context_key:
            work.context_key = context_key
            work.researched = False
            work.review_key = ""
            # A repair instruction changes what we need to understand, not the
            # task's retrieval allowance. Keep its three-search history and
            # completed proposals; re-evaluate the record and current gaps below.
            states = self.services.store.states(self.services.job_id)
            work.global_facts = {
                s.section_id: relevant_facts(
                    s,
                    states,
                    exclude_sections=task.section_ids,
                    plan=self.services.plan,
                    **self.services.issue_context(),
                )
                for s in self.services.task_sections(task)
            }
        work.dependency_key = digest(json.dumps(dependencies, sort_keys=True))
        work.research_instruction = instruction
        sections = self.services.task_sections(task)
        evidence = {e.evidence_id: e for e in work.evidence}
        for section in sections:
            for e in self.services.references.evidence(section, None):
                evidence[e.evidence_id] = e
        # Resume old jobs and let dependent tasks use the original sources of supported facts.
        dependency_evidence = {
            eid
            for d in dependencies
            for s in d["sections"]
            for eid in dependency_evidence_ids(s)
        }
        shared_evidence = {
            eid
            for facts in work.global_facts.values()
            for fact in facts
            for eid in fact["evidence_ids"]
        }
        for state in self.services.store.states(self.services.job_id):
            for e in state.evidence:
                if (
                    state.section_id in task.section_ids
                    or e.evidence_id in dependency_evidence | shared_evidence
                ):
                    evidence.setdefault(e.evidence_id, e)
        work.evidence = list(evidence.values())
        if self.services.request.research and self.services.request.research.enabled:
            from folio.research.workflow import research_business

            return await research_business(
                self.services, task, work, writing_gaps=writing_gaps
            )
        while True:
            context = {
                "work_task": task.model_dump(),
                "sections": [s.model_dump() for s in sections],
                "evidence": [e.model_dump() for e in work.evidence],
                "dependencies": dependencies,
                "repair_instruction": instruction,
            }
            if writing_gaps:
                context["writing_gaps"] = list(writing_gaps)
            record_key = digest(json.dumps(context, sort_keys=True))
            if work.evidence and work.record_key != record_key:
                work.record = await self.services.call(
                    "understand_work",
                    context
                    | {
                        "previous_record": work.record.model_dump(),
                    },
                    BusinessRecord,
                )
                work.record_key = record_key
                self.services.store.work_event(
                    self.services.job_id,
                    f"“{task.title}”已整理 {len(work.record.facts)} 条有来源的业务记录，"
                    f"发现 {len(work.record.open_questions)} 个缺口、{len(work.record.conflicts)} 项材料冲突。",
                )
            self.services.store.save_work_state(self.services.job_id, work)
            actions = ["search"] if len(work.searches) < self.services.limits.max_searches else []
            if work.evidence:
                actions.append("write")
            if work.searches or work.evidence:
                actions.append("pending")
            context.update(
                {
                    "business_record": work.record.model_dump(),
                    "search_count": len(work.searches),
                    "previous_queries": work.searches,
                    "web_search_count": len(work.web_searches),
                    "web_search_queries": [a.query for a in work.web_searches if a.query],
                }
            )
            decision = await self.services.call(
                "next_action", context | {"available_actions": actions}, ActionDecision
            )
            work.missing = list(
                dict.fromkeys(
                    decision.missing_information
                    + work.record.open_questions
                    + work.record.conflicts
                    + (
                        list(writing_gaps)
                        if not any(a.evidence_ids for a in work.web_searches)
                        else []
                    )
                )
            )
            if (not work.evidence or decision.action == "pending") and not work.missing:
                work.missing = ['The available sources do not sufficiently support this business task']
            if len(work.searches) >= self.services.limits.max_searches:
                if work.missing and self.services.web_search_available(work):
                    await self.services.supplement_web(task, work)
                    # New evidence is read and assessed before any model completion.
                    continue
                if work.missing:
                    await self.services.complete_gaps(task, work, work.missing)
                work.writable = True
                work.researched = True
                self.services.store.save_work_state(self.services.job_id, work)
                return work
            if decision.action == "write" and not work.missing:
                work.writable = True
                work.researched = True
                self.services.store.save_work_state(self.services.job_id, work)
                return work
            self.services.store.work_event(
                self.services.job_id,
                f"正在检索业务任务“{task.title}”（第 {len(work.searches) + 1} 次），补充共同业务依据。",
            )
            query = await self.services.call(
                "query",
                context
                | {
                    "missing_information": work.missing,
                },
                Query,
            )
            key = digest(task.task_id + ":" + query.model_dump_json())
            cached = self.services.store.retrieval(self.services.job_id, key)
            if cached is None:
                from folio.orchestration.retrieval_audit import search_with_audit

                found = await search_with_audit(
                    self.services.store,
                    self.services.job_id,
                    self.services.provider,
                    query.queries,
                    task_id=task.task_id,
                    source_kind="knowledge_base",
                    limit=self.services.limits.retrieval_limit,
                    max_chars=self.services.limits.evidence_chars,
                )
                cached = json.dumps([e.model_dump() for e in found], ensure_ascii=False)
                self.services.store.save_retrieval(self.services.job_id, key, cached)
            else:
                self.services.store.audit_event(
                    self.services.job_id,
                    "retrieval_cache_hit",
                    {
                        "work_task_id": task.task_id,
                        "queries": query.queries,
                        "source_kind": "knowledge_base",
                        "retrieval_key": key,
                        "evidence_ids": [item["evidence_id"] for item in json.loads(cached)],
                    },
                )
            ids = {e.evidence_id for e in work.evidence}
            for item in json.loads(cached):
                e = Evidence.model_validate(item)
                if e.evidence_id not in ids:
                    work.evidence.append(e)
                    ids.add(e.evidence_id)
            work.searches.append(query.queries)
            self.services.store.save_work_state(self.services.job_id, work)


class ReviewAgent:
    def __init__(self, services: Engine):
        self.services = services

    async def review(self, task):
        from folio.orchestration.business import shared_content

        work = self.services.store.work_states(self.services.job_id).get(task.task_id)
        saved_ids = {state.section_id for state in self.services.store.states(self.services.job_id)}
        saved_body_ready = bool(
            work and work.writable and work.evidence and set(task.section_ids) <= saved_ids
        )
        if work is None or (not work.researched and not saved_body_ready):
            work = await self.services.research_work(
                task, work.research_instruction if work else ""
            )
        # Review diagnoses the saved document against CURRENT upstream information.
        # A changed diagnosis/revision is not an instruction to restart retrieval,
        # reset the three-search budget, or replace the task's existing proposals.
        # Any required research belongs to the subsequent, explicit repair action.
        dependencies = self.services.dependency_context(task)
        evidence = {e.evidence_id: e for e in work.evidence}
        dependency_sources = {
            eid
            for dependency in dependencies
            for section in dependency["sections"]
            for eid in dependency_evidence_ids(section)
        }
        for state in self.services.store.states(self.services.job_id):
            for source in state.evidence:
                if source.evidence_id in dependency_sources:
                    evidence.setdefault(source.evidence_id, source)
        states = [
            s
            for s in self.services.store.states(self.services.job_id)
            if s.section_id in task.section_ids
        ]
        if len(states) != len(task.section_ids):
            return []
        from folio.research.artifact_issues import reconcile_artifact_issues
        from folio.research.workflow import coverage_context, local, research_findings

        await reconcile_artifact_issues(self.services, task, work, states)
        research_checks = await local(research_findings, self.services, task, work, states)
        key = digest(
            json.dumps(
                [
                    [s.model_dump() for s in states],
                    work.record.model_dump(),
                    [e.model_dump() for e in evidence.values()],
                    work.missing,
                    [p.model_dump() for p in work.proposals],
                    dependencies,
                    work.research,
                    [finding.model_dump() for finding in research_checks],
                ],
                sort_keys=True,
            )
        )
        if work.review_key == key:
            return self.services.effective_findings(work.findings)
        common = {
            "review_scope": "task_consistency",
            "work_task": task.model_dump(),
            "business_record": work.record.model_dump(),
            "known_gaps": self.services.unfilled_gaps(work),
            "model_proposals": [p.model_dump() for p in work.proposals],
            "evidence": [e.model_dump() for e in evidence.values()],
            "definitions": [s.model_dump() for s in self.services.task_sections(task)],
            "dependencies": dependencies,
            "research_coverage": await local(coverage_context, work),
            "all_section_summaries": [
                {"section_id": s.section_id, "summary": s.extraction.summary} for s in states
            ],
        }
        content = [
            {"section_id": s.section_id, "content": shared_content(s.content, work.proposals)}
            for s in states
        ]
        # RequestWorkflow owns bounded reads scoped to concrete review questions.
        batches = [content]
        # Preserve the research gaps in audit; only actually completed proposals cover them.
        # A reviewer cannot waive a gap with no stored completion.
        findings = [
            Finding(
                kind="pending",
                key="work-gap:" + task.task_id + ":" + digest(gap),
                description=gap[:3500],
                section_ids=task.section_ids,
            )
            for gap in self.services.unfilled_gaps(work)
        ] + research_checks
        for batch in batches:
            review = await self.services.call("review_work", common | {"sections": batch}, Review)
            findings.extend(review.findings)
        candidates = deduplicate_review_findings(findings)
        work.findings = candidates.findings
        if candidates.audit:
            self.services.archive_context("review_deduplication", candidates.audit)
        work.review_key = key
        work.review_revisions = {s.section_id: s.revision for s in states}
        self.services.store.save_work_state(self.services.job_id, work)
        self.services.store.work_event(
            self.services.job_id,
            f"业务任务“{task.title}”整体检查完成：{len(work.findings)} 项需处理的问题。",
        )
        return self.services.effective_findings(work.findings)


class BusinessCoordinator:
    """Release dependencies only after persisted body, task review and receipt."""

    def __init__(self, services: Engine):
        self.services = services

    @staticmethod
    def isolated_failure(error):
        """A failed model result must not discard unrelated, already-started work."""
        if isinstance(error, ModelCallFailed):
            return error.code not in {
                "authentication", "billing", "configuration", "schema_unsupported",
            }
        if isinstance(error, (TimeoutError, httpx.TransportError)):
            return True
        # Only validation errors attributed to an actual model request qualify.
        # Assignment, storage and coordinator invariants must still fail fast.
        context = getattr(error, "folio_failure_context", None)
        return (
            isinstance(error, ValueError)
            and isinstance(context, dict)
            and bool(context.get("task_key"))
            and context.get("code") == "validation_error"
        )

    async def run(self):
        host = self.services
        await host.prepare_work_plan()
        host.store.update(host.job_id, stage="generation")
        existing = {s.section_id for s in host.store.states(host.job_id)}
        for section in host.plan.sections:
            if section.content_mode == "container" and section.section_id not in existing:
                host.store.save_section(host.job_id, await host.generate(section))
        completed, running = set(), {}
        pending = list(host.work_plan.tasks)
        first_error, first_context = None, None
        task_titles = {task.task_id: task.title for task in pending}
        try:
            while pending or running:
                ready = [] if first_error is not None else [
                    t for t in pending if set(t.depends_on) <= completed
                ]
                for task in ready[: host.limits.max_parallel_tasks - len(running)]:
                    pending.remove(task)
                    worker = asyncio.create_task(
                        host.execute_business_task(task), name=task.task_id
                    )
                    running[worker] = task.task_id
                if not running:
                    if first_error is not None:
                        break
                    raise ValueError("No runnable business task; invalid dependencies")
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    task_id = running.pop(worker)
                    try:
                        result = worker.result()
                    except Exception as exc:
                        if not self.isolated_failure(exc):
                            raise
                        context = dict(getattr(exc, "folio_failure_context", None) or {})
                        if first_error is None:
                            first_error, first_context = exc, context
                        host.store.audit_event(host.job_id, "business_task_failed", {
                            "task_id": task_id,
                            "error_type": type(exc).__name__,
                            "code": getattr(exc, "code", context.get("code", "model_error")),
                            "failure_context": context,
                        })
                        host.store.work_event(
                            host.job_id,
                            f"业务任务“{task_titles[task_id]}”暂时失败；"
                            "其余已启动任务继续完成并保存，暂不启动新任务。",
                        )
                        continue
                    if result.task_id != task_id or not result.review_key:
                        raise ValueError("Business agent did not deliver a reviewed result")
                    completed.add(task_id)
            if first_error is not None:
                # Preserve the original request's diagnostic identity even if
                # other workers failed while their independent work drained.
                if first_context:
                    first_error.folio_failure_context = first_context
                host.store.work_event(
                    host.job_id,
                    "已启动业务任务均已结束，成功结果已保存；失败步骤保留，等待继续处理。",
                )
                raise first_error
        finally:
            for worker in running:
                worker.cancel()
            await asyncio.gather(*running, return_exceptions=True)
