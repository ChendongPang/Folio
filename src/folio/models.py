"""Versioned contracts shared by the workflow, runtime and renderer."""

from __future__ import annotations

import json
import re
import unicodedata
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from folio.document.guidance import WritingGuide
from folio.document.style_contracts import StyleAnalysisConfig
from folio.document.template_contract import TemplateContract, TemplateTable
from folio.issue_identity import canonical_issue_description
from folio.requirements import RequirementSnapshot
from folio.research.contracts import ResearchConfig
from folio.runtime.language import OutputLanguage
from folio.runtime.timeouts import DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()[:24]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(Model):
    max_calls: int = Field(default=2000, ge=1, le=100000)
    call_timeout_seconds: float = Field(default=DEFAULT_MODEL_CALL_TIMEOUT_SECONDS, gt=0)
    run_timeout_seconds: float = Field(default=7200, gt=0)
    max_prompt_chars: int = Field(default=600000, ge=4000)
    max_planning_prompt_chars: int = Field(default=600000, ge=4000)
    max_searches: int = Field(default=3, ge=1, le=10)
    max_repairs: int = Field(default=3, ge=0, le=10)
    call_attempts: int = Field(default=2, ge=1, le=5)
    work_plan_attempts: int = Field(default=3, ge=1, le=5)
    evidence_chars: int = Field(default=600000, ge=1000)
    retrieval_limit: int = Field(default=8, ge=1, le=50)
    review_batch_size: int = Field(default=6, ge=2, le=20)
    max_work_sections: int = Field(default=8, ge=1, le=40)
    max_parallel_tasks: int = Field(default=16, ge=1, le=16)
    max_parallel_reviews: int = Field(default=16, ge=1, le=16)


class PlanItem(Model):
    node_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    level: int = Field(ge=1, le=6)
    instruction: str = Field(default="", max_length=4000)
    content_mode: Literal["generate", "container"] = Field(
        default="generate",
        description="The purpose of future body content, not a request to write it in this step. Every leaf must be generate with writing instructions; container is only for a group with children and no body of its own.",
    )


class PlanProposal(Model):
    document_title: str = Field(
        default="",
        max_length=200,
        description="A concise document title derived from the request and sources; not the full writing prompt.",
    )
    sections: list[PlanItem] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list, max_length=30)


class Section(Model):
    section_id: str
    node_id: str
    title: str
    level: int
    display_number: str
    parent_id: str | None = None
    instruction: str
    heading_path: list[str] = Field(default_factory=list)
    expected_table_headers: list[str] = Field(default_factory=list)
    content_mode: Literal["generate", "container"] = "generate"
    source_heading_paths: list[list[str]] = Field(default_factory=list)
    template_section_id: str | None = None
    required_tables: list[TemplateTable] = Field(default_factory=list)
    allow_subheadings: bool = True


class DocumentPlan(Model):
    document_title: str | None = Field(default=None, max_length=200)
    sections: list[Section]
    notes: list[str] = Field(default_factory=list)
    template: TemplateContract | None = None


class Evidence(Model):
    evidence_id: str
    source_id: str
    source_version: str
    locator: str
    text: str = Field(min_length=1)
    retrieved_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Query(Model):
    queries: list[str] = Field(min_length=1, max_length=6)


