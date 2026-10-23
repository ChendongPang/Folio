"""Research-backed planning for jobs that do not already have a reviewed plan."""

import json
from uuid import uuid4

from folio.document.planner import build_plan, planning_context
from folio.document.research_context import planning_research_context
from folio.document.research_review import (
    MAX_REVIEW_CALLS,
    DirectoryResearchReview,
    review_directory_research,
)
from folio.models import PlanProposal
from folio.research.planning import PlanningResearchBatch, research_for_directory
from folio.research.planning_handoff import freeze_directory_research
from folio.research.workspace import WorkspaceManager
from folio.runtime.base import RuntimeResult, prompt_chars
from folio.runtime.call_budget import CallBudgetPolicy, call_budget_scope
from folio.runtime.errors import ModelCallBudgetExceeded


class _ResearchModelFailure(RuntimeError):
    """Carry an exhausted Engine request past the research tool-error handler."""

    def __init__(self, error):
        self.error = error
        super().__init__('Outline research model requests have exhausted their bounded retries')


class JobPlanningRuntime:
    """Adapt the directory tools to the durable Engine.call accounting path."""

    models = {
        "directory_research": PlanningResearchBatch,
        "review_directory_research": DirectoryResearchReview,
    }

    def __init__(self, engine):
        self.engine = engine

    @property
    def config(self):
        # Directory tool availability must match the runtime used by CLI/API
        # jobs as well as the interactive workspace. Never include it in prompts.
        return getattr(self.engine.runtime, "config", {})

    def request(self, task, context):
        model = self.models[task]
        context = self.engine.request_context(task, context)
        return context, self.engine.request_schema(task, context, model)

    def prompt_chars(self, task, context, schema):
        context, schema = self.request(task, context)
        return prompt_chars(self.engine.runtime, task, context, schema)

    def fits_prompt_budget(self, task, context, schema, char_limit):
        context, schema = self.request(task, context)
        fits = getattr(self.engine.runtime, "fits_prompt_budget", None)
        if fits is not None:
            return fits(task, context, schema, char_limit)
        return prompt_chars(self.engine.runtime, task, context, schema) <= char_limit

    def request_parts(self, task, context, schema):
        """Audit the same context/schema the durable Engine sends to its runtime."""
        context, schema = self.request(task, context)
        builder = getattr(self.engine.runtime, "request_parts", None)
        if builder is not None:
            return builder(task, context, schema)
        from folio.research.transport import research_messages

        return research_messages(context, schema, task=task), schema, {}, None, {}

    async def measure(self, task, context, schema, char_limit):
        # Use the same model/provider measurement and validated-cache path as
        # Engine.call before choosing a smaller source-summary checkpoint.
        return await self.engine.request_capacity(task, context, self.models[task])

    async def complete(self, task, context, schema, session_key):
        engine = self.engine
        actual_context, actual_schema = self.request(task, context)
        key = engine.request_cache_key(task, actual_context, actual_schema)
        cached = engine.store.task(engine.job_id, key)
        maximum = 36 if task == "directory_research" else (
            MAX_REVIEW_CALLS * min(engine.limits.call_attempts, engine.limits.max_calls)
        )
        # Includes Engine's provider retries and survives job recovery. A new
        # planning folder does not grant another research/model allowance.
        policy = CallBudgetPolicy(bucket=task, max_calls=min(maximum, engine.limits.max_calls),
                                  purpose="research")
        try:
            with call_budget_scope(policy):
                parsed = await engine.call(task, context, self.models[task])
        except Exception as exc:
            # Engine.call has already retried and persisted model/protocol
            # failures. The source-research loop may repair tool arguments, but
            # must not treat this exhausted model request as another tool error.
            if task == "directory_research":
                raise _ResearchModelFailure(exc) from exc
            raise
        saved = engine.store.task(engine.job_id, key)
        usage = json.loads(saved["usage"] or "{}") if saved else {}
        if cached and cached.get("result"):
            usage = {"cached": True, "original_usage": usage}
        if task == "directory_research":
            # The outer source loop executes the real call, including on a
            # cached Engine response. Never turn it back into reply-text JSON.
            return RuntimeResult(parsed.assistant_content, usage=usage,
                                 tool_calls=[step.tool_call for step in parsed.steps],
                                 reasoning_content=parsed.reasoning_content)
        return RuntimeResult(parsed.model_dump_json(), usage=usage)


