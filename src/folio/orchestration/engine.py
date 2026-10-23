"""Durable generation and bounded review/repair. No meeting-update workflow."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TypeVar

from filelock import FileLock
from pydantic import BaseModel, ValidationError

from folio.decisions import DECISION_TASKS, ActionDecision, LLMDecisionProvider
from folio.document.guidance import guidance_schema
from folio.document.planner import build_plan
from folio.document.word import render
from folio.knowledge.local import EmptyKnowledge, KnowledgeProvider, LocalKnowledge
from folio.knowledge.ragflow import RagflowKnowledge
from folio.models import (
    Block,
    DocumentPlan,
    DrillDown,
    Extraction,
    Finding,
    JobInput,
    LocalReview,
    PlanProposal,
    Review,
    Section,
    SectionContent,
    SectionState,
    digest,
)
from folio.orchestration.business import BusinessWorkflow
from folio.orchestration.context import (
    ContextSelection,
    GapMaterialRead,
    dependency_evidence_ids,
)
from folio.orchestration.focused_review import review_schema, validate_review_result
from folio.orchestration.local_checks import (
    finding_version,
    hard_findings,
)
from folio.orchestration.requests import RequestWorkflow
from folio.orchestration.review import parallel_reviews, review_groups
from folio.orchestration.review_candidates import deduplicate_review_findings
from folio.orchestration.review_progress import ReviewProgress, call_summary, finding_detail
from folio.orchestration.review_trace import (
    record_cache_hit,
    record_call_failure,
    record_model_call,
    review_calls,
)
from folio.orchestration.task_usage import (
    record_cache_hit as record_task_cache_hit,
)
from folio.orchestration.task_usage import (
    record_model_call as record_task_model_call,
)
from folio.orchestration.task_usage import (
    record_model_failure as record_task_model_failure,
)
from folio.orchestration.task_usage import (
    record_model_usage as record_task_model_usage,
)
from folio.orchestration.work import (
    BusinessRecord,
    GapCompletion,
    WorkPlan,
    validate_work_plan,
)
from folio.runtime.base import Runtime, parse_json, prompt_chars, prompt_for
from folio.runtime.errors import (
    ModelCallBudgetExceeded,
    ModelCallFailed,
    PlanningContextTooLarge,
    TaskRetriesExhausted,
)
from folio.runtime.language import localize_text, output_language
from folio.store import Store

T = TypeVar("T", bound=BaseModel)


async def _run_local_export(function, *args, cancel_event=None, **kwargs):
    """Keep the job lock until a file-writing thread has actually stopped.

    Cancelling ``to_thread`` only cancels its awaiter. A render or pagination
    thread can still overwrite the same temporary file used by a resumed job.
    Finish that local operation before acknowledging cancellation; do not
    continue to formatting, publication, or another model request afterward.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if cancel_event is not None:
            cancel_event.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A second stop/shutdown request must not release ownership early.
                continue
            except Exception:
                break
        if not worker.cancelled():
            # Observe a local failure, but retain the caller's cancellation.
            worker.exception()
        raise