class Block(Model):
    origin: Literal["evidence", "model_proposal"] = "evidence"
    block_id: str = Field(min_length=1, max_length=100, description="Body block ID unique within this section")
    kind: Literal["paragraph", "table", "heading"] = "paragraph"
    text: str = Field(default="", max_length=8000)
    heading_level: int = Field(default=4, ge=1, le=6)
    rows: list[list[str]] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # Expose cross-field rules to constrained decoding, not just post-validation.
        # Otherwise a model can satisfy the field schema but put table rows on a paragraph.
        from copy import deepcopy

        base = handler(core_schema)
        prose, table = deepcopy(base), deepcopy(base)
        prose["properties"]["kind"]["enum"] = ["paragraph", "heading"]
        prose["properties"]["text"]["minLength"] = 1
        prose["properties"]["rows"]["maxItems"] = 0
        # New model output must classify provenance explicitly; defaults above
        # remain for loading historical stored blocks.
        prose["required"] = ["block_id", "text", "origin", "evidence_ids"]
        table["properties"]["kind"] = {"type": "string", "const": "table"}
        table["properties"]["rows"]["minItems"] = 1
        table["properties"]["rows"]["items"]["minItems"] = 1
        table["required"] = ["block_id", "kind", "rows", "origin", "evidence_ids"]
        return {"title": "Block", "anyOf": [prose, table]}

    @model_validator(mode="after")
    def check_shape(self):
        if self.kind in {"paragraph", "heading"} and (not self.text.strip() or self.rows):
            raise ValueError("A paragraph must have text and no rows")
        if self.kind == "table":
            if not self.rows or not self.rows[0]:
                raise ValueError("A table must contain cells")
            if any(len(row) != len(self.rows[0]) for row in self.rows):
                raise ValueError("Table rows must have the same width")
            if len(self.rows[0]) > 20 or any(len(c) > 4000 for r in self.rows for c in r):
                raise ValueError("Table exceeds cell budget")
        return self


class VisualRequest(Model):
    figure_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["ui", "flowchart"]
    caption: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=3500)
    source_block_ids: list[str] = Field(min_length=1, max_length=100)
    after_block_id: str = Field(min_length=1, max_length=100)


class RecordOnlyPending(Model):
    """A missing document/contact value at an existing, empty table cell."""

    kind: Literal[
        "person_name", "contact_information", "document_date", "document_version", "document_number"
    ]
    field_name: str = Field(min_length=1, max_length=80)
    block_id: str = Field(min_length=1, max_length=100)
    row_index: int = Field(
        ge=1,
        strict=True,
        description="Row index in the final complete table: header=0, first data row=1. Use these coordinates even when returning only data rows.",
    )
    column_index: int = Field(ge=0, strict=True)
    description: str = Field(min_length=1, max_length=300)


class SectionContent(Model):
    blocks: list[Block] = Field(default_factory=list, max_length=100)
    pending_items: list[str] = Field(default_factory=list, max_length=50)
    record_only_pending: list[RecordOnlyPending] = Field(default_factory=list, max_length=50)
    visuals: list[VisualRequest] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [b.block_id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate block ID")
        figure_ids = [v.figure_id for v in self.visuals]
        if len(figure_ids) != len(set(figure_ids)):
            raise ValueError("Duplicate figure ID")
        sources = {b.block_id for b in self.blocks if b.kind != "heading"}
        for visual in self.visuals:
            references = set(visual.source_block_ids)
            if not references <= set(ids):
                raise ValueError("Figure sources must reference existing blocks")
            if not references & sources:
                raise ValueError("Figure sources must include at least one prose or table block")
            if visual.after_block_id not in ids:
                raise ValueError("Figure insertion point must reference an existing block")
        cells = set()
        blocks = {block.block_id: block for block in self.blocks}
        for pending in self.record_only_pending:
            # One contact cell may lack both a name and contact details. Keep
            # each distinct attribute; only repeat entries of the same kind clash.
            cell = (pending.block_id, pending.row_index, pending.column_index, pending.kind)
            if cell in cells:
                raise ValueError("Duplicate pending metadata cell and kind")
            cells.add(cell)
            block = blocks.get(pending.block_id)
            if (
                block is None
                or block.kind != "table"
                or pending.row_index >= len(block.rows)
                or pending.column_index >= len(block.rows[pending.row_index])
                or block.rows[0][pending.column_index] != pending.field_name
                or block.rows[pending.row_index][pending.column_index].strip()
            ):
                raise ValueError("Pending metadata must identify an existing empty table cell")
        return self


class Claim(Model):
    claim_id: str
    block_ids: list[str] = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=3500)
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=1000)
    scope: str = Field(default="document", max_length=200)
    cardinality: Literal["single", "multiple", "unspecified"] = "unspecified"
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["candidate", "supported"] = "candidate"

    @property
    def key(self) -> str:
        return "|".join(x.casefold().strip() for x in (self.scope, self.subject, self.predicate))