async def plan_job_from_sources(engine):
    """Keep unresearched/partial source plans out of the writing phase."""
    from folio.orchestration.engine import _run_local_export
    from folio.research.workflow import source_workspace
    from folio.source_documents import source_catalog

    if not engine.request.user_request.strip():
        raise ValueError('Writing requirements are missing. Create a new task with explicit requirements based on the original sources.')
    sources = await _run_local_export(source_workspace, engine)
    catalog = await _run_local_export(
        source_catalog, sources, cache_root=engine.store.root / "source-cache", include_content=False,
    )
    context = planning_context(engine.request.user_request, catalog)
    if engine.request.language:
        context["document_language"] = engine.request.language
    if getattr(engine.request, "ui_language", None):
        context["ui_language"] = engine.request.ui_language
    if engine.request.requirements is not None:
        context["requirements_revision"] = engine.request.requirements.revision
    if not catalog:
        # No local files means no claim of local research. Explicit remote
        # providers keep their established retrieval behavior during writing.
        proposal = await engine.call("plan", context, PlanProposal)
        return build_plan(engine.request.document_id, proposal)

    root = engine.store.root / "jobs" / engine.job_id
    folder = root / "directory-planning" / uuid4().hex
    folder.mkdir(parents=True)
    engine.store.audit_event(engine.job_id, "directory_research_started", {
        "record_path": str(folder.relative_to(root)),
    })
    runtime = JobPlanningRuntime(engine)
    try:
        research = await research_for_directory(
            runtime, sources, folder / "research", engine.request.user_request,
            cache_root=engine.store.root / "source-cache", limits=engine.limits,
            output_language=getattr(engine.request, "ui_language", None),
            on_progress=lambda text: engine.store.work_event(engine.job_id, text),
        )
    except _ResearchModelFailure as exc:
        # Preserve the public error type and its Engine diagnostics after the
        # research loop has saved its interrupted/partial receipt.
        raise exc.error from None
    if research["status"] != "ready":
        used = engine.store.get(engine.job_id)["calls"]
        if used >= engine.limits.max_calls:
            raise ModelCallBudgetExceeded(used, engine.limits.max_calls)
        raise ValueError('Source research for the outline is incomplete; body generation cannot begin: ' + "；".join(
            research.get("blocking_gaps", []) or [research.get("summary", 'Source coverage still has gaps')]
        ))
    context["directory_research"] = planning_research_context(research)
    WorkspaceManager._atomic_json(folder / "planning-input.json", context)
    # Review is a regular Engine task, so its batches must fit the same ordinary
    # request capacity rather than the larger whole-directory planning budget.
    review_limits = engine.limits.model_copy(update={
        "max_planning_prompt_chars": min(engine.limits.max_prompt_chars,
                                         engine.limits.max_planning_prompt_chars),
    })
    original = context
    for attempt in range(min(engine.limits.call_attempts, engine.limits.max_calls)):
        proposal = await engine.call("plan", context, PlanProposal)
        plan = build_plan(engine.request.document_id, proposal)
        WorkspaceManager._atomic_json(folder / "planning-proposal.json", proposal.model_dump())
        WorkspaceManager._atomic_json(folder / "document-plan.json", plan.model_dump(mode="json"))
        review = await review_directory_research(
            runtime, proposal, research, requirements=engine.request.user_request,
            limits=review_limits, session_key=f"job:{engine.job_id}:directory:{attempt + 1}",
            output=folder / f"research-review-attempt-{attempt + 1}.json",
        )
        WorkspaceManager._atomic_json(folder / "research-review.json", review)
        if review["status"] == "passed":
            await _run_local_export(
                freeze_directory_research, folder, root / "inputs", sources, plan,
                cache_root=engine.store.root / "source-cache",
            )
            return plan
        if not review.get("retryable"):
            raise ValueError('Outline source coverage review is incomplete; body generation cannot begin: ' + "；".join(review["issues"]))
        context = original | {
            "rejected_proposal": proposal.model_dump(),
            "format_feedback": 'The outline does not yet cover the researched requirements. Add the corresponding sections or writing instructions:\n'
                               + "\n".join(review["issues"]),
        }
    raise ValueError('The outline does not yet cover source requirements; body generation cannot begin: ' + "；".join(review["issues"]))
