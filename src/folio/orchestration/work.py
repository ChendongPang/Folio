"""Business work contracts, independent of Word heading depth."""

from typing import Literal

from pydantic import Field, model_validator

from folio.models import DocumentPlan, Evidence, Finding, Model


class WorkTask(Model):
    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=3500)
    section_ids: list[str] = Field(min_length=1)
    questions: list[str] = Field(min_length=1, max_length=20)
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of upstream tasks that block execution until their retrieval, writing and review finish. "
        "Use only when a specific new upstream deliverable is indispensable and cannot be obtained directly from sources; explain each in rationale. "
        "Business sequence, directory order, shared sources and consistency do not create blocking dependencies. Return [] when research can proceed independently.",
    )
    completion_criteria: list[str] = Field(min_length=1, max_length=20)
    rationale: str = Field(
        min_length=1,
        max_length=1000,
        description="Explain the business grouping. For each depends_on entry, identify the upstream task ID, the specific new deliverable required, "
        "and why this task cannot independently obtain the information from uploads or configured supplementary sources.",
    )


class WorkPlan(Model):
    version: int = 1
    tasks: list[WorkTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def graph(self):
        tasks = {t.task_id: t for t in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("Duplicate business task ID")
        owners = set()
        for task in self.tasks:
            if len(set(task.section_ids)) != len(task.section_ids):
                raise ValueError("Duplicate section inside business task")
            if owners.intersection(task.section_ids):
                raise ValueError("Each section must have exactly one writing owner")
            owners.update(task.section_ids)
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError("Duplicate dependency")
            if not set(task.depends_on).issubset(tasks):
                raise ValueError("Unknown business task dependency")
        pending = set(tasks)
        while pending:
            ready = {k for k in pending if not set(tasks[k].depends_on).intersection(pending)}
            if not ready:
                raise ValueError("Business task dependencies contain a cycle")
            pending -= ready
        return self


def validate_work_plan(plan: WorkPlan, document: DocumentPlan) -> None:
    expected = {s.section_id for s in document.sections if s.content_mode != "container"}
    assigned = {sid for t in plan.tasks for sid in t.section_ids}
    if assigned != expected:
        raise ValueError(
            f"WorkPlan must cover every content node exactly once; missing={sorted(expected - assigned)}, "
            f"unknown or container={sorted(assigned - expected)}"
        )


class BusinessFact(Model):
    fact_id: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(min_length=1)


class ResearchAction(Model):
    """An obtainable answer, not every unresolved business question."""

    channel: Literal["local_sources", "public_web"]
    question: str = Field(min_length=1)
    action: str = Field(
        min_length=1,
        description="A concrete, still useful next read, verification or public search. "
        "Identify the source/range to check or the public fact to verify. "
        "Do not repeat completed reads or ask retrieval to supply private information held by the user.",
    )


class BusinessRecord(Model):
    """Working conclusions, never a substitute for original evidence or business approval."""

    summary: str = Field(default="")
    facts: list[BusinessFact] = Field(default_factory=list)
    terminology: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    research_actions: list[ResearchAction] = Field(
        default_factory=list,
        description="Only unanswered questions with a concrete next retrieval action. "
        "local_sources requires available uploaded material that has not yet answered the question; "
        "public_web is for independently verifiable public information. "
        "Unknown bidder identity, quotation, credentials, current infrastructure and project decisions "
        "stay in open_questions, with no research action unless an actual available source can answer them. "
        "Return [] when further retrieval cannot help. This does not resolve or remove open_questions.",
    )

    @model_validator(mode="after")
    def unique_facts(self):
        if len({f.fact_id for f in self.facts}) != len(self.facts):
            raise ValueError("Duplicate business fact ID")
        return self


class GapProposal(Model):
    gap_id: str
    section_ids: list[str] = Field(min_length=1)
    content: str = Field(min_length=20, max_length=5000)
    disposition: Literal["body", "record_only"] = Field(
        default="body",
        description=(
            "body=designable business rules, processes, inputs/outputs and inventories such as reports, assets, interfaces or registers. "
            "content must propose concrete content, not restate the gap. "
            "record_only=unverifiable people, real dates, source versions or external-document existence, "
            "or backend-only writing/retrieval notes that must not enter the body. "
            "A request to supplement a business inventory still belongs to body; words such as supplement, list or complete the document do not change it to record_only. "
            "Never invent actual regulatory sources, existing report identifiers or approvals."
        ),
    )


class GapCompletion(Model):
    proposals: list[GapProposal] = Field(min_length=1)


class WebSearchAttempt(Model):
    query: str = ""
    status: Literal["started", "completed", "failed", "skipped"] = "started"
    evidence_ids: list[str] = Field(default_factory=list)
    error_code: str = ""


class TaskUsage(Model):
    """Durable cost of one business task, including cached and failed calls."""

    task_id: str
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)


class WorkState(Model):
    task_id: str
    context_key: str = ""
    dependency_key: str = ""
    research_instruction: str = ""
    record: BusinessRecord = Field(default_factory=BusinessRecord)
    record_key: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    searches: list[list[str]] = Field(default_factory=list)
    web_searches: list[WebSearchAttempt] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    proposals: list[GapProposal] = Field(default_factory=list)
    # Immutable generation inputs for this task's research/dependency version.
    global_facts: dict[str, list[dict]] = Field(default_factory=dict)
    research: dict = Field(default_factory=dict)
    researched: bool = False
    writable: bool = False
    # Review belongs to exact content revisions AND the current shared record.
    review_key: str = ""
    review_revisions: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    usage: TaskUsage | None = None