class Extraction(Model):
    summary: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("Duplicate claim ID")
        return self


class FindingBlockRef(Model):
    section_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)


class Finding(Model):
    kind: Literal["unsupported", "conflict", "coverage", "extraction", "pending"]
    description: str = Field(min_length=1, max_length=3500)
    section_ids: list[str] = Field(min_length=1)
    # Reviewer-provided subject key. It alone cannot identify an issue: reviewers
    # can reuse the same key for different concerns, even in the same block.
    key: str = Field(min_length=1, max_length=300)
    basis: Literal["content", "guidance"] = Field(
        default="content",
        description="content=an explicit task requirement or substantive business issue; guidance=an advisory writing-guide suggestion, only for coverage/pending.",
    )
    block_refs: list[FindingBlockRef] = Field(default_factory=list, max_length=12)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    rule: Literal[
        "citation_required", "evidence_conflict", "fabricated_fact", "coverage", "other"
    ] = Field(
        default="other",
        description="citation_required only means a business design lacks sources; use evidence_conflict for contradictions with explicit rules, "
        "and fabricated_fact for invented real regulations, people, dates or approvals. Red proposal styling does not exempt these claims.",
    )

    comparison_scope: str = ""
    incompatibility: str = ""

    @property
    def issue_identity(self) -> tuple:
        """Exact issue identity, independent of batching, order and deduplication."""

        def normalized(text):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

        return (
            self.kind,
            self.rule,
            self.basis,
            tuple(sorted(set(self.section_ids))),
            tuple(sorted({(ref.section_id, ref.block_id) for ref in self.block_refs})),
            tuple(sorted(set(self.evidence_ids))),
            normalized(canonical_issue_description(
                self.description, kind=self.kind, key=self.key, basis=self.basis, rule=self.rule,
            )),
            normalized(self.comparison_scope),
            normalized(self.incompatibility),
            # An unlocated question can refer to a different table row despite
            # having identical wording. In that case keep the reviewer's key.
            self.key if not self.block_refs and not self.comparison_scope else "",
        )

    @property
    def fingerprint(self) -> str:
        # v1 omitted description/basis and could waive unrelated concerns. Never
        # apply a persisted v1 waiver to a v2 issue; retain it only for audit.
        return "v2:" + digest(json.dumps(self.issue_identity, ensure_ascii=False))


class Verdict(Model):
    claim_id: str
    supported: bool
    reason: str = Field(min_length=1, max_length=1000)


class LocalReview(Model):
    verdicts: list[Verdict]
    findings: list[Finding] = Field(default_factory=list)


class Review(Model):
    findings: list[Finding] = Field(default_factory=list)


class SourceApplicabilityCheck(Model):
    section_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    # Quotes use the same character ceiling as long source text; the runtime
    # independently enforces the whole request/response token budgets.
    target_quote: str = Field(min_length=1, max_length=600000)
    target_subject: str = Field(min_length=1, max_length=120)
    target_role: Literal[
        "project_requirement", "project_description", "proposed_design", "source_reference"
    ] = Field(
        description=(
            "Classify the disputed statement's actual subject and use. source_reference includes "
            "objective reporting of third-party products, projects, and market facts, even without "
            "an explicit 'for reference' label. project_requirement/project_description refer only "
            "to requirements or adopted/existing facts of the user's own delivery project; merely "
            "describing a third-party project is not project_description. proposed_design denotes "
            "a proposed solution, not an established adoption. All roles still require source, "
            "subject, and condition checks."
        )
    )
    evidence_id: str = ""
    source_quote: str = Field(default="", max_length=600000)
    source_subject_quote: str = Field(default="", max_length=600000)
    source_role: Literal[
        "project_requirement",
        "project_description",
        "product_capability",
        "test_result",
        "external_reference",
        "other_context",
        "missing",
    ]
    applicability: Literal["direct", "reference_only", "conflict", "unsupported", "uncertain"]
    scope_reason: str = Field(min_length=1, max_length=400)
    adoption_evidence_id: str = ""
    adoption_quote: str = Field(default="", max_length=600000)


