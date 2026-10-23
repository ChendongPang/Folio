"""One optional, cached audit of blocking edges before business workers start."""

import asyncio
import json
import logging
from typing import Literal

from pydantic import Field

from folio.models import Model, digest
from folio.runtime.base import parse_json, prompt_chars

POLICY_VERSION = 1
AUDIT_TIMEOUT_SECONDS = 20
MAX_AUDIT_EDGES = 16
TASK = "audit_work_dependencies"
logger = logging.getLogger(__name__)


class EdgeVerdict(Model):
    edge_id: str
    verdict: Literal["keep", "remove", "uncertain"]
    reason: str = Field(min_length=1, max_length=120)


class DependencyAudit(Model):
    edges: list[EdgeVerdict] = Field(min_length=1, max_length=MAX_AUDIT_EDGES)


def longest_path(plan):
    depths, pending = {}, list(plan.tasks)
    while pending:
        ready = [task for task in pending if set(task.depends_on) <= depths.keys()]
        if not ready:
            return 0  # The normal WorkPlan validator owns invalid-graph failures.
        for task in ready:
            depths[task.task_id] = 1 + max((depths[d] for d in task.depends_on), default=0)
            pending.remove(task)
    return max(depths.values(), default=0)


async def audit_dependencies(engine, plan):
    """The audit is advisory: unavailable/invalid results retain the original graph."""
    if len(plan.tasks) < 4 or engine.limits.max_parallel_tasks < 2 or longest_path(plan) < 3:
        return plan
    source_hash = digest(plan.model_dump_json())
    if engine.store.completed_dependency_audit(engine.job_id, source_hash, POLICY_VERSION):
        return plan
    protected = set(engine.store.work_states(engine.job_id))
    written = {state.section_id for state in engine.store.states(engine.job_id)}
    protected.update(task.task_id for task in plan.tasks if written.intersection(task.section_ids))
    edges = [
        (task.task_id, dependency)
        for task in plan.tasks
        if task.task_id not in protected
        for dependency in task.depends_on
    ]
    if not edges or len(edges) > MAX_AUDIT_EDGES:
        return plan
    key = (
        TASK
        + ":"
        + digest(
            json.dumps(
                [POLICY_VERSION, source_hash, getattr(engine.runtime, "identity", {})],
                sort_keys=True,
            )
        )
    )
    # Even an interrupted or failed optional audit must not consume time on each resume.
    if engine.store.task(engine.job_id, key):
        return plan
    candidates = {f"e{i}": edge for i, edge in enumerate(edges, 1)}
    context = {
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "objective": task.objective,
                "questions": task.questions,
                "rationale": task.rationale,
                "section_titles": [s.title for s in engine.task_sections(task)],
                "depends_on": task.depends_on,
                "started": task.task_id in protected,
            }
            for task in plan.tasks
        ],
        "candidate_edges": [
            {"edge_id": edge_id, "dependent": edge[0], "upstream": edge[1]}
            for edge_id, edge in candidates.items()
        ],
        "sources_available_independently": ['Uploaded sources', 'Configured supplementary sources, if any'],
    }
    schema = DependencyAudit.model_json_schema()
    schema["$defs"]["EdgeVerdict"]["properties"]["edge_id"]["enum"] = list(candidates)
    schema["properties"]["edges"].update(minItems=len(edges), maxItems=len(edges))
    try:
        measure = getattr(engine.runtime, "measure", None)
        if measure:
            fits = (await measure(TASK, context, schema, engine.limits.max_prompt_chars))["fits"]
        else:
            fits = prompt_chars(engine.runtime, TASK, context, schema) <= engine.limits.max_prompt_chars
    except Exception:
        return plan
    if not fits:
        return plan
    try:
        engine.store.begin_task(engine.job_id, key, engine.limits.max_calls)
    except RuntimeError:
        return plan  # The normal generation path retains ownership of its call budget.
    engine.store.work_event(engine.job_id, "正在检查任务执行依赖；最多等待20秒，已有生成进度保留。")
    try:
        engine.archive_context("requests", {"task": TASK, "task_key": key, "context": context})
        async with asyncio.timeout(AUDIT_TIMEOUT_SECONDS), engine._model_slots:
            result = await engine.runtime.complete(TASK, context, schema, f"{engine.job_id}:{key}")
        if result.stop_reason != "completed":
            raise ValueError("Incomplete dependency audit")
        audit = DependencyAudit.model_validate(parse_json(result.content))
        ids = [verdict.edge_id for verdict in audit.edges]
        if (
            len(ids) != len(set(ids))
            or set(ids) != candidates.keys()
            or any(not verdict.reason.strip() for verdict in audit.edges)
        ):
            raise ValueError("Dependency audit must cover only the listed edges exactly once")
        removals = {candidates[v.edge_id] for v in audit.edges if v.verdict == "remove"}
        updated, applied = engine.store.apply_dependency_audit(
            engine.job_id,
            key,
            source_hash,
            removals,
            {
                "policy": POLICY_VERSION,
                "verdicts": audit.model_dump(),
                "candidates": context["candidate_edges"],
            },
            result.usage,
        )
    except asyncio.CancelledError:
        # Preserve actual user/job cancellation; only the optional audit's own deadline falls back.
        engine.store.finish_task(engine.job_id, key, error="dependency_audit_cancelled")
        raise
    except Exception as exc:
        code = "timeout" if isinstance(exc, TimeoutError) else "invalid_or_unavailable"
        engine.store.finish_task(engine.job_id, key, error=f"dependency_audit_{code}")
        logger.warning(
            "dependency_audit_skipped job_id=%s error_type=%s code=%s",
            engine.job_id,
            type(exc).__name__,
            code,
        )
        engine.store.work_event(engine.job_id, "任务依赖检查未采用，保留原执行计划继续生成。")
        return plan
    # The graph and cache are committed together. A later progress-display error
    # must never revert only the in-memory graph or overwrite the committed cache.
    try:
        engine.store.work_event(
            engine.job_id,
            f"任务依赖检查完成：移除 {len(removals) if applied else 0} 条经确认的非阻塞关联，保留真实或不确定依赖。",
        )
    except Exception:
        logger.warning("dependency_audit_progress_unavailable job_id=%s", engine.job_id)
    return updated
