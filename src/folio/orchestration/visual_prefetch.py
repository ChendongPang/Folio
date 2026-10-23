"""Overlap locally checked figures with writing; publish only the final manifest."""

import asyncio

from folio.runtime.call_budget import (
    VisualPrefetchDeferred,
    call_budget_scope,
    visual_prefetch_policy,
)
from folio.visuals.agent import VisualAgent
from folio.visuals.workflow import VisualSession, build_visuals, visual_identity


class VisualPrefetch:
    def __init__(self, engine):
        self.engine = engine
        self.session = VisualSession()
        self.tasks = {}
        self.closed = False
        self.budget_deferred = False

    def _available(self):
        policy = visual_prefetch_policy(self.engine.limits.max_calls)
        return (
            not self.closed
            and not self.budget_deferred
            and policy.max_calls > 0
            and self.engine.store.get(self.engine.job_id)["calls"]
            < self.engine.limits.max_calls - policy.reserve_calls
        )

    def refresh(self, states=None, *, blocked_sections=()):
        """Cancel obsolete source identities as soon as a saved body changes."""
        states = self.engine.store.states(self.engine.job_id) if states is None else states
        included = {section.section_id for section in self.engine.plan.sections}
        blocked = set(blocked_sections)
        needed = {
            visual_identity(self.engine.plan, state, visual)
            for state in states
            if state.section_id in included
            and state.section_id not in blocked
            and not any(finding.kind != "pending" for finding in state.local_findings)
            for visual in state.content.visuals
        }
        cancelled = []
        for identity, task in self.tasks.items():
            if identity not in needed and not task.done():
                if not task.cancelling():
                    task.cancel()
                cancelled.append(task)
        return cancelled

    async def refresh_and_drain(self, states=None, *, blocked_sections=()):
        await self._cancel(self.refresh(states, blocked_sections=blocked_sections))

    def _schedule_state(self, plan, state):
        if any(finding.kind != "pending" for finding in state.local_findings):
            return
        for visual in state.content.visuals:
            identity = visual_identity(plan, state, visual)
            if identity in self.tasks:
                continue
            snapshot = state.model_copy(deep=True)
            snapshot.content.visuals = [visual.model_copy(deep=True)]
            self.tasks[identity] = asyncio.create_task(
                self._build(plan, snapshot), name="folio-visual-" + identity
            )

    def schedule_section(self, state):
        """Accept only the just-saved, locally checked revision, never a live draft.

        Group review still runs normally, and final export rechecks each figure's
        source identity. This is speculative work, not permission to publish.
        """
        states = self.engine.store.states(self.engine.job_id)
        self.refresh(states)
        persisted = next(
            (
                saved
                for saved in states
                if saved.section_id == state.section_id
            ),
            None,
        )
        # Comparing the whole state also rejects an unsaved change to findings,
        # evidence or extraction even when the revision number was left intact.
        if persisted is None or persisted != state:
            return
        if not self._available() or not state.content.visuals:
            return
        self._schedule_state(self.engine.plan.model_copy(deep=True), persisted)

    def schedule(self, task, findings):
        engine = self.engine
        blocked = {
            sid for finding in findings if finding.kind != "pending" for sid in finding.section_ids
        }
        states = engine.store.states(engine.job_id)
        self.refresh(states, blocked_sections=blocked)
        if not self._available():
            return
        work = engine.store.work_states(engine.job_id).get(task.task_id)
        if not work or not work.review_key:
            return
        plan = engine.plan.model_copy(deep=True)
        for state in states:
            if state.section_id not in task.section_ids or state.section_id in blocked:
                continue
            self._schedule_state(plan, state)

    async def _build(self, plan, state):
        engine = self.engine
        try:
            agent = VisualAgent(
                engine.store.root / "jobs" / engine.job_id,
                engine._call,
                lambda text: engine.store.work_event(engine.job_id, "配图预生成：" + text),
                engine.measure_request,
                session=self.session,
                builder=build_visuals,
            )
            with call_budget_scope(visual_prefetch_policy(engine.limits.max_calls)):
                return await agent.build(plan, [state], write_manifest=False)
        except VisualPrefetchDeferred:
            if not self.budget_deferred:
                engine.store.work_event(
                    engine.job_id,
                    "配图预生成已让出调用额度，优先完成正文和审阅；最终导出时继续核对未完成配图。",
                )
            self.budget_deferred = True
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Individual figure failures are normally returned as unresolved.
            # A setup failure is optional too; final export still checks this figure.
            engine.store.work_event(
                engine.job_id,
                f"配图预生成未完成（{type(exc).__name__}），最终导出仍将核对配图。",
            )
            return None

    @staticmethod
    async def _cancel(tasks):
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        drained = asyncio.gather(*tasks, return_exceptions=True)
        interrupted = False
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def settle(self, states, *, allow_model_calls=True):
        """Join current figures; obsolete work must finish cleanup before final rendering."""
        self.closed = True
        needed = {
            visual_identity(self.engine.plan, state, visual)
            for state in states
            for visual in state.content.visuals
        }
        await self._cancel(
            [
                task
                for identity, task in self.tasks.items()
                if not allow_model_calls or identity not in needed or task.cancelling()
            ]
        )
        if allow_model_calls:
            await asyncio.gather(
                *(
                    task
                    for identity, task in self.tasks.items()
                    if identity in needed and not task.cancelled() and not task.cancelling()
                )
            )

    async def close(self):
        self.closed = True
        await self._cancel(list(self.tasks.values()))