class Engine(RequestWorkflow, BusinessWorkflow):
    def __init__(
        self,
        store: Store,
        runtime: Runtime,
        knowledge: KnowledgeProvider | None = None,
        decisions=None,
        web_knowledge=None,
    ):
        self.store, self.runtime, self.knowledge = store, runtime, knowledge
        self.decisions = decisions or LLMDecisionProvider(runtime)
        self.web_knowledge = web_knowledge
        if self.web_knowledge is None:
            from folio.knowledge.search import search_provider

            self.web_knowledge = search_provider(getattr(runtime, "config", {}))
        self._request_locks: dict[str, asyncio.Lock] = {}
        self._work_locks: dict[str, asyncio.Lock] = {}
        self._model_slots = asyncio.Semaphore(16)
        self._generation_model_slots = asyncio.Semaphore(32)
        self._provider_model_slots = asyncio.Semaphore(32)
        self._prefetch_model_slots = asyncio.Semaphore(2)
        self._decision_model_slots = asyncio.Semaphore(16)
        self._writing_active = False

    @asynccontextmanager
    async def model_slot(self, task):
        from folio.runtime.call_budget import current_call_budget

        selector = getattr(self.runtime, "material_selector", None)
        strict_decisions = getattr(selector, "use_decision", False) is True
        # Strict selection has a separate service. During document writing, a
        # larger main-provider window overlaps independent readers and writers;
        # review/adjudication retain sixteen slots. A shared outer ceiling also
        # covers background visuals crossing the generation/review boundary.
        policy = current_call_budget.get()
        if policy is not None and policy.purpose == "visual_prefetch":
            async with self._prefetch_model_slots, self._provider_model_slots:
                yield
        elif strict_decisions and task == "select_context":
            async with self._decision_model_slots:
                yield
        else:
            gate = self._generation_model_slots if self._writing_active else self._model_slots
            async with gate, self._provider_model_slots:
                yield

    async def run(self, job_id: str, *, already_locked: bool = False) -> dict:
        # Lock spans await points but acquisition never blocks the event loop.
        lock = FileLock(self.store.root / f"{job_id}.lock")
        with nullcontext() if already_locked else lock.acquire(timeout=0):
            job = self.store.start_execution(job_id)
            if job["status"] != "running":
                return job
            self.job_id = job_id
            self.request = JobInput.model_validate(job["input"])
            if self.request.language is None:
                self.request.language = output_language(self.request.user_request)
            self.limits = self.request.limits
            from folio.confirmations import ConfirmedReferences, rounds

            self.confirmation_rounds = rounds(self.store, job_id)
            self.repair_baselines = (
                self.confirmation_rounds[-1]["data"]["section_revisions"]
                if self.confirmation_rounds else {}
            )
            from folio.orchestration.visual_prefetch import VisualPrefetch

            self._visual_prefetch = VisualPrefetch(self)
            try:
                # The job deadline covers source preparation as well as model
                # work. Cache/Office reads run outside the event loop. On stop,
                # a cooperative signal aborts between files and the job lock is
                # retained until any in-flight cache write has finished.
                async with asyncio.timeout(self.limits.run_timeout_seconds):
                    cancelled = Event()

                    def prepare_inputs():
                        from folio.research.source_index import _check_cancelled

                        provider = self.knowledge or (
                            RagflowKnowledge(self.request.ragflow)
                            if self.request.ragflow
                            else LocalKnowledge(
                                Path(self.request.knowledge_path),
                                cache_root=self.store.root / "source-cache",
                                cancelled=cancelled,
                            )
                            if self.request.knowledge_path
                            else EmptyKnowledge()
                        )
                        _check_cancelled(cancelled)
                        references = ConfirmedReferences(
                            self.request.reference_paths, self.request.reference_revision_view,
                            self.confirmation_rounds,
                        )
                        _check_cancelled(cancelled)
                        return provider, references

                    self.provider, self.references = await _run_local_export(
                        prepare_inputs, cancel_event=cancelled
                    )
                    from folio.orchestration.style_processing import prepare_job_style

                    self.writing_profile = await prepare_job_style(self)
                    await self._execute(job)
            except (
                ModelCallBudgetExceeded,
                ModelCallFailed,
                TaskRetriesExhausted,
                TimeoutError,
            ) as exc:
                if failure := getattr(exc, "folio_failure_context", None):
                    self.store.audit_event(job_id, "job_failure", failure)
                try:
                    delivered = await self.deliver_interrupted(exc)
                except Exception as export_error:
                    self.store.update(
                        job_id,
                        status="failed",
                        error=f"{type(export_error).__name__}: {export_error}",
                    )
                    raise
                if not delivered:
                    self.store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
                    raise
            except Exception as exc:
                if failure := getattr(exc, "folio_failure_context", None):
                    self.store.audit_event(job_id, "job_failure", failure)
                self.store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                await self._visual_prefetch.close()
            return self.store.get(job_id)

    async def deliver_interrupted(self, error) -> bool:
        """Deliver a complete checkpoint when a provider/check cannot finish.

        Cancellation, storage errors, and programming errors do not enter this
        path. No new model calls or optional layout work are permitted here.
        """
        states = self.store.states(self.job_id)
        if (
            not hasattr(self, "plan")
            or not hasattr(self, "work_plan")
            or {s.section_id for s in states} != {s.section_id for s in self.plan.sections}
        ):
            return False
        await self._visual_prefetch.close()
        code = getattr(
            error, "code", "timeout" if isinstance(error, TimeoutError) else "retries_exhausted"
        )
        if isinstance(error, ModelCallBudgetExceeded):
            code = "call-budget"
        snapshot = self.store.get(self.job_id)["snapshot"] or {}
        preserved = [Finding.model_validate(f) for f in snapshot.get("blocking_issues", [])]
        current_digest = digest("".join(state.model_dump_json() for state in states))
        confirmation_history = getattr(self, "confirmation_rounds", [])
        if confirmation_history and snapshot.get("state_digest") != current_digest:
            # A submitted answer or a saved repair is not a completed review.
            # Research/section commits invalidate the snapshot, so retain the
            # latest round's original questions if its fresh review is interrupted.
            # Once a full review covers the current states, its result takes
            # precedence; resurrecting older questions would undo that review.
            previous_snapshot = confirmation_history[-1]["data"]["previous_snapshot"]
            preserved.extend(
                Finding.model_validate(f)
                for f in previous_snapshot.get("blocking_issues", [])
            )
        preserved.extend(f for state in states for f in state.local_findings)
        preserved.extend(
            f for work in self.store.work_states(self.job_id).values() for f in work.findings
        )
        definitions = {s.section_id: s for s in self.plan.sections}
        deterministic = [
            f
            for state in states
            if definitions[state.section_id].content_mode != "container"
            for f in hard_findings(
                definitions[state.section_id],
                state.content,
                state.evidence,
                state.extraction,
                (
                    e.evidence_id
                    for e in self.references.evidence(definitions[state.section_id], None)
                ),
            )
        ]
        from folio.orchestration.visual_obligations import visual_obligation_findings

        deterministic.extend(
            visual_obligation_findings(
                [s.model_dump() for s in self.plan.sections],
                [s.model_dump() for s in states],
                user_request=self.request.user_request,
            )
        )
        hard_keys = {f.fingerprint for f in deterministic}
        dismissed = self._dismissed()
        preserved = [
            f
            for f in [*preserved, *deterministic]
            if f.fingerprint in hard_keys or self._finding_version(f, states) not in dismissed
        ]
        pending = Finding(
            kind="pending",
            key="review-incomplete:" + code,
            description=(
                f"模型调用次数已达到 {error.used}/{error.limit}，部分检查尚未完成；"
                "已保留正文和原有疑点，导出待确认草稿。"
            )
            if isinstance(error, ModelCallBudgetExceeded)
            else "检查因服务或执行中断尚未完成，已保留完整正文及原有疑点，导出待确认草稿。",
            section_ids=[s.section_id for s in states],
        )
        snapshot.update(
            review_passed=False,
            review_interruption={
                "code": code,
                "error_type": type(error).__name__,
                "stage": self.store.get(self.job_id)["stage"],
            },
            blocking_issues=[
                f.model_dump() for f in deduplicate_review_findings([*preserved, pending]).findings
            ],
        )
        if isinstance(error, ModelCallBudgetExceeded):
            snapshot["call_budget_exhausted"] = {"used": error.used, "limit": error.limit}
        self.store.update(self.job_id, snapshot=snapshot, error=None)
        self.store.audit_event(self.job_id, "delivery_degraded", snapshot["review_interruption"])
        self.store.work_event(self.job_id, pending.description)
        await self.finish(states, draft=True, needs_confirmation=True, allow_model_calls=False)
        return True

    async def _call(self, task: str, context: dict, model: type[T]) -> T:
        # Different parent groups can split into the exact same child request.
        # Serialize only identical requests so the follower reuses the durable result.
        context = self.request_context(task, context)
        identity = digest(
            json.dumps([self.job_id, task, context, model.model_json_schema()], sort_keys=True)
        )
        lock = self._request_locks.setdefault(identity, asyncio.Lock())
        async with lock:
            from folio.runtime.interaction_audit import interaction_scope

            with interaction_scope(self.store, self.job_id, task, "preflight:" + identity, 0):
                return await self._call_unlocked(task, context, model)

    def request_schema(self, task: str, context: dict, model: type[T]) -> dict:
        """Build the exact per-call contract for both sizing and transmission."""
        schema = model.model_json_schema()
        from folio.runtime.business_transport import evidence_schema, supplement_edit_schema

        schema = evidence_schema(schema, context)
        schema = supplement_edit_schema(schema, context)
        schema = review_schema(model, schema, context)
        schema = guidance_schema(schema, context)
        if model is GapMaterialRead:
            schema["properties"]["constraints"]["maxItems"] = 24
            schema["$defs"]["GapConstraint"]["properties"]["source_refs"]["items"]["enum"] = [
                item["context_ref"] for item in context["catalog"]
            ]
        if model is GapCompletion:
            gaps = [g["gap_id"] for g in context["gaps"]]
            sections = [s["section_id"] for s in context["sections"]]
            fields = schema["$defs"]["GapProposal"]["properties"]
            fields["gap_id"]["enum"] = gaps
            fields["section_ids"]["items"]["enum"] = sections
            fields["section_ids"]["maxItems"] = len(sections)
            schema["properties"]["proposals"].update(minItems=len(gaps), maxItems=len(gaps))
        if model is ContextSelection:
            for field, kinds in (
                ("selected_refs", {"fact", "proposal", "evidence"}),
                ("risk_refs", {"risk"}),
            ):
                refs = [x["context_ref"] for x in context["catalog"] if x["kind"] in kinds]
                schema["properties"][field]["maxItems"] = len(refs)
                if refs:
                    schema["properties"][field]["items"]["enum"] = refs
            if not any(x["kind"] == "risk" for x in context["catalog"]):
                schema["properties"]["risk_summary"]["const"] = ""
        if model in {BusinessRecord, Extraction}:
            # Output contracts apply per request; merged backend records can be larger.
            schema["properties"]["summary"]["maxLength"] = 3500
            if model is BusinessRecord:
                schema["properties"]["facts"]["maxItems"] = min(
                    16, schema["properties"]["facts"].get("maxItems", 16)
                )
                schema["properties"]["summary"]["maxLength"] = 800
                schema["$defs"]["BusinessFact"]["properties"]["statement"]["maxLength"] = 240
                for field, count, length in (
                    ("terminology", 16, 80),
                    ("conflicts", 8, 200),
                    ("open_questions", 10, 200),
                ):
                    schema["properties"][field].update(maxItems=count)
                    schema["properties"][field]["items"]["maxLength"] = length
            else:
                # This index is for cross-node consistency, not a second copy of
                # every sentence/table cell. Validation still reads the FULL body.
                schema["properties"]["claims"]["maxItems"] = 12
                schema["properties"]["summary"]["maxLength"] = 3500
                schema["properties"]["entities"]["maxItems"] = 20
                fields = schema["$defs"]["Claim"]["properties"]
                # Claim owns field lengths. Re-tightening them here rejected
                # otherwise valid facts without improving provenance checks.
                schema["$defs"]["Claim"]["required"].extend(["cardinality", "evidence_ids"])
                if context.get("evidence"):
                    # A default on the persisted model must not let the wire
                    # response silently omit provenance. The validator still
                    # checks whether these actual sources support the claim.
                    fields["evidence_ids"]["minItems"] = 1
                block_ids = [b["block_id"] for b in context.get("content", {}).get("blocks", [])]
                if block_ids:
                    schema["$defs"]["Claim"]["properties"]["block_ids"]["items"]["enum"] = block_ids
        if model is ActionDecision:
            available = context["available_actions"]
            declared = schema["properties"]["action"]["enum"]
            if not available or not set(available).issubset(declared):
                raise ValueError("No valid actions available for this decision")
            schema["properties"]["action"]["enum"] = list(dict.fromkeys(available))
        if model in {Review, LocalReview}:
            # Review results are diagnoses, not copies of the source document.
            finding_fields = schema["$defs"]["Finding"]["properties"]
            finding_fields["description"]["maxLength"] = 240
            finding_fields["key"]["maxLength"] = 120
            schema["$defs"]["Finding"]["required"].extend(["rule", "block_refs", "evidence_ids"])
            section_ids = (
                [context["section"]["section_id"]]
                if model is LocalReview
                else [s["section_id"] for s in context["sections"]]
            )
            if section_ids:
                field = schema["$defs"]["Finding"]["properties"]["section_ids"]
                field["items"]["enum"] = section_ids
                field["maxItems"] = len(section_ids)
        if model is LocalReview:
            refs = schema["$defs"]["FindingBlockRef"]["properties"]
            refs["section_id"]["enum"] = [context["section"]["section_id"]]
            block_ids = [b["block_id"] for b in context["content"].get("blocks", [])]
            if block_ids:
                refs["block_id"]["enum"] = block_ids
            else:
                finding_fields["block_refs"]["maxItems"] = 0
            evidence_ids = [e["evidence_id"] for e in context.get("evidence", [])]
            if evidence_ids:
                finding_fields["evidence_ids"]["items"]["enum"] = evidence_ids
            else:
                finding_fields["evidence_ids"]["maxItems"] = 0
            schema["$defs"]["Verdict"]["properties"]["reason"]["maxLength"] = 200
            claim_ids = [claim["claim_id"] for claim in context["extraction"]["claims"]]
            schema["properties"]["verdicts"].update(
                {"minItems": len(claim_ids), "maxItems": len(claim_ids)}
            )
            if claim_ids:
                schema["$defs"]["Verdict"]["properties"]["claim_id"]["enum"] = claim_ids
        if model is DrillDown:
            targets = list(dict.fromkeys(context["finding"]["section_ids"]))
            schema["properties"]["affected_section_ids"].update(
                {
                    "items": {"type": "string", "enum": targets},
                    "maxItems": len(targets),
                }
            )
            # Some constrained decoders emit only one anyOf branch and omit
            # required siblings (e.g. reason). Use one explicit response shape;
            # check_result still enforces the conditional repair requirements.
            schema["required"] = list(schema["properties"])
        from folio.orchestration.citation_review import citation_review_schema
        from folio.orchestration.cross_review import cross_review_schema
        from folio.orchestration.visual_obligations import visual_obligation_schema

        schema = cross_review_schema(model, schema, context)
        if model is SectionContent:
            schema = visual_obligation_schema(
                schema,
                context.get("required_visual_kinds", []),
                context.get("figure_policy", {}).get("allowed_visual_kinds"),
                self.preserved_figures(context),
            )
        from folio.orchestration.review_scope import applicability_schema

        return applicability_schema(model, citation_review_schema(task, schema, context), context)

    def request_cache_key(self, task, context, schema):
        from folio.research.native_tools import NATIVE_TOOL_TASKS, tool_definitions

        prompt = prompt_for(task, context, schema)
        runtime_identity = getattr(self.runtime, "identity", {})
        if task in NATIVE_TOOL_TASKS:
            # Old action-text results must never be mistaken for native calls.
            # Tool contracts are part of the provider request, even when they
            # no longer appear in the human-readable prompt.
            runtime_identity = runtime_identity | {
                "action_protocol": "native_tool_calls_v4_reasoning",
                "tools": tool_definitions(task, context, schema),
            }
        if task == "select_context" and hasattr(self.runtime, "material_selection_policy"):
            runtime_identity = runtime_identity | {
                "material_selection_policy": self.runtime.material_selection_policy
            }
        return (
            task
            + ":"
            + digest(
                prompt
                + (json.dumps(context, sort_keys=True) if task == "plan" else "")
                + json.dumps(runtime_identity, sort_keys=True)
            )
        )

    async def _call_unlocked(self, task: str, context: dict, model: type[T]) -> T:
        from folio.research.native_tools import NATIVE_TOOL_TASKS, native_result

        context = self.request_context(task, context)
        schema = self.request_schema(task, context, model)
        prompt_budget = (
            self.limits.max_planning_prompt_chars
            if task == "plan"
            else self.limits.max_prompt_chars
        )
        size = prompt_chars(self.runtime, task, context, schema)
        key = self.request_cache_key(task, context, schema)
        cached = self.store.task(self.job_id, key)
        if cached and cached["result"]:
            parsed = model.model_validate_json(cached["result"])
            self.check_result(parsed, context, task=task)
            cached_usage = json.loads(cached.get("usage") or "{}")
            if task in DECISION_TASKS:
                self.store.record_decision(
                    self.job_id,
                    key,
                    task,
                    context,
                    parsed.model_dump(),
                    {
                        "provider": cached_usage.get(
                            "decision_provider", getattr(self.runtime, "provider", "demo")
                        ),
                        "model": cached_usage.get(
                            "decision_model", getattr(self.runtime, "model", "offline-demo")
                        ),
                        "policy_version": 1,
                    },
                )
            record_cache_hit(task)
            record_task_cache_hit()
            self.store.audit_event(self.job_id, "task_cache_hit", {"task": task, "task_key": key})
            return parsed
        # A durable, validated result needs no provider connection. Capacity
        # services may be unavailable during recovery of an already completed call.
        capacity = await self.measure_request(task, context, model, schema)
        if not capacity["fits"]:
            if task == "plan":
                raise PlanningContextTooLarge(size, prompt_budget)
            raise ValueError(f"{task} request exceeds measured capacity: {capacity}")
        if cached and cached["result"] is None and cached["error"] is None:
            cached = self.store.recover_interrupted_task(self.job_id, key)
        if (
            cached
            and cached.get("error")
            and task
            in {
                "select_context",
                "understand_work",
                "complete_gaps",
                "read_gap_materials",
                "read_review_materials",
                "integrate_supplement",
                "check_supplement_conflicts",
                "repair_supplement_conflicts",
                "extract",
                "validate",
                "review_work",
                "review",
                "drill_down",
            }
        ):
            if "finish_reason='length'" in cached["error"]:
                raise ModelCallFailed("Cached request requires partitioning", "length_limit")
            for code in ("context_limit", "length_limit"):
                if f"[{code}]" in cached["error"]:
                    raise ModelCallFailed("Cached request requires partitioning", code)
        attempts = cached["attempts"] if cached else 0
        attempt_limit = (
            1
            if task
            in {
                "format_document",
                "review_layout",
                "check_supplement_conflicts",
                "repair_supplement_conflicts",
            }
            else self.limits.work_plan_attempts
            if task == "work_plan"
            else self.limits.call_attempts
        )
        if task == "integrate_supplement":
            attempt_limit = min(attempt_limit, 2)
        # Only an explicit paused/failed-job resume opens another bounded window. The
        # durable attempt counter and all global/research spending stay intact.
        attempt_limit += self.store.task_retry_baseline(self.job_id, key)
        while attempts < attempt_limit:
            capacity = await self.measure_request(task, context, model, schema)
            if not capacity["fits"]:
                raise ValueError(f"{task} retry exceeds measured capacity: {capacity}")
            context_ref = self.archive_context(
                "requests",
                {"task": task, "task_key": key, "context": context, "capacity": capacity},
            )
            from folio.runtime.call_budget import current_call_budget

            policy = current_call_budget.get()
            attempts = self.store.begin_task(
                self.job_id, key, self.limits.max_calls,
                **({"budget_policy": policy} if policy is not None else {}),
            )
            self.store.audit_event(
                self.job_id,
                "request_prepared",
                {
                    "task": task,
                    "task_key": key,
                    "attempt": attempts,
                    "context_ref": context_ref,
                },
            )
            session = f"folio:{self.request.document_id}:{self.job_id}:{key}:{attempts}"
            candidate = None
            result = None
            model_attempted = False
            queued_at, service_at = monotonic(), None
            try:
                async with self.model_slot(task):
                    service_at = monotonic()
                    record_model_call(task)
                    record_task_model_call()
                    model_attempted = True
                    from folio.runtime.interaction_audit import interaction_scope
                    from folio.runtime.timeouts import model_call_timeout

                    with interaction_scope(self.store, self.job_id, task, key, attempts), model_call_timeout(self.limits.call_timeout_seconds):
                        result = await asyncio.wait_for(
                            (
                                self.decisions.decide
                                if task in DECISION_TASKS and task not in NATIVE_TOOL_TASKS
                                else self.runtime.complete
                            )(task, context, schema, session),
                            timeout=self.limits.call_timeout_seconds,
                        )
                record_task_model_usage(result.usage)
                timing = {
                    "queue_ms": round((service_at - queued_at) * 1000),
                    "service_ms": round((monotonic() - service_at) * 1000),
                }
                if task in NATIVE_TOOL_TASKS:
                    # Keep the provider's actual call identities and arguments
                    # even for one-shot searches or rejected parameter sets.
                    self.archive_context("native_responses", {
                        "task": task, "task_key": key, "attempt": attempts,
                        "content": result.content, "tool_calls": result.tool_calls,
                        "reasoning_content": result.reasoning_content,
                        "usage": result.usage,
                    })
                if result.stop_reason != "completed":
                    raise ValueError(f"Incomplete runtime result: {result.stop_reason}")
                candidate = (
                    native_result(task, result, context, schema)
                    if task in NATIVE_TOOL_TASKS
                    else parse_json(result.content)
                )
                if task == "understand_work" and model is BusinessRecord:
                    from folio.orchestration.record_identity import normalize_record_ids

                    normalized = normalize_record_ids(candidate)
                    if normalized != candidate:
                        self.archive_context(
                            "record_id_normalization",
                            {"task_key": key, "original": candidate, "normalized": normalized},
                        )
                    candidate = normalized
                parsed = model.model_validate(candidate)
                self.check_result(parsed, context, task=task)
                self.store.finish_task(
                    self.job_id, key, result=parsed.model_dump_json(), usage=result.usage | timing
                )
                if task in DECISION_TASKS:
                    self.store.record_decision(
                        self.job_id,
                        key,
                        task,
                        context,
                        parsed.model_dump(),
                        {
                            "provider": result.usage.get(
                                "decision_provider", getattr(self.runtime, "provider", "demo")
                            ),
                            "model": result.usage.get(
                                "decision_model", getattr(self.runtime, "model", "offline-demo")
                            ),
                            "policy_version": 1,
                        },
                    )
                return parsed
            except Exception as exc:
                if model_attempted:
                    record_task_model_failure()
                exc.folio_failure_context = {
                    "task": task,
                    "task_key": key,
                    "attempt": attempts,
                    "code": getattr(
                        exc,
                        "code",
                        "validation_error" if isinstance(exc, ValueError) else "model_error",
                    ),
                }
                record_call_failure(task)
                if task == "work_plan":
                    from folio.runtime.work_plan_transport import WorkPlanFormatError

                    if isinstance(exc, WorkPlanFormatError):
                        candidate = exc.candidate
                error_code = f" [{exc.code}]" if isinstance(exc, ModelCallFailed) else ""
                if candidate is not None:
                    self.archive_context(
                        "rejected",
                        {
                            "task": task,
                            "task_key": key,
                            "attempt": attempts,
                            "candidate": candidate,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                from folio.runtime.diagnostics import (
                    partial_output_for_audit,
                    safe_failure_diagnostics,
                )

                diagnostics = safe_failure_diagnostics(exc)
                partial = partial_output_for_audit(exc)
                if partial is not None:
                    self.archive_context(
                        "failed_outputs",
                        {
                            "task": task,
                            "task_key": key,
                            "attempt": attempts,
                            "diagnostics": diagnostics,
                            "partial_output": partial,
                        },
                    )
                self.store.finish_task(
                    self.job_id,
                    key,
                    error=f"{type(exc).__name__}{error_code}: {exc}",
                    usage={
                        "queue_ms": round(((service_at or monotonic()) - queued_at) * 1000),
                        "service_ms": round((monotonic() - service_at) * 1000) if service_at else 0,
                        **({"diagnostics": diagnostics} if diagnostics else {}),
                    },
                )
                if isinstance(exc, ModelCallBudgetExceeded):
                    # An internal provider retry can exhaust the HTTP allowance
                    # after the logical task was admitted. Do not retry or route
                    # that failure to another model.
                    raise
                if isinstance(exc, ModelCallFailed) and exc.code in {
                    "schema_unsupported",
                    "authentication",
                    "billing",
                    "configuration",
                }:
                    # The backend cannot compile this schema. More output-format
                    # instructions cannot fix a deterministic grammar rejection.
                    raise
                if (
                    isinstance(exc, ModelCallFailed)
                    and exc.code == "length_limit"
                    and task
                    in {
                        "select_context",
                        "understand_work",
                        "complete_gaps",
                        "read_gap_materials",
                        "read_review_materials",
                        "extract",
                        "validate",
                        "review_work",
                        "review",
                        "drill_down",
                    }
                ):
                    raise
                if isinstance(exc, ModelCallFailed) and exc.code == "context_limit":
                    # Selection already split its inputs; other transports tried
                    # smaller output reservations where supported.
                    # Adding format feedback would make this request even larger.
                    raise
                if attempts >= attempt_limit:
                    raise
                if task == "integrate_supplement" and not isinstance(exc, ValueError):
                    # Correct one invalid edit; do not multiply transport failures.
                    raise
                if isinstance(exc, ValidationError):
                    feedback = json.dumps(
                        [
                            {"field": e["loc"], "error": e["msg"]}
                            for e in exc.errors(include_input=False, include_context=False)
                        ],
                        ensure_ascii=False,
                    )[:3000]
                elif isinstance(exc, ModelCallFailed) and exc.code == "stalled_output":
                    feedback = (
                        "The previous response kept emitting whitespace before completing its JSON or required fields; it was stopped and discarded. Return complete, compact JSON matching the schema, with every required field and no padding whitespace."
                    )
                elif isinstance(exc, ModelCallFailed) and exc.code == "repetition":
                    feedback = (
                        "The previous response repeated the same long paragraph three times or cycled through the same paragraph group three times; it was stopped and discarded. Do not repeat a paragraph under different block_ids. State each rule once; every block must add information, and existing model_proposals must not be repeated. End blocks after covering this node's required content and return complete JSON."
                    )
                elif isinstance(exc, ModelCallFailed) and exc.code == "length_limit":
                    feedback = (
                        "The previous response exhausted its output allowance before finishing JSON and was discarded. Merge repetitive wording and write only what this node requires, retaining necessary business rules and tables. Do not rewrite other nodes or repeat supplied model_proposals. Omit unnecessary default fields and return compact, complete JSON."
                    )
                elif isinstance(exc, ValueError):
                    feedback = str(exc)[:3000]
                else:
                    feedback = 'The previous call did not return a complete, valid result.'
                if task in NATIVE_TOOL_TASKS:
                    from folio.runtime.tool_calls import native_retry_context

                    context = native_retry_context(
                        task, context, result=result, error=exc, feedback=feedback,
                    )
                else:
                    context = context | {
                        "format_feedback": "Correct the specific errors and return complete JSON again: " + feedback
                    }
                if task == "work_plan" and candidate:
                    context["previous_invalid_work_plan"] = candidate
                    from folio.runtime.work_plan_transport import WorkPlanCoverageError

                    if isinstance(exc, WorkPlanCoverageError):
                        # Preserve valid model-chosen owners; ask only about omitted nodes.
                        # Final WorkPlan validation still checks the whole document and DAG.
                        context["fixed_work_tasks"] = candidate["tasks"]
                        context["sections"] = [
                            s for s in context["sections"] if s["section_id"] in exc.missing
                        ]
                        budget = context.get("execution_budget", {})
                        context["execution_budget"] = budget | {
                            "minimum_tasks": (len(exc.missing) + self.limits.max_work_sections - 1)
                            // self.limits.max_work_sections,
                            "content_node_count": len(exc.missing),
                        }
                        context.pop("previous_invalid_work_plan", None)
        raise TaskRetriesExhausted(key, cached.get("error") if cached else None)

    def check_result(self, parsed, context, *, task=None):
        from folio.orchestration.supplement_integration import (
            SupplementIntegration,
            apply_integration,
            validate_checked_patch,
        )

        if isinstance(parsed, SupplementIntegration):
            item = context["integration_item"]
            if context.get("supplement_conflicts_checked"):
                validate_checked_patch(
                    SectionContent.model_validate(context["current"]), parsed, item
                )
            if item.get("disposition") == "record_only" and (
                parsed.updates or parsed.remove_block_ids
            ):
                raise ValueError("Record-only supplement cannot edit the document body")
            apply_integration(
                SectionContent.model_validate(context["current"]),
                parsed,
                set(context.get("existing_evidence_ids", []))
                | {e["evidence_id"] for e in context.get("evidence", [])},
                model_supplement=not item.get("evidence_refresh"),
                protected_block_ids=context.get("protected_block_ids", []),
            )
        validate_review_result(parsed, context)
        from folio.orchestration.review_scope import validate_applicability

        validate_applicability(parsed, context)
        from folio.orchestration.citation_review import validate_citation_review
        from folio.orchestration.cross_review import validate_cross_review

        validate_citation_review(parsed, context, task=task)
        if isinstance(parsed, (Review, LocalReview)) and context.get("writing_guidance"):
            from folio.orchestration.advisory import partition_advisory_findings

            actionable, notes = partition_advisory_findings(parsed, enabled=True)
            if notes:
                self.archive_context(
                    "advisory_notes",
                    {
                        "guide": {
                            k: context["writing_guidance"][k] for k in ("id", "version", "snapshot")
                        },
                        "notes": [note.model_dump() for note in notes],
                    },
                )
                parsed.findings = actionable
        validate_cross_review(parsed, context)
        if isinstance(parsed, SectionContent):
            from folio.document.presentation import validate_document_content
            from folio.runtime.output_guard import validate_paragraph_cycles
            from folio.runtime.writing_quality import validate_writing_repetition
            from folio.runtime.writing_transport import validate_writing_provenance

            validate_writing_provenance(task, context, parsed.model_dump())
            validate_writing_repetition(task, context, parsed.model_dump())
            if task in {"generate", "repair"}:
                validate_paragraph_cycles(parsed.model_dump())
            validate_document_content(
                parsed, [e["evidence_id"] for e in context.get("evidence", [])]
            )
            from folio.orchestration.visual_obligations import validate_required_visuals

            validate_required_visuals(
                parsed.model_dump(),
                context.get("required_visual_kinds", []),
                context.get("figure_policy", {}).get("allowed_visual_kinds"),
                self.preserved_figures(context),
            )
        if isinstance(parsed, GapMaterialRead):
            available = {x["context_ref"]: x["kind"] for x in context["catalog"]}
            for item in parsed.constraints:
                if any(available.get(ref) != item.source_kind for ref in item.source_refs):
                    raise ValueError(
                        "Gap material notes must retain each source's reference and kind"
                    )
        if isinstance(parsed, ContextSelection):
            from folio.orchestration.context import (
                SELECTION_REFERENCE_ERROR,
                normalize_selection_references,
            )

            normalize_selection_references(parsed, context["catalog"])
            available = {x["context_ref"]: x["kind"] for x in context["catalog"]}
            for refs, kinds in (
                (parsed.selected_refs, {"fact", "proposal", "evidence"}),
                (parsed.risk_refs, {"risk"}),
            ):
                if len(refs) != len(set(refs)) or any(
                    available.get(ref) not in kinds for ref in refs
                ):
                    raise ValueError(SELECTION_REFERENCE_ERROR)
            if bool(parsed.risk_refs) != bool(parsed.risk_summary.strip()):
                raise ValueError(
                    "Risk summary must have source risk references, and vice versa. When risk_refs=[], risk_summary must be the empty string, not a no-risk statement or new risk analysis. When selecting risks, reference context_refs of catalog items with kind=risk and summarize only those selected risks."
                )

        if isinstance(parsed, PlanProposal):
            build_plan(self.request.document_id, parsed)
        if isinstance(parsed, GapCompletion):
            expected = {g["gap_id"] for g in context["gaps"]}
            ids = [p.gap_id for p in parsed.proposals]
            sections = {s["section_id"] for s in context["sections"]}
            if set(ids) != expected or len(ids) != len(expected):
                raise ValueError("Gap completion must cover every requested gap exactly once")
            if any(
                not set(p.section_ids).issubset(sections) or not p.content.strip()
                for p in parsed.proposals
            ):
                raise ValueError("Gap proposal must use the supplied document section IDs")
        if isinstance(parsed, WorkPlan):
            validate_work_plan(parsed, self.plan)
            oversized = [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "node_count": len(t.section_ids),
                    "section_ids": t.section_ids,
                }
                for t in parsed.tasks
                if len(t.section_ids) > self.limits.max_work_sections
            ]
            if oversized:
                raise ValueError(
                    "Business task exceeds max_sections_per_task="
                    + str(self.limits.max_work_sections)
                    + "; oversized="
                    + json.dumps(oversized, ensure_ascii=False)
                    + "; split these tasks by business subproblem. Count every assigned node, including overview headings. Preserve ownership of all nodes, update dependencies for new tasks, and leave tasks that already satisfy the constraints unchanged."
                )
        if isinstance(parsed, BusinessRecord):
            available = {e["evidence_id"] for e in context["evidence"]}
            invalid = [
                {
                    "path": f"/facts/{index}/evidence_ids",
                    "fact_id": fact.fact_id,
                    "invalid_ids": sorted(set(fact.evidence_ids) - available),
                }
                for index, fact in enumerate(parsed.facts)
                if not set(fact.evidence_ids).issubset(available)
            ]
            if invalid:
                raise ValueError(
                    "Business record referenced unknown evidence: "
                    + json.dumps(invalid, ensure_ascii=False)
                    + "; allowed_evidence_ids="
                    + json.dumps(sorted(available))
                    + "; evidence_ids may reference only evidence_id values in this request's evidence, not section, fact or proposal IDs. Verify and correct citations using original sources. Move unsupported statements to open_questions; do not guess replacement identifiers."
                )
        if isinstance(parsed, ActionDecision):
            if parsed.action not in context["available_actions"]:
                raise ValueError(
                    f"Decision selected an unavailable action: {parsed.action!r}; "
                    f"available_actions={context['available_actions']!r}; "
                    f"search_count={context.get('search_count', 0)}. "
                    "Choose only from available_actions. Do not search when search is unavailable; "
                    "if evidence is insufficient, select pending and list missing_information so the program can address the gaps."
                )
            if not set(parsed.evidence_refs).issubset(
                e["evidence_id"] for e in context["evidence"]
            ):
                raise ValueError("Decision referenced unknown evidence")
        if isinstance(parsed, LocalReview):
            claim_ids = {c["claim_id"] for c in context["extraction"]["claims"]}
            if (
                len(parsed.verdicts) != len(claim_ids)
                or {v.claim_id for v in parsed.verdicts} != claim_ids
            ):
                raise ValueError("Validator must return exactly one verdict for every claim")
            self.check_findings(parsed.findings, {context["section"]["section_id"]})
            available_blocks = {b["block_id"] for b in context["content"].get("blocks", [])}
            for finding in parsed.findings:
                if any(ref.block_id not in available_blocks for ref in finding.block_refs):
                    raise ValueError("Reviewer referenced a body block outside its input")
                if not set(finding.evidence_ids).issubset(
                    e["evidence_id"] for e in context.get("evidence", [])
                ):
                    raise ValueError("Reviewer referenced evidence outside its input")
        if isinstance(parsed, Review):
            self.check_findings(parsed.findings, {s["section_id"] for s in context["sections"]})

        if isinstance(parsed, DrillDown) and parsed.confirmed and not parsed.needs_confirmation:
            ids = parsed.affected_section_ids
            if (
                not ids
                or len(ids) != len(set(ids))
                or not set(ids).issubset(context["finding"]["section_ids"])
                or not parsed.repair_instruction.strip()
            ):
                raise ValueError(
                    "Repair must name unique document section IDs from finding.section_ids and a nonempty repair_instruction, not business task IDs"
                )

    async def _execute(self, job: dict) -> None:
        if job["plan"]:
            plan = DocumentPlan.model_validate(job["plan"])
        else:
            from folio.orchestration.directory_planning import plan_job_from_sources

            plan = await plan_job_from_sources(self)
            self.store.save_plan(self.job_id, plan)
            self.store.work_event(
                self.job_id,
                f"已建立文档计划：{len(plan.sections)} 个章节节点，接下来按业务问题规划执行任务。",
            )
        if plan.template is not None:
            from folio.document.template_contract import validate_template_plan

            validate_template_plan(plan)
        self.plan = plan
        saved_ids = {s.section_id for s in self.store.states(self.job_id)}
        if job["stage"] in {"review", "illustrating", "rendering", "formatting"} and all(
            s.section_id in saved_ids for s in plan.sections
        ):
            # A review failure does not restart completed writing/research with
            # an empty repair instruction. business_findings checks dependency
            # versions below and refreshes only genuinely stale work.
            await self.prepare_work_plan()
        else:
            self._writing_active = True
            try:
                await self.run_business_tasks()
            finally:
                self._writing_active = False
        from folio.confirmations import apply_pending_confirmations
        from folio.orchestration.provenance import revalidate_saved_references

        await apply_pending_confirmations(self, getattr(self, "confirmation_rounds", []))
        if getattr(self, "confirmation_rounds", []):
            from folio.confirmations import ConfirmedReferences, rounds

            self.confirmation_rounds = rounds(self.store, self.job_id)
            self.references = ConfirmedReferences(
                self.request.reference_paths, self.request.reference_revision_view,
                self.confirmation_rounds,
            )
        await revalidate_saved_references(self)
        self.store.update(self.job_id, stage="review")
        # Each iteration either terminates or advances a bounded section revision.
        while True:
            states = self.store.states(self.job_id)
            definitions = {section.section_id: section for section in plan.sections}
            deterministic = [
                finding
                for state in states
                if definitions[state.section_id].content_mode != "container"
                for finding in hard_findings(
                    definitions[state.section_id],
                    state.content,
                    state.evidence,
                    state.extraction,
                    (
                        e.evidence_id
                        for e in self.references.evidence(definitions[state.section_id], None)
                    ),
                )
            ]
            from folio.orchestration.visual_obligations import visual_obligation_findings

            deterministic.extend(
                visual_obligation_findings(
                    [s.model_dump() for s in plan.sections],
                    [s.model_dump() for s in states],
                    user_request=self.request.user_request,
                )
            )
            hard_keys = {finding.fingerprint for finding in deterministic}
            findings = deterministic + [f for s in states for f in s.local_findings]
            from folio.orchestration.public_evidence_review import public_evidence_candidates

            # This is a source-scope question, not a deterministic business error.
            # Public facts can be legitimate; ordinary adjudication may dismiss it.
            findings.extend(public_evidence_candidates(states))
            from folio.orchestration.known_proposals import known_proposal_candidates

            findings.extend(
                known_proposal_candidates(
                    states,
                    [
                        proposal
                        for work in self.store.work_states(self.job_id).values()
                        for proposal in work.proposals
                    ],
                )
            )
            findings.extend(await self.business_findings())
            candidates = deduplicate_review_findings(findings)
            findings = candidates.findings
            if candidates.audit:
                self.archive_context("review_deduplication", candidates.audit)
            dismissed = self._dismissed()
            findings = [
                f
                for f in findings
                if f.fingerprint in hard_keys or self._finding_version(f, states) not in dismissed
            ]
            findings.extend(await self.global_review(states))
            from folio.confirmations import open_business_questions

            findings.extend(
                self.effective_findings(
                    open_business_questions(getattr(self, "confirmation_rounds", [])), states
                )
            )
            candidates = deduplicate_review_findings(findings)
            findings = candidates.findings
            if candidates.audit:
                self.archive_context("review_deduplication", candidates.audit)
            # Unresolved comments in factual sources remain explicit delivery blockers.
            for comment in self.references.comments():
                if not comment["resolved"]:
                    targets = [
                        s.section_id
                        for s in plan.sections
                        if s.content_mode != "container"
                        and any(
                            c["source"] == comment["source"]
                            and c["comment_id"] == comment["comment_id"]
                            for c in self.references.comments(s)
                        )
                    ]
                    findings.append(
                        Finding(
                            kind="pending",
                            key=f"source-comment:{comment['source']}:{comment['comment_id']}",
                            description='Unresolved comment in the source document: ' + comment["text"][:1500],
                            section_ids=targets or [s.section_id for s in plan.sections
                                                    if s.content_mode != "container"]
                            or [plan.sections[0].section_id],
                        )
                    )
            snapshot = self.shared_state(states)
            snapshot.update(
                {
                    "blocking_issues": [f.model_dump() for f in findings],
                    "review_passed": not findings,
                    "state_digest": digest("".join(s.model_dump_json() for s in states)),
                    "source_audit": self.references.audit(),
                    "writing_profile": self.writing_profile,
                    "work_plan": self.work_plan.model_dump(),
                    "work_states": {
                        k: v.model_dump() for k, v in self.store.work_states(self.job_id).items()
                    },
                }
            )
            self.store.update(self.job_id, snapshot=snapshot)
            if not findings:
                await self.finish(states, draft=self.request.runtime == "demo")
                return
            repairable = [f for f in findings if f.kind != "pending"]
            ReviewProgress(self, "drill_down", states, len(repairable)).emit(
                "已汇总章节校验、业务任务审阅和跨章节检查："
                f"去重后共 {len(findings)} 项未解决疑点，本轮精查 {len(repairable)} 项；"
                f"其余 {len(findings) - len(repairable)} 项已待确认。",
                status="summary",
                unresolved=len(findings),
                to_adjudicate=len(repairable),
            )
            if not repairable:
                await self.finish(states, draft=True, needs_confirmation=True)
                return
            # Read-only adjudications share an immutable snapshot. Repair independent
            # owners together; overlapping readers/writers wait for the next snapshot.
            decisions = await self.adjudicate_findings(repairable, states, hard_keys=hard_keys)
            confirmed = []
            pending = []
            dismissed_count = 0
            for candidate, result, error in decisions:
                if error or result.needs_confirmation:
                    pending.append(
                        Finding(
                            kind="pending",
                            key="adjudication:" + candidate.fingerprint,
                            description='Detailed review was inconclusive. Original text is preserved pending confirmation: '
                            + candidate.description[:1600],
                            section_ids=candidate.section_ids,
                        )
                    )
                elif not result.confirmed:
                    if candidate.fingerprint in hard_keys:
                        pending.append(
                            Finding(
                                kind="pending",
                                key="adjudication:" + candidate.fingerprint,
                                description='Model opinions cannot override remaining structural or source errors: '
                                + candidate.description[:1600],
                                section_ids=candidate.section_ids,
                            )
                        )
                    else:
                        self._dismiss(candidate, states, result.reason)
                        dismissed_count += 1
                else:
                    # A finding's read scope may include an exhausted chapter
                    # which is already correct. Charge only the actual writes.
                    revisions = {s.section_id: s.revision for s in states}
                    exhausted = [
                        sid
                        for sid in result.affected_section_ids
                        if revisions[sid] - getattr(self, "repair_baselines", {}).get(sid, 1)
                        >= self.limits.max_repairs
                    ]
                    if exhausted:
                        pending.append(
                            Finding(
                                kind="pending",
                                key="repair-budget:" + candidate.fingerprint,
                                description='The actual revision target reached its revision limit. The issue remains pending confirmation: '
                                + candidate.description[:1500],
                                section_ids=candidate.section_ids,
                            )
                        )
                    else:
                        confirmed.append((candidate, result))
            from folio.orchestration.repair_batch import apply_repair_batch, independent_repairs

            chosen = independent_repairs(confirmed, self.work_plan)
            dismissed = self._dismissed()
            remaining = [
                f
                for f in findings
                if f.fingerprint in hard_keys or self._finding_version(f, states) not in dismissed
            ]
            snapshot.update(self.shared_state(states))
            snapshot["blocking_issues"] = [f.model_dump() for f in remaining + pending]
            snapshot["review_passed"] = not snapshot["blocking_issues"]
            self.store.update(self.job_id, snapshot=snapshot)
            disposition = ReviewProgress(self, "repair", states, 0)
            disposition.emit(
                f"精查结论已处理：排除 {dismissed_count} 项疑点，{len(pending)} 项保留待确认。"
                + (
                    f"接下来并行修订 {len(chosen)} 项互不影响的已确认问题，再统一复查。"
                    if chosen
                    else "本轮保留原正文，继续生成草稿。"
                    if remaining or pending
                    else "本轮无需自动修订正文。"
                ),
                status="summary",
                dismissed=dismissed_count,
                pending=len(pending),
                selected_for_repair=len(chosen),
            )
            if not chosen:
                if pending:
                    self.store.work_event(
                        self.job_id, "部分问题仍需确认，保留原正文和问题清单，继续生成草稿。"
                    )
                await self.finish(
                    states,
                    draft=bool(remaining or pending) or self.request.runtime == "demo",
                    needs_confirmation=bool(remaining or pending),
                )
                return
            accepted = await apply_repair_batch(self, chosen, states, snapshot)
            if not accepted:
                await self.finish(states, draft=True, needs_confirmation=True)
                return

    def required_visuals(self, section_id):
        return tuple(self.figure_policy(section_id)["required_visual_kinds"])

    def preserved_figures(self, context):
        prohibited = set(context.get("figure_policy", {}).get("prohibited_visual_kinds", []))
        return [
            visual
            for visual in context.get("current", {}).get("visuals", [])
            if visual["kind"] not in prohibited
        ]

    def figure_policy(self, section_id):
        from folio.orchestration.visual_obligations import figure_policy

        return figure_policy(
            section_id,
            [s.model_dump() for s in self.plan.sections],
            self.request.user_request,
        )

    async def generate(
        self,
        section: Section,
        previous: SectionState | None = None,
        finding: Finding | None = None,
        instruction: str = "",
    ) -> SectionState:
        if section.content_mode == "container":
            return SectionState(
                section_id=section.section_id,
                content=SectionContent(),
                extraction=Extraction(summary=section.title + ': outline container'),
                evidence=[],
            )
        task = self.owner(section.section_id)
        table_structure_repair = bool(
            previous
            and finding
            and finding in previous.local_findings
            and finding.kind == "coverage"
            and (
                finding.key == "required-table" and section.expected_table_headers
                or finding.key.startswith("required-table:") and section.required_tables
            )
        )
        template_heading_repair = bool(
            previous
            and finding
            and finding in previous.local_findings
            and finding.kind == "coverage"
            and finding.key.startswith("template-heading:")
            and not section.allow_subheadings
        )
        if table_structure_repair and finding.key == "required-table":
            # The acceptance condition is already known; looking for new business
            # evidence cannot fix a missing header and may rewrite unrelated facts.
            instruction = (
                "Repair only the missing required table headers in the current body. At least one table must have this exact first row: "
                + json.dumps(section.expected_table_headers, ensure_ascii=False)
                + ". Organize existing data by column meaning, retaining business content, sources and red proposal provenance. Add no source names, business facts or writing advice. Do not create new research gaps for a structural repair."
            )
        elif table_structure_repair:
            instruction = (
                "Repair only the structure of template-required tables. Each required_tables entry needs its own table with exactly the specified headers in the first row and at least min_data_rows data rows. header_rows counts physical header rows in the original Word template, not the model output. Constraints: "
                + json.dumps(
                    [table.model_dump() for table in section.required_tables], ensure_ascii=False
                )
                + ". Use existing body and evidence to organize data, preserving business content, sources, red proposal provenance and pending items. Do not invent facts to fill rows or introduce research gaps for a structural repair."
            )
        elif template_heading_repair:
            instruction = (
                "Repair only the additional body subheadings that changed the fixed template directory. section.allow_subheadings=false: convert added heading blocks to ordinary paragraphs while preserving all business content, provenance and red proposal styling. Keep the confirmed directory unchanged; add no facts or new research gaps."
            )
        # Editing saved prose is not a new research objective. Reuse its sources
        # first; only newly reported gaps below can consume remaining retrieval.
        work = self.store.work_states(self.job_id)[task.task_id]

        def available_evidence(current_work):
            sources = {
                e.evidence_id: e
                for e in (*(previous.evidence if previous else []), *current_work.evidence)
            }
            if previous:
                referenced = {
                    upstream["section_id"]: dependency_evidence_ids(upstream)
                    for dependency in self.dependency_context(task)
                    for upstream in dependency["sections"]
                }
                # A revised upstream may publish a new supported fact after this
                # task's research. Bring only that fact's original sources forward.
                for state in self.store.states(self.job_id):
                    for evidence in state.evidence:
                        if evidence.evidence_id in referenced.get(state.section_id, set()):
                            sources.setdefault(evidence.evidence_id, evidence)
            return list(sources.values())

        evidence = available_evidence(work)
        missing = self.unfilled_gaps(work)
        known_repair_gaps = (
            {gap.strip() for gap in previous.content.pending_items + work.missing}
            if previous
            else set()
        )
        revision = previous.revision + 1 if previous else 1
        if not work.writable and previous is None:
            content = SectionContent(
                blocks=[Block(
                    block_id="pending",
                    text=localize_text(
                        "待确认：当前业务任务缺少充分依据。", self.request.language
                    ),
                )],
            )
        else:
            context = {
                **self.business_context(task, current_section=section.section_id),
                "section": section.model_dump(),
                "evidence": [e.model_dump() for e in evidence],
                "required_project_evidence_ids": [
                    e.evidence_id for e in self.references.evidence(section, None)
                ],
                "global_facts": work.global_facts.get(section.section_id, []),
                "revision": revision,
                "known_gaps": missing,
                "source_comments": self.references.comments(section),
                "writing_profile": self.writing_profile,
                "required_visual_kinds": self.required_visuals(section.section_id),
                "figure_policy": self.figure_policy(section.section_id),
                "child_sections": [
                    s.model_dump() for s in self.plan.sections if s.parent_id == section.section_id
                ],
            }
            if previous:
                context.update(
                    {
                        "current": previous.content.model_dump(),
                        "finding": finding.model_dump(),
                        "repair_instruction": instruction,
                    }
                )
            content = await self.call("repair" if previous else "generate", context, SectionContent)
        # New gaps are research inputs for a bounded rewrite, not appendices pasted
        # into the document. The writer owns structure, relevance and deduplication.
        from folio.orchestration.completed_proposals import reuse_exact_written_proposals

        work = self.store.work_states(self.job_id)[task.task_id]
        completion_allowed = self.model_completion_allowed(work)
        content, already_written = reuse_exact_written_proposals(
            content, work.proposals, section.section_id, completion_allowed=completion_allowed
        )
        if already_written:
            self.archive_context("already_integrated_proposals", {"matches": already_written})
            self.store.work_event(
                self.job_id,
                f"《{section.title}》{len(already_written)}项补充内容已完整写入正文，"
                "跳过重复整合，继续独立核验。",
            )
        gaps = list(content.pending_items)
        if not content.blocks and not gaps:
            # An empty writer response is not a completed section. Feed the
            # confirmed requirement into the same bounded research/completion
            # path immediately, before extraction and global review.
            gaps.append(f"{section.title}: The body has not been generated. Research this section's requirements: {section.instruction}")
            content = content.model_copy(update={"pending_items": list(gaps)})
        from folio.document.presentation import missing_provenance

        old_blocks = {b.block_id: b for b in previous.content.blocks} if previous else {}
        untraced = [
            b.block_id
            for b in content.blocks
            if missing_provenance(b) and old_blocks.get(b.block_id) != b
        ]
        if untraced:
            gaps.append(
                f"{section.title}: Body blocks {', '.join(untraced)} are marked evidence-backed but have no citations. "
                "Check original sources and add precise citations when supported. Business designs still unsupported after three research rounds must be model_proposal. "
                "Leave unavailable people, dates and other metadata blank; never invent them."
            )
        if previous:
            completed = {proposal.gap_id for proposal in work.proposals}
            gaps = [
                gap
                for gap in gaps
                if gap.strip() not in known_repair_gaps and digest(gap) not in completed
            ]
        premature_supplements = any(
            b.origin == "model_proposal" and old_blocks.get(b.block_id) != b for b in content.blocks
        ) and not self.model_completion_allowed(work)
        if premature_supplements and not gaps:
            gap = f"{section.title}: Supplemental content lacks evidence; research this node's business rules and processing conditions."
            if gap.strip() not in known_repair_gaps and digest(gap) not in {
                proposal.gap_id for proposal in work.proposals
            }:
                gaps = [gap]
        if gaps and not (table_structure_repair or template_heading_repair):
            async with self._work_locks.setdefault(task.task_id, asyncio.Lock()):
                # Concurrent nodes must merge against the latest task record,
                # never overwrite a sibling's completed gap proposals.
                work = self.store.work_states(self.job_id)[task.task_id]
                before_proposals = {p.gap_id: p.model_dump() for p in work.proposals}
                if previous:
                    completed = {proposal.gap_id for proposal in work.proposals}
                    gaps = [
                        gap
                        for gap in gaps
                        if gap.strip() not in {value.strip() for value in work.missing}
                        and digest(gap) not in completed
                    ]
                refresh_gap_content = bool(gaps)
                requested_gaps = list(gaps)
                supported_gaps = []
                if gaps and not self.model_completion_allowed(work):
                    work = await self.research_work(
                        task, work.research_instruction, writing_gaps=gaps
                    )
                    remaining = {gap.strip() for gap in work.missing}
                    # Research owns the whole business task; this writer owns only
                    # the gaps it raised. Never reassign every task-wide proposal
                    # to the current section as a side effect of one local search.
                    gaps = [gap for gap in requested_gaps if gap.strip() in remaining]
                    supported_gaps = [gap for gap in requested_gaps if gap.strip() not in remaining]
                evidence = available_evidence(work)
                if gaps:
                    await self.complete_gaps(task, work, gaps, [section.section_id])
                proposals = [
                    p.model_dump()
                    for p in work.proposals
                    if section.section_id in p.section_ids
                    and (
                        p.gap_id in {digest(gap) for gap in gaps}
                        or before_proposals.get(p.gap_id) != p.model_dump()
                    )
                ]
            if refresh_gap_content:
                from folio.orchestration.supplement_integration import integrate_supplements

                refreshed = (
                    context
                    | self.business_context(task, current_section=section.section_id)
                    | {
                        "evidence": [e.model_dump() for e in evidence],
                        "known_gaps": self.unfilled_gaps(work),
                    }
                )
                if not content.blocks and previous is None:
                    # A deferred/empty first draft has no established prose to
                    # protect. Write it once from the updated research/proposals,
                    # including required tables and figure requests.
                    content = await self.call("generate", refreshed, SectionContent)
                else:
                    content = await integrate_supplements(
                        self,
                        refreshed
                        | {
                            "current": content.model_dump(),
                            "integration_gaps": list(dict.fromkeys([*requested_gaps, *gaps])),
                            "repair_instruction": "Revise this section using the supplemental gap records. Integrate business proposals into formal body text with origin=model_proposal. Keep record_only information in backend records; leave unknown people, dates and versions blank in tables, without invention or writing advice. Preserve existing business content, merge duplicate explanations and do not relist resolved gaps.",
                        },
                        proposals,
                        supported_gaps=supported_gaps,
                    )
        from folio.document.presentation import normalize_content

        content = normalize_content(content, section)
        for block in content.blocks:
            if block.origin == "model_proposal":
                block.evidence_ids = []
        return await self.validate_section(section, content, evidence, revision)

    async def validate_section(
        self, section, content, evidence, revision, feedback=""
    ) -> SectionState:
        shared = self.business_context(
            self.owner(section.section_id), current_section=section.section_id
        )
        from folio.orchestration.provenance import normalize_fact_references

        content = normalize_fact_references(content, evidence, shared["business_record"])
        context = {
            "section": section.model_dump(),
            "content": content.model_dump(),
            "evidence": [e.model_dump() for e in evidence],
            "revision": revision,
            "feedback": feedback,
            "business_context": {
                key: shared[key]
                for key in ("work_task", "business_record", "model_proposals", "dependencies")
            },
            "required_project_evidence_ids": [
                e.evidence_id for e in self.references.evidence(section, None)
            ],
        }
        # Extract only this section's statements. Cross-section context belongs
        # to validation/task review and can otherwise double the input size.
        extraction_context = {
            key: context[key] for key in ("section", "content", "evidence", "revision", "feedback")
        }
        try:
            extraction = await self.call("extract", extraction_context, Extraction)
        except (ModelCallFailed, TimeoutError) as exc:
            return self.incomplete_local_check(section, content, evidence, revision, "extract", exc)
        proposal_blocks = {b.block_id for b in content.blocks if b.origin == "model_proposal"}
        extraction.claims = [
            c for c in extraction.claims if not proposal_blocks.intersection(c.block_ids)
        ]
        # The model has no authority to promote records by setting status itself.
        for claim in extraction.claims:
            claim.status = "candidate"
        findings = hard_findings(
            section,
            content,
            evidence,
            extraction,
            (e.evidence_id for e in self.references.evidence(section, None)),
        )
        try:
            review = await self.call(
                "validate", context | {"extraction": extraction.model_dump()}, LocalReview
            )
        except (ModelCallFailed, TimeoutError) as exc:
            return self.incomplete_local_check(
                section, content, evidence, revision, "validate", exc, extraction
            )
        claim_ids = {c.claim_id for c in extraction.claims}
        if (
            len(review.verdicts) != len(claim_ids)
            or {v.claim_id for v in review.verdicts} != claim_ids
        ):
            raise ValueError("Validator must return exactly one verdict for every claim")
        self.check_findings(review.findings, {section.section_id})
        findings.extend(review.findings)
        supported = {v.claim_id for v in review.verdicts if v.supported}
        for verdict in review.verdicts:
            if not verdict.supported:
                findings.append(
                    Finding(
                        kind="unsupported",
                        description=verdict.reason,
                        key=verdict.claim_id,
                        section_ids=[section.section_id],
                    )
                )
        # Promote only when the section passes every local check, including unextracted prose.
        if not findings:
            for claim in extraction.claims:
                if claim.claim_id in supported:
                    claim.status = "supported"
        self.store.work_event(
            self.job_id, f"“{section.title}”局部检查完成：{len(findings)} 项需处理的问题。"
        )
        return SectionState(
            section_id=section.section_id,
            revision=revision,
            content=content,
            extraction=extraction,
            evidence=evidence,
            local_findings=findings,
            claim_verdicts=review.verdicts,
            validation_completed=True,
        )

    def incomplete_local_check(
        self, section, content, evidence, revision, task, error, extraction=None
    ):
        """Keep completed prose when a bounded model check cannot finish, never pass it."""
        code = "timeout" if isinstance(error, TimeoutError) else error.code
        reasons = {
            "stalled_output": "模型未完成结构化结果，连续输出空白",
            "length_limit": "模型输出达到上限，结果不完整",
            "protocol": "模型未返回完整有效的结构化结果",
            "repetition": "模型重复输出，结果未采用",
            "timeout": "模型核验请求超时",
        }
        if code not in reasons:
            raise error  # Configuration, capacity and programming failures stay explicit.
        label = "事实抽取" if task == "extract" else "正文核验"
        extraction = extraction or Extraction(summary=section.title + ': fact extraction remains incomplete')
        for claim in extraction.claims:
            claim.status = "candidate"
        findings = hard_findings(
            section,
            content,
            evidence,
            extraction,
            (e.evidence_id for e in self.references.evidence(section, None)),
        )
        findings.append(
            Finding(
                kind="pending",
                key=f"local-check-incomplete:{task}:{code}",
                description=f"{label}未完成：{reasons[code]}。已保留正文，尚未通过完整核验，需确认。",
                section_ids=[section.section_id],
            )
        )
        self.store.work_event(
            self.job_id,
            f"《{section.display_number} {section.title}》{label}未完成：{reasons[code]}。"
            "已保留本节正文并标记待确认，继续处理其他章节。",
        )
        return SectionState(
            section_id=section.section_id,
            revision=revision,
            content=content,
            extraction=extraction,
            evidence=evidence,
            local_findings=findings,
        )

    async def global_review(self, states: list[SectionState]) -> list[Finding]:
        groups = review_groups(self.plan, self.work_plan, states, self.limits.review_batch_size)
        limit = self.limits.max_parallel_reviews
        progress = ReviewProgress(self, "consistency", states, len(groups))
        started = monotonic()
        plan_ref = self.archive_context(
            "review_plans",
            {
                "policy": 4,
                "max_parallel_reviews": limit,
                "groups": [
                    [{"section_id": s.section_id, "revision": s.revision} for s in group.sections]
                    for group in groups
                ],
                "group_reasons": [group.reasons for group in groups],
            },
        )
        progress.emit(
            f"开始检查章节间一致性：共 {len(groups)} 组，最多 {limit} 组同时处理；"
            "核对相关正文、事实及补写的适用条件与具体差异，发现疑点后精查原始依据。",
            status="summary",
            step="started",
            plan_ref=plan_ref,
        )

        async def review_batch(item):
            index, group = item
            batch = group.sections
            owners = {self.owner(s.section_id).task_id for s in batch}
            identity = {s.section_id for s in batch}
            summaries = [
                {
                    "section_id": s.section_id,
                    "revision": s.revision,
                    "work_task_id": self.owner(s.section_id).task_id,
                    "heading_path": next(
                        n.heading_path or [n.title]
                        for n in self.plan.sections
                        if n.section_id == s.section_id
                    ),
                    **s.extraction.model_dump(),
                    "comparison_blocks": [
                        b.model_dump()
                        for b in s.content.blocks
                        if any(b.block_id in c.block_ids for c in s.extraction.claims)
                    ],
                    "model_proposals": [
                        b.model_dump() for b in s.content.blocks if b.origin == "model_proposal"
                    ],
                }
                for s in batch
            ]
            review_context = {
                "review_scope": "cross_section_consistency",
                "sections": summaries,
                "work_tasks": [t.model_dump() for t in self.work_plan.tasks if t.task_id in owners],
                "review_topics": group.reasons,
            }
            try:
                with progress.unit(
                    index,
                    [s.section_id for s in batch],
                    f"跨章节检查第 {index}/{len(groups)} 组："
                    + progress.titles([s.section_id for s in batch])
                    + "。",
                    reasons=group.reasons,
                ) as data:
                    review = await self.call("review", review_context, Review)
                    self.check_findings(review.findings, identity)
                    data["findings"] = [finding_detail(f) for f in review.findings]
            except (ValidationError, json.JSONDecodeError, ModelCallFailed, TaskRetriesExhausted) as exc:
                if isinstance(exc, ModelCallFailed) and exc.code not in {"protocol", "validation_error"}:
                    raise
                pending = Finding(
                    kind="pending",
                    key="cross-review-incomplete:" + digest(json.dumps(review_context, sort_keys=True)),
                    description=(
                        "Cross-section review could not be completed because the model response "
                        "could not be validated after bounded retries. Saved content is preserved; "
                        "this group remains unverified and needs another check."
                    ),
                    section_ids=[s.section_id for s in batch],
                )
                progress.units[index]["findings"] = [finding_detail(pending)]
                return [pending]
            return review.findings

        def completed(done, total, index):
            data = progress.units[index]
            count = len(data["findings"])
            if data.get("status") == "failed":
                progress.complete(
                    index, done,
                    f"Cross-section review: {done}/{total} groups processed; group {index} "
                    "remains incomplete. Other groups will continue; saved content is preserved.",
                )
                return
            progress.complete(
                index,
                done,
                f"跨章节检查：已完成 {done}/{total} 组；第 {index} 组"
                + (f"发现 {count} 项疑点，尚未确认" if count else "未发现疑点")
                + f"；{call_summary(data['calls'])}，耗时 {data['elapsed_ms'] / 1000:.1f} 秒。",
            )

        with review_calls() as calls:
            results = await parallel_reviews(
                list(enumerate(groups, 1)), limit, review_batch, completed
            )
        findings = [finding for result in results for finding in result]
        model_candidates = len(findings)
        self.archive_context(
            "review_results",
            {
                "plan_ref": plan_ref,
                "groups": [[finding.model_dump() for finding in result] for result in results],
            },
        )
        view = self.shared_state(states)
        from folio.orchestration.rule_conflicts import conflict_candidates, wide_fact_candidates

        wide_keys, wide_questions = wide_fact_candidates(states, self.limits.review_batch_size)
        findings.extend(conflict_candidates(view, exclude_keys=wide_keys))
        findings.extend(wide_questions)
        candidates = deduplicate_review_findings(findings)
        unique = {f.fingerprint: f for f in candidates.findings}
        if candidates.audit:
            self.archive_context("review_deduplication", candidates.audit)
        dismissed = self._dismissed()
        remaining = [
            f for f in unique.values() if self._finding_version(f, states) not in dismissed
        ]
        incomplete = sum(unit.get("status") == "failed" for unit in progress.units.values())
        if incomplete:
            progress.emit(
                f"Cross-section review has {incomplete} incomplete groups out of {len(groups)}. "
                "These groups remain unverified and are included in the pending items.",
                status="summary", step="incomplete", incomplete=incomplete,
            )
        progress.emit(
            f"本轮跨章节检查结束：{len(groups)} 组，{call_summary(calls.snapshot())}；"
            f"模型提出 {model_candidates} 项疑点，规则比对提出 {len(findings) - model_candidates} 项；"
            f"去重并排除当前版本已精查不成立的疑点后，剩余 {len(remaining)} 项。",
            status="summary",
            step="completed",
            completed=len(groups),
            calls=calls.snapshot(),
            elapsed_ms=round((monotonic() - started) * 1000),
            model_candidates=model_candidates,
            rule_candidates=len(findings) - model_candidates,
            duplicate_candidates=len(findings) - len(unique),
            dismissed_candidates=len(unique) - len(remaining),
            findings=[finding_detail(f) for f in remaining],
        )
        return remaining

    async def adjudicate_findings(self, findings, states, *, hard_keys=frozenset()):
        progress = ReviewProgress(self, "drill_down", states, len(findings))
        started = monotonic()
        progress.emit(
            f"开始精查 {len(findings)} 项疑点，并发上限 {self.limits.max_parallel_reviews}；"
            "读取相关正文和原始资料，核对适用条件，正文暂不修改。",
            status="summary",
            step="started",
        )

        async def examine(item):
            index, finding = item
            try:
                with progress.unit(
                    index,
                    finding.section_ids,
                    f"正在精查第 {index}/{len(findings)} 项：{finding.description[:120]}"
                    + ("…" if len(finding.description) > 120 else ""),
                    findings=[finding_detail(finding)],
                    reasons=['Compare related body text with original sources to verify the concern and whether the same conditions apply'],
                ) as data:
                    result = await self.call(
                        "drill_down",
                        {
                            "finding": finding.model_dump(),
                            "business_contexts": [
                                self.business_context(t)
                                for t in self.work_plan.tasks
                                if set(t.section_ids).intersection(finding.section_ids)
                            ],
                            "sections": [
                                s.model_dump()
                                for s in states
                                if s.section_id in finding.section_ids
                            ],
                            "definitions": [
                                s.model_dump()
                                for s in self.plan.sections
                                if s.section_id in finding.section_ids
                            ],
                        },
                        DrillDown,
                    )
                    if result.needs_confirmation:
                        outcome = "needs_confirmation"
                    elif not result.confirmed and finding.fingerprint in hard_keys:
                        outcome = "hard_check_retained"
                    else:
                        outcome = "confirmed" if result.confirmed else "dismissed"
                    data.update(
                        outcome=outcome,
                        reason=result.reason,
                        model_verdict={
                            "confirmed": result.confirmed,
                            "needs_confirmation": result.needs_confirmation,
                            "affected_section_ids": result.affected_section_ids,
                            "repair_instruction": result.repair_instruction,
                        },
                    )
                return finding, result, None
            except (ValueError, ModelCallFailed, TimeoutError, TaskRetriesExhausted) as exc:
                return finding, None, type(exc).__name__

        labels = {
            "confirmed": "模型判定疑点成立，等待处理",
            "dismissed": "模型判定疑点不成立，正文保持原样",
            "needs_confirmation": "无法确定，保留待确认",
            "hard_check_retained": "模型认为疑点不成立，但结构或来源校验仍不通过，保留待确认",
        }

        def completed(done, total, index):
            data = progress.units[index]
            progress.complete(
                index,
                done,
                f"问题精查：已完成 {done}/{total} 项；第 {index} 项{labels[data['outcome']]}；"
                f"{call_summary(data['calls'])}，耗时 {data['elapsed_ms'] / 1000:.1f} 秒。",
            )

        with review_calls() as calls:
            results = await parallel_reviews(
                list(enumerate(findings, 1)), self.limits.max_parallel_reviews, examine, completed
            )
        counts = {
            outcome: sum(unit["outcome"] == outcome for unit in progress.units.values())
            for outcome in labels
        }
        progress.emit(
            f"本轮精查结束：模型判定成立 {counts['confirmed']} 项，"
            f"不成立 {counts['dismissed']} 项，"
            f"待确认 {counts['needs_confirmation'] + counts['hard_check_retained']} 项；"
            f"{call_summary(calls.snapshot())}。精查本身未修改正文。",
            status="summary",
            step="completed",
            completed=len(findings),
            outcomes=counts,
            calls=calls.snapshot(),
            elapsed_ms=round((monotonic() - started) * 1000),
        )
        return results

    @staticmethod
    def check_findings(findings: list[Finding], section_ids: set[str]) -> None:
        for finding in findings:
            if not set(finding.section_ids).issubset(section_ids):
                raise ValueError("Reviewer referenced a section outside its input")
            if any(ref.section_id not in finding.section_ids for ref in finding.block_refs):
                raise ValueError("Finding block reference is outside its affected sections")

    def _finding_version(self, finding, states) -> str:
        return finding_version(
            finding,
            states,
            getattr(self, "work_plan", None),
            self.store.work_states(self.job_id) if hasattr(self, "work_plan") else {},
        )

    def _dismissed(self) -> dict:
        return self.issue_ledger().dismissed()

    def _dismiss(self, finding, states, reason):
        self.issue_ledger().dismiss(self._finding_version(finding, states), reason)

    async def finish(
        self,
        states: list[SectionState],
        *,
        draft: bool,
        needs_confirmation=False,
        allow_model_calls=True,
    ):
        # Model suggestions are complete prose but not approved project facts.
        draft = draft or any(b.origin == "model_proposal" for s in states for b in s.content.blocks)
        folder = self.store.root / "jobs" / self.job_id
        from folio.document.pagination import paginate
        from folio.orchestration.formatting import format_document
        from folio.visuals.agent import VisualAgent

        # Old page scores describe a previous delivery, not a requirement for
        # this export. Keep their on-disk reports, but retire only their active
        # blockers; business review and explicit draft decisions still apply.
        snapshot = self.store.get(self.job_id)["snapshot"] or {}
        previous_issues = snapshot.get("blocking_issues", [])
        issues = [
            item for item in previous_issues if not item["key"].startswith("page-layout:")
        ]
        if "page_review" in snapshot or len(issues) != len(previous_issues):
            snapshot.pop("page_review", None)
            if len(issues) != len(previous_issues):
                snapshot["blocking_issues"] = issues
                snapshot["review_passed"] = (
                    not issues
                    and not snapshot.get("review_interruption")
                    and snapshot.get("state_digest")
                    == digest("".join(state.model_dump_json() for state in states))
                )
            self.store.update(self.job_id, snapshot=snapshot)

        async def exhausted_call(*args, **kwargs):
            raise ModelCallBudgetExceeded(self.limits.max_calls, self.limits.max_calls)

        if any(s.content.visuals for s in states):
            self.store.update(self.job_id, stage="illustrating")
        prefetch = getattr(self, "_visual_prefetch", None)
        if prefetch:
            await prefetch.settle(states, allow_model_calls=allow_model_calls)
        figures = await VisualAgent(
            folder,
            self._call if allow_model_calls else exhausted_call,
            lambda message: self.store.work_event(self.job_id, message),
            self.measure_request if allow_model_calls else None,
            session=prefetch.session if prefetch else None,
        ).build(self.plan, states)
        visual_issues = getattr(figures, "issues", [])
        if visual_issues:
            draft = needs_confirmation = True
            snapshot = self.store.get(self.job_id)["snapshot"] or {}
            issues = [
                Finding(
                    kind="pending",
                    key="visual:" + item["figure_id"],
                    section_ids=[item["section_id"]],
                    description=(
                        f"配图“{item['caption']}”未完成校验，正文已保留，需确认后补图：{item['reason']}"
                    )[:3500],
                )
                for item in visual_issues
            ]
            existing = [Finding.model_validate(f) for f in snapshot.get("blocking_issues", [])]
            snapshot["blocking_issues"] = [
                f.model_dump() for f in {f.fingerprint: f for f in existing + issues}.values()
            ]
            snapshot["review_passed"] = False
            snapshot["visual_issues"] = visual_issues
            self.store.update(self.job_id, snapshot=snapshot)
            self.store.work_event(
                self.job_id,
                f"有 {len(visual_issues)} 张配图待确认，已在文档标明缺图位置；继续导出正文草稿。",
            )
        self.store.update(self.job_id, stage="rendering")
        target = folder / ("draft.docx" if draft else "document.docx")
        temporary = target.with_suffix(".tmp.docx")
        render_options = dict(
            title=self.plan.document_title or self.request.title,
            draft=draft,
            demo=self.request.runtime == "demo",
            cover=self.references.cover_values() | self.request.cover,
            comments=self.references.comments(),
            include_evidence_appendix=self.request.include_evidence_appendix,
            figures=figures,
            defer_pagination=True,
            writing_profile=getattr(self, "writing_profile", None),
            language=self.request.language,
        )
        layout = await _run_local_export(
            render,
            temporary,
            self.plan,
            states,
            **render_options,
        )
        original_layout = temporary.read_bytes()
        self.store.update(self.job_id, stage="formatting")
        try:
            formatting = await format_document(
                temporary,
                self.plan,
                states,
                self.work_plan,
                folder,
                self._call,
                lambda message: self.store.work_event(self.job_id, message),
                self.measure_request,
                enabled=self.request.runtime != "demo" and allow_model_calls,
                disabled_reason="offline_demo"
                if self.request.runtime == "demo"
                else "model_calls_disabled",
            )
        except Exception as exc:
            temporary.write_bytes(original_layout)
            formatting = {"status": "failed", "changed": False, "error_type": type(exc).__name__}
            self.store.work_event(
                self.job_id, f"排版优化未完成，继续输出原始排版；原因：{type(exc).__name__}。"
            )
        self.store.update(self.job_id, stage="rendering")
        if layout["status"] == "deferred":
            try:
                layout = await _run_local_export(paginate, temporary)
            except Exception:
                if not formatting.get("changed"):
                    raise
                # Cosmetic changes must not make an otherwise deliverable document fail.
                temporary.write_bytes(original_layout)
                formatting.update(status="discarded", changed=False, reason="pagination_failed")
                (folder / "formatting.json").write_text(
                    json.dumps(formatting, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self.store.work_event(
                    self.job_id, "调整后的排版未通过分页，已恢复初始排版并继续输出。"
                )
                layout = await _run_local_export(paginate, temporary)
        layout["formatting"] = {key: formatting[key] for key in ("status", "changed")}
        (folder / "layout.json").write_text(
            json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.plan.template is not None:
            from folio.document.template_layout import validate_template_output

            # Cosmetic/Office transformations run after initial rendering. The
            # actual delivered bytes must still honor the frozen template.
            await _run_local_export(validate_template_output, temporary, self.plan)
        temporary.replace(target)
        (folder / "work-plan.json").write_text(
            self.work_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (folder / "work-records.json").write_text(
            json.dumps(
                {k: v.model_dump() for k, v in self.store.work_states(self.job_id).items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (folder / "trace.json").write_text(
            json.dumps([s.model_dump() for s in states], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if draft:
            self.store.update(
                self.job_id,
                status="needs_confirmation" if needs_confirmation else "draft",
                stage="complete",
                artifact=target,
            )
        else:
            self.store.publish(self.job_id, target)
