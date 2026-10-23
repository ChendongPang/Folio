"""Durable research and review of business tasks, with section-addressed output."""

from __future__ import annotations

from folio.models import digest
from folio.orchestration.dependency_audit import audit_dependencies
from folio.orchestration.issues import IssueLedger, effective_findings
from folio.orchestration.local_checks import hard_findings
from folio.orchestration.review import parallel_reviews
from folio.orchestration.state import hierarchical_state
from folio.orchestration.web_research import WebResearchWorkflow
from folio.orchestration.work import (
    WorkPlan,
    WorkState,
    validate_work_plan,
)


def shared_content(content, proposals):
    """Preserve prose, using references only for exact copies of shared proposals."""
    shared = {"proposal_" + p.gap_id: p for p in proposals}
    result = content.model_dump(exclude_defaults=True)
    blocks = []
    for block in content.blocks:
        proposal = shared.get(block.block_id)
        expected = proposal.content if proposal else ""
        # Older drafts used a literal Chinese marker. Preserve exact-copy
        # detection for both languages without changing the proposal itself.
        markers = ("【模型建议】", "[Model suggestion]")
        expected_values = {expected}
        if expected and not expected.startswith(markers):
            expected_values = {marker + expected for marker in markers}
            expected_values.add("[Model suggestion] " + expected)
        if (
            proposal
            and block.origin == "model_proposal"
            and block.kind == "paragraph"
            and block.text in expected_values
            and not block.evidence_ids
        ):
            blocks.append(
                {
                    "block_id": block.block_id,
                    "origin": "model_proposal",
                    "proposal_ref": proposal.gap_id,
                }
            )
        else:
            blocks.append(block.model_dump(exclude_defaults=True))
    result["blocks"] = blocks
    return result