class ReviewRepairTarget(Model):
    section_id: str = Field(min_length=1)
    component: Literal["body", "extraction", "visuals"]
    action: Literal["add", "update", "remove"]
    visual_kind: Literal["", "flowchart", "ui"] = ""


class DrillDown(Model):
    confirmed: bool = Field(
        description="Whether the candidate finding is valid: false=the text is correct or the review is a false positive; true=the issue exists. This does not mean confirming that the body is correct."
    )
    reason: str = Field(min_length=1, max_length=1000)
    affected_section_ids: list[str] = Field(default_factory=list, max_length=40)
    repair_instruction: str = Field(default="", max_length=1500)
    needs_confirmation: bool = False
    applicability_checks: list[SourceApplicabilityCheck] = Field(
        default_factory=list, max_length=24
    )
    repair_targets: list[ReviewRepairTarget] = Field(default_factory=list, max_length=40)


class SectionState(Model):
    section_id: str
    revision: int = 1
    content: SectionContent
    extraction: Extraction
    evidence: list[Evidence]
    local_findings: list[Finding] = Field(default_factory=list)
    # Retain individual source verdicts even when another finding blocks the
    # section. Effective facts are derived from these and current dispositions.
    claim_verdicts: list[Verdict] = Field(default_factory=list)
    validation_completed: bool = False


class RagflowConfig(Model):
    url: str = Field(pattern=r"^https?://[^\s<>]+$")
    dataset_ids: list[str] = Field(min_length=1)
    document_ids: list[str] = Field(default_factory=list)
    similarity_threshold: float = Field(default=0.2, ge=0, le=1)
    vector_similarity_weight: float = Field(default=0.3, ge=0, le=1)
    keyword: bool = False
    top_k: int = Field(default=1024, ge=1, le=1024)
    rerank_id: str | None = None
    timeout_seconds: float = Field(default=120, gt=0, le=600)

    @model_validator(mode="after")
    def nonempty_ids(self):
        if any(not value.strip() for value in self.dataset_ids + self.document_ids):
            raise ValueError("Dataset and document IDs cannot be empty")
        return self


class JobInput(Model):
    user_request: str = Field(default="", max_length=600000)
    requirements: RequirementSnapshot | None = None
    writing_guide: WritingGuide | None = None
    title: str = Field(min_length=1, max_length=200)
    document_id: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]*$", max_length=100)
    knowledge_path: str | None = None
    ragflow: RagflowConfig | None = None
    reference_paths: list[str] = Field(
        default_factory=list, description="Explicit project factual sources, not output examples"
    )
    sample_paths: list[str] = Field(
        default_factory=list, description="Output examples used only for writing form and detail"
    )
    style_analysis: StyleAnalysisConfig = Field(default_factory=StyleAnalysisConfig)
    reference_revision_view: Literal["final", "original"] = "final"
    cover: dict[str, str] = Field(default_factory=dict)
    include_evidence_appendix: bool = False
    runtime: Literal["llm", "demo"] = "llm"
    # Persist the document output language separately from the workbench locale.
    # None lets direct API/CLI callers infer it from the original user request.
    language: OutputLanguage | None = None
    # Process labels can follow the interface without changing document content.
    ui_language: OutputLanguage | None = None
    # Absence preserves the execution contract of existing persisted jobs.
    research: ResearchConfig | None = None
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def one_knowledge_source(self):
        if self.requirements is not None:
            self.requirements.ensure_capacity()
            if self.user_request and self.user_request != self.requirements.text:
                raise ValueError("user_request must match the confirmed requirement snapshot")
            self.user_request = self.requirements.text
        if (self.knowledge_path and self.ragflow) or not (
            self.knowledge_path or self.ragflow or self.reference_paths
        ):
            raise ValueError("Provide knowledge_path or ragflow (not both), or reference_paths")
        return self