class BusinessWorkflow(WebResearchWorkflow):
    """Service facade for agents; bounded calls, persistence and audit stay shared."""

    def issue_ledger(self):
        return IssueLedger(self.store.root / "jobs" / self.job_id / "adjudications.json")

    def issue_context(self):
        return {
            "dismissed": self.issue_ledger().dismissed(),
            "work_plan": getattr(self, "work_plan", None),
            "work_states": self.store.work_states(self.job_id),
        }

    def effective_findings(self, findings, states=None):
        states = self.store.states(self.job_id) if states is None else states
        sections = {s.section_id: s for s in self.plan.sections}
        hard_keys = {
            finding.fingerprint
            for state in states
            if state.section_id in sections
            and sections[state.section_id].content_mode != "container"
            for finding in hard_findings(
                sections[state.section_id],
                state.content,
                state.evidence,
                state.extraction,
                (e.evidence_id for e in self.references.evidence(sections[state.section_id], None))
                if hasattr(self, "references")
                else (),
            )
        }
        return effective_findings(findings, states, hard_keys=hard_keys, **self.issue_context())

    def shared_state(self, states=None):
        states = self.store.states(self.job_id) if states is None else states
        return hierarchical_state(self.plan, states, **self.issue_context())

    def task_sections(self, task):
        return [s for s in self.plan.sections if s.section_id in task.section_ids]

    def owner(self, section_id):
        return next(t for t in self.work_plan.tasks if section_id in t.section_ids)

    def dependency_context(self, task):
        all_states = self.store.states(self.job_id)
        states = {s.section_id: s for s in all_states}
        sections = {s.section_id: s for s in self.plan.sections}
        work_states = self.store.work_states(self.job_id)
        records = self.shared_state(all_states)["records"]
        result = []
        for dependency in self.work_plan.tasks:
            if dependency.task_id not in task.depends_on:
                continue
            source = work_states.get(dependency.task_id, WorkState(task_id=dependency.task_id))
            result.append(
                {
                    "task_id": dependency.task_id,
                    "title": dependency.title,
                    "open_questions": self.unfilled_gaps(source),
                    "model_proposals": [p.model_dump() for p in source.proposals],
                    "findings": [
                        f.model_dump() for f in self.effective_findings(source.findings, all_states)
                    ],
                    "sections": [
                        {
                            "section_id": sid,
                            "revision": states[sid].revision,
                            "title": sections[sid].title,
                            "display_number": sections[sid].display_number,
                            "review_status": "reviewed" if source.review_key and (
                                not source.review_revisions
                                or source.review_revisions.get(sid) == states[sid].revision
                            ) else "saved",
                            # A downstream synthesis consumes the actual saved
                            # document, including tables and proposal provenance.
                            "content": states[sid].content.model_dump(),
                            "supported_facts": [
                                {
                                    k: v
                                    for k, v in record.items()
                                    if k not in {"record_id", "section_id", "section_revision"}
                                }
                                for record in records.values()
                                if record["section_id"] == sid
                            ],
                            "pending_items": states[sid].content.pending_items,
                            "findings": [
                                f.model_dump()
                                for f in self.effective_findings(
                                    states[sid].local_findings, all_states
                                )
                            ],
                        }
                        for sid in dependency.section_ids
                        if sid in states
                    ],
                }
            )
        return result

    async def prepare_work_plan(self):
        self.store.update(self.job_id, stage="work_planning")
        plan = self.store.work_plan(self.job_id)
        if plan is None:
            sections = [s for s in self.plan.sections if s.content_mode != "container"]
            index = [
                {
                    "source": s.path.name,
                    "role": "project_requirement",
                    "headings": [b.path for b in s.blocks if b.kind == "heading"][:60],
                    "characters": sum(
                        len(b.text) + sum(len(c) for r in b.rows for c in r) for b in s.blocks
                    ),
                }
                for s in self.references.sources
            ]
            if self.request.ragflow:
                index.append(
                    {
                        "role": "knowledge",
                        "dataset_ids": self.request.ragflow.dataset_ids,
                        "content_status": 'Not researched yet; source volume unknown',
                    }
                )
            elif self.request.knowledge_path:
                # File/chunk metadata only; output samples are never factual inputs.
                chunks = getattr(self.provider, "chunks", [])
                index.append(
                    {
                        "role": "knowledge",
                        "sources": sorted({e.source_id for e in chunks})[:80],
                        "characters": sum(len(e.text) for e in chunks),
                    }
                )
            context = {
                "sections": [
                    {
                        "section_id": s.section_id,
                        "number": s.display_number,
                        "heading_path": s.heading_path or [s.title],
                        "instruction": s.instruction[:600],
                        "instruction_truncated": len(s.instruction) > 600,
                    }
                    for s in sections
                ],
                "material_index": index,
                "execution_budget": {
                    "max_parallel_tasks": self.limits.max_parallel_tasks,
                    "max_sections_per_task": self.limits.max_work_sections,
                    "content_node_count": len(sections),
                    "minimum_tasks": (len(sections) + self.limits.max_work_sections - 1)
                    // self.limits.max_work_sections,
                    "evidence_chars_per_task": self.limits.evidence_chars,
                    "max_prompt_chars": self.limits.max_prompt_chars,
                },
            }
            plan = await self.call("work_plan", context, WorkPlan) if sections else WorkPlan()
            validate_work_plan(plan, self.plan)
            self.store.save_work_plan(self.job_id, plan)
        validate_work_plan(plan, self.plan)
        plan = await audit_dependencies(self, plan)
        self.work_plan = plan
        self.store.work_event(
            self.job_id,
            f"已建立业务执行计划：{len(plan.tasks)} 个任务，分别负责对应的文档内容节点。",
        )
        for task in plan.tasks:
            labels = "、".join(s.display_number + " " + s.title for s in self.task_sections(task))
            self.store.work_event(
                self.job_id, f"任务“{task.title}”负责：{labels}。{task.rationale}"
            )

    async def run_business_tasks(self):
        from folio.orchestration.agents import BusinessCoordinator

        return await BusinessCoordinator(self).run()

    async def execute_business_task(self, task):
        from folio.orchestration.agents import BusinessAgent
        from folio.orchestration.task_usage import (
            TaskUsageTracker,
            bind_task_usage,
            reset_task_usage,
        )
        from folio.orchestration.work import WorkState

        tracker = TaskUsageTracker(task.task_id)
        token = bind_task_usage(tracker)
        try:
            agent = BusinessAgent(self)
            return await agent.run(agent.assignment(task))
        finally:
            reset_task_usage(token)
            usage = tracker.snapshot()
            try:
                work = self.store.work_states(self.job_id).get(
                    task.task_id, WorkState(task_id=task.task_id)
                )
                if work.usage is not None:
                    usage = usage.model_copy(update={
                        "started_at": work.usage.started_at or usage.started_at,
                        "elapsed_ms": work.usage.elapsed_ms + usage.elapsed_ms,
                        "input_tokens": work.usage.input_tokens + usage.input_tokens,
                        "output_tokens": work.usage.output_tokens + usage.output_tokens,
                        "total_tokens": work.usage.total_tokens + usage.total_tokens,
                        "model_calls": work.usage.model_calls + usage.model_calls,
                        "cache_hits": work.usage.cache_hits + usage.cache_hits,
                        "failed_calls": work.usage.failed_calls + usage.failed_calls,
                    })
                work.usage = usage
                self.store.save_work_state(self.job_id, work)
            except Exception as exc:
                self.store.audit_event(
                    self.job_id,
                    "task_usage_save_failed",
                    {"task_id": task.task_id, "error_type": type(exc).__name__},
                )
            self.store.work_event(
                self.job_id,
                f"任务“{task.title}”统计已更新："
                f"{usage.elapsed_ms / 1000:.1f} 秒，{usage.total_tokens:,} Token。",
                task_id=task.task_id,
                task_usage=usage.model_dump(),
            )

    async def complete_gaps(self, task, work, gaps, section_ids=None):
        from folio.orchestration.agents import BusinessAgent

        return await BusinessAgent(self).complete_gaps(task, work, gaps, section_ids)

    def model_completion_allowed(self, work):
        if work.research:
            return bool(work.research.get("fallback_ready"))
        return len(work.searches) >= self.limits.max_searches and not self.web_search_available(work)

    def business_context(self, task, *, current_section=None):
        work = self.store.work_states(self.job_id)[task.task_id]
        from folio.research.workflow import coverage_context

        return {
            "work_task": task.model_dump(),
            "business_record": work.record.model_dump(),
            "model_proposals": [p.model_dump() for p in work.proposals],
            "model_completion_allowed": self.model_completion_allowed(work),
            "dependencies": self.dependency_context(task),
            "task_sections": [s.model_dump() for s in self.task_sections(task)],
            "research_coverage": coverage_context(work),
            "peer_sections": [
                {"section_id": s.section_id, "content": shared_content(s.content, work.proposals)}
                for s in self.store.states(self.job_id)
                if s.section_id in task.section_ids and s.section_id != current_section
            ],
        }

    async def research_work(self, task, instruction="", *, writing_gaps=()):
        from folio.orchestration.agents import BusinessAgent

        return await BusinessAgent(self).research_work(task, instruction, writing_gaps=writing_gaps)

    async def review_business(self, task):
        from folio.orchestration.agents import ReviewAgent

        return await ReviewAgent(self).review(task)

    @staticmethod
    def unfilled_gaps(work):
        covered = {p.gap_id for p in work.proposals}
        return [g for g in work.missing if digest(g) not in covered]

    async def business_findings(self):
        findings = []
        # Review dependencies first even if the LLM returned tasks in a different order.
        pending = list(self.work_plan.tasks)
        complete = set()
        while pending:
            ready = [t for t in pending if set(t.depends_on) <= complete]
            if not ready:
                raise ValueError("Business review dependency cycle")
            results = await parallel_reviews(
                ready, self.limits.max_parallel_reviews, self.review_business, lambda *_: None
            )
            for task, result in zip(ready, results, strict=True):
                findings.extend(result)
                complete.add(task.task_id)
                pending.remove(task)
        return findings
