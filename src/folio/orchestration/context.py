"""Operation-specific views. Full sources and diagnostics remain in the store."""

import json
from collections import Counter
from copy import deepcopy
from typing import Literal

from pydantic import Field, field_validator

from folio.decisions.risks import LEGACY_RISK_ORIGINALS_LABEL, RISK_ORIGINALS_LABEL
from folio.models import Model, digest

CONTEXT_TASKS = {
    "understand_work",
    "next_action",
    "query",
    "generate",
    "repair",
    "complete_gaps",
    "extract",
    "validate",
    "review_work",
    "review",
    "drill_down",
}
SELECTION_REFERENCE_ERROR = (
    "Context selection must use unique references of the declared kind from this page"
)


class ContextSelection(Model):
    selected_refs: list[str] = Field(default_factory=list)
    risk_refs: list[str] = Field(default_factory=list)
    risk_summary: str = Field(default="", max_length=1200)

    @field_validator("selected_refs", "risk_refs")
    @classmethod
    def unique_references(cls, values):
        # Selection is a set; repeating a valid reference does not change its meaning.
        # Unknown references and wrong source kinds still fail the engine's check.
        return list(dict.fromkeys(values))


def normalize_selection_references(selection: ContextSelection, catalog) -> None:
    """Recover only a unique inner ID from a source of the permitted kind on this page."""
    outer_refs = {item["context_ref"] for item in catalog}
    identifier_fields = {"evidence": "evidence_id", "fact": "fact_id", "proposal": "gap_id"}
    for field, kinds in (
        ("selected_refs", {"evidence", "fact", "proposal"}),
        ("risk_refs", {"risk"}),
    ):
        aliases = {}
        for item in catalog:
            identifier_field = identifier_fields.get(item["kind"])
            value = item.get("value")
            if item["kind"] not in kinds or not identifier_field or not isinstance(value, dict):
                continue
            identifier = value.get(identifier_field)
            if isinstance(identifier, str) and identifier:
                aliases.setdefault(identifier, set()).add(item["context_ref"])
        normalized = []
        for ref in getattr(selection, field):
            candidates = aliases.get(ref, set())
            if ref not in outer_refs and len(candidates) == 1:
                ref = next(iter(candidates))
            normalized.append(ref)
        setattr(selection, field, list(dict.fromkeys(normalized)))


class GapConstraint(Model):
    source_refs: list[str] = Field(min_length=1)
    source_kind: Literal["evidence", "fact", "proposal", "risk"]
    text: str = Field(min_length=1, max_length=600)


class GapMaterialRead(Model):
    constraints: list[GapConstraint] = Field(default_factory=list)


def gap_material_catalog(context):
    catalog = {}

    def add(kind, value):
        ref = "ctx_" + digest(json.dumps([kind, value], sort_keys=True))
        catalog[ref] = {"context_ref": ref, "kind": kind, "value": value}

    for field, kind in (
        ("evidence", "evidence"),
        ("global_facts", "fact"),
        ("existing_proposals", "proposal"),
    ):
        for value in context.get(field, []):
            add(kind, value)
    record = context.get("business_record", {})
    for value in record.get("facts", []):
        add("fact", value)
    for field in ("conflicts", "open_questions"):
        for value in record.get(field, []):
            add("risk", {field: value})
    for field in ("summary", "terminology"):
        if record.get(field):
            add("fact", {field: record[field]})
    for dep in context.get("dependencies", []):
        for section in dep.get("sections", []):
            for value in section.get("supported_facts", []):
                add("fact", {"section_id": section["section_id"], **value})
        for field, kind in (
            ("model_proposals", "proposal"),
            ("risk_briefs", "risk"),
            ("open_questions", "risk"),
        ):
            for value in dep.get(field, []):
                add(kind, {"task_id": dep["task_id"], field: value})
    return list(catalog.values())


def evidence_view(e):
    # Provenance IDs remain resolvable; ingestion/debug metadata stays in storage.
    result = {k: e[k] for k in ("evidence_id", "text", "source_id", "locator") if k in e}
    if e.get("retrieved_at"):
        result["retrieved_at"] = e["retrieved_at"]
    metadata = e.get("metadata", {})
    result["metadata"] = {
        k: metadata[k]
        for k in (
            "role",
            "document_name",
            "revision_view",
            "heading_path",
            "block_kind",
            "rows",
            "url",
            "title",
            "project_fact",
            "provenance",
            "source_kind",
            "origin_format",
            "requires_extraction_review",
            "human_confirmation",
        )
        if k in metadata
    }
    if isinstance(metadata.get("derivation"), dict):
        result["metadata"]["derivation"] = {
            key: metadata["derivation"][key]
            for key in ("artifact_id", "status") if key in metadata["derivation"]
        }
    if isinstance(metadata.get("extraction_review"), dict):
        from folio.models import Evidence
        from folio.research.extraction_review import reviewed_evidence_error

        try:
            verified = reviewed_evidence_error(Evidence.model_validate(e)) is None
        except ValueError:
            verified = False
        if verified:
            result["metadata"]["requires_extraction_review"] = False
            result["metadata"]["extraction_review"] = {
                "review_id": metadata["extraction_review"]["review_id"], "status": "verified"
            }
    return result


def node_business_view(context, section_id):
    """Task plans locate a writer; only the confirmed node defines its obligations.

    This is a request view, never a mutation of the durable research/review record.
    Shared factual constraints remain available; a group's questions and acceptance
    criteria must not become requirements for every individual chapter.
    """
    result = deepcopy(context)
    if "work_task" in result:
        result["work_task"] = {
            key: value for key, value in result["work_task"].items() if key in {"task_id", "title"}
        }
    if "model_proposals" in result:
        result["model_proposals"] = [
            p for p in result["model_proposals"] if section_id in p.get("section_ids", [])
        ]
    return result


def project(task, source):
    result = deepcopy(source)

    def clean(value):
        if isinstance(value, dict):
            if "evidence_id" in value and "text" in value and "source_id" in value:
                return evidence_view(value)
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    result = clean(result)
    if task in {"generate", "repair"}:
        result = node_business_view(result, result.get("section", {}).get("section_id"))
    if task == "validate" and "business_context" in result:
        shared = node_business_view(
            result["business_context"], result.get("section", {}).get("section_id")
        )
        # Outstanding group-wide research questions are not local coverage rules.
        # Original facts/conflicts and the section's own pending items remain;
        # review_work still sees all group questions and acceptance criteria.
        shared.get("business_record", {}).pop("open_questions", None)
        result["business_context"] = shared
    if task in {"understand_work", "next_action", "query"}:
        result["research_scope"] = {
            "responsibility_source": "sections.instruction",
            "derived_task_is_navigation": True,
            "instruction": "Research each confirmed sections instruction and explicit writing_gap. "
            "Do not omit a section because work_task's summary omitted it, or expand derived questions into other sections' responsibilities. "
            "Facts must be business rules supported by evidence; task assignments, section numbers and source catalogs are not business facts.",
        }
        # Raw paragraphs are needed by the reader, not repeatedly by the router.
        if task != "understand_work":
            result["evidence"] = [
                {"evidence_id": e["evidence_id"], "source_id": e.get("source_id", "")}
                for e in result.get("evidence", [])
            ]
    if task in {"generate", "repair"}:
        sid = result.get("section", {}).get("section_id")
        if "child_sections" in result:
            # Children delimit the parent's responsibility. Keep their complete
            # writing instructions and table/figure obligations, not duplicate
            # directory planning diagnostics already archived with the plan.
            child_fields = {
                "section_id",
                "title",
                "display_number",
                "level",
                "parent_id",
                "heading_path",
                "instruction",
                "content_mode",
                "expected_table_headers",
                "required_tables",
                "template_section_id",
                "allow_subheadings",
                "required_visual_kinds",
            }
            result["child_sections"] = [
                {key: value for key, value in child.items() if key in child_fields}
                for child in result["child_sections"]
            ]
            if result["child_sections"]:
                result["writing_scope"] = {
                    "role": "parent_overview_and_shared_rules",
                    "instruction": "The parent section and child_sections jointly satisfy this section's overall responsibility. "
                    "Write the overview, module relationships, shared rules and parent-specific requirements here. "
                    "Children own their instructed functions, parameters and operational details; do not repeat those details here. "
                    "Apply this division to source selection, gap detection and writing; details owned by children are not gaps in the parent. "
                    "Retain parent-specific constraints, tables and required figures. Writing guidance must not expand these responsibilities.",
                }
        result["model_proposals"] = [
            p for p in result.get("model_proposals", []) if sid in p["section_ids"]
        ]
        # The shared business record carries rules; peer original prose belongs in review.
        result.pop("peer_sections", None)
        result["task_sections"] = [
            {k: s[k] for k in ("section_id", "title", "heading_path") if k in s}
            for s in result.get("task_sections", [])
        ]
    if task == "understand_work":
        # All accumulated sources are read again or partitioned; no duplicate old synthesis.
        result.pop("previous_record", None)
    if task == "review_work" and result.get("review_scope") == "task_consistency":
        # Every saved node already went through original-evidence validation.
        # This pass checks how nodes fit together, not those same citations again.
        # Original sources remain in the archived context and in drill-down.
        result.pop("evidence", None)
    if task in {"extract", "validate"}:
        required = set(result.get("required_project_evidence_ids", []))
        cited = {
            eid
            for b in result.get("content", {}).get("blocks", [])
            for eid in b.get("evidence_ids", [])
        }
        if task == "validate":
            cited.update(
                eid
                for claim in result.get("extraction", {}).get("claims", [])
                for eid in claim.get("evidence_ids", [])
            )
        result["evidence"] = [
            e for e in result.get("evidence", []) if e["evidence_id"] in required | cited
        ]
    if task == "review":
        result["work_tasks"] = [
            {k: work[k] for k in ("task_id", "title", "objective") if k in work}
            for work in result.get("work_tasks", [])
        ]
    if task == "extract":
        result.pop("business_context", None)
    return result


def scope_work_review(context):
    """Keep the current nodes' definitions/proposals beside their original prose.

    Shared business facts, risks and all original evidence remain available.
    Other node summaries only locate peers; their full writing metadata is not
    needed in every node-pair request.
    """
    ids = {s["section_id"] for s in context["sections"]}
    refs = {
        b["proposal_ref"]
        for s in context["sections"]
        for b in s.get("content", {}).get("blocks", [])
        if b.get("proposal_ref")
    }
    result = context.copy()
    if "definitions" in context:
        result["definitions"] = [s for s in context["definitions"] if s["section_id"] in ids]
    if "model_proposals" in context:
        result["model_proposals"] = [
            p
            for p in context["model_proposals"]
            if ids.intersection(p.get("section_ids", [])) or p.get("gap_id") in refs
        ]
    return result


def dependency_research_identity(dependencies):
    """Adding readable drafts must not invalidate existing research checkpoints.

    The original section revisions already identify the saved body. Retain that
    identity (and every fact/risk) when upgrading a session that lacked drafts.
    """
    result = deepcopy(dependencies)
    for dependency in result:
        for section in dependency.get("sections", []):
            for field in ("content", "title", "display_number", "review_status"):
                section.pop(field, None)
    return result


def dependency_evidence_ids(section):
    """Preserve original citations even when a paragraph yielded no fact claim."""
    return {
        eid
        for item in [
            *section.get("supported_facts", []),
            *section.get("content", {}).get("blocks", []),
        ]
        for eid in item.get("evidence_ids", [])
    }


def dependency_catalog(dependencies):
    catalog, objects, views = [], {}, []

    def add(kind, owner, payload):
        ref = "ctx_" + digest(json.dumps([kind, owner, payload], sort_keys=True))
        if ref not in objects:
            catalog.append({"context_ref": ref, "kind": kind, "owner": owner, "value": payload})
            objects[ref] = (kind, owner, payload)

    for dep in dependencies:
        findings = list(dep.get("findings", []))
        view = {
            "task_id": dep["task_id"],
            "title": dep["title"],
            "sections": [],
            "model_proposals": [],
            "open_questions": dep.get("open_questions", []),
            "risk_briefs": [],
        }
        for section in dep.get("sections", []):
            findings += section.get("findings", [])
            view["sections"].append(
                {
                    "section_id": section["section_id"],
                    "revision": section["revision"],
                    "supported_facts": [],
                    # The selector may reduce factual indexes, not discard the
                    # upstream deliverable this dependency was created to use.
                    **{key: deepcopy(section[key]) for key in (
                        "title", "display_number", "review_status", "content"
                    ) if key in section},
                }
            )
            for fact in section.get("supported_facts", []):
                add("fact", dep["task_id"], {"section_id": section["section_id"], **fact})
        for proposal in dep.get("model_proposals", []):
            if proposal.get("disposition", "body") != "record_only":
                add("proposal", dep["task_id"], proposal)
        unique = {digest(json.dumps(f, sort_keys=True)): f for f in findings}
        for finding in unique.values():
            add("risk", dep["task_id"], finding)
        view["quality"] = {
            "unresolved_findings": dict(Counter(f["kind"] for f in unique.values())),
            "source": "stored_review",
            "note": "Review issues are not business facts. Only relevant risks are carried in risk_briefs; the original report remains accessible by reference.",
        }
        views.append(view)
    return catalog, objects, views


def apply_selection(views, objects, selection):
    by_task = {v["task_id"]: v for v in views}
    for ref in selection.selected_refs:
        kind, owner, value = objects[ref]
        if kind == "proposal":
            by_task[owner]["model_proposals"].append(value)
        elif kind == "fact":
            target = next(
                s for s in by_task[owner]["sections"] if s["section_id"] == value["section_id"]
            )
            target["supported_facts"].append({k: v for k, v in value.items() if k != "section_id"})
    if selection.risk_refs:
        owners = {objects[ref][1] for ref in selection.risk_refs}
        for owner in owners:
            owner_refs = [r for r in selection.risk_refs if objects[r][1] == owner]
            # A page-level summary may mix several owners. Reconstruct the risk
            # from the selected originals before attaching it to an owner.
            summary = selection.risk_summary if len(owners) == 1 else RISK_ORIGINALS_LABEL
            by_task[owner]["risk_briefs"].append(
                {
                    "status": "unresolved_risk",
                    "summary": summary,
                    "source_refs": owner_refs,
                    # Selection chooses references; a short label must never replace
                    # the original constraints or require a second LLM to summarize.
                    **(
                        {
                            "originals": [
                                {"source_ref": r, "value": objects[r][2]} for r in owner_refs
                            ]
                        }
                        if summary in {RISK_ORIGINALS_LABEL, LEGACY_RISK_ORIGINALS_LABEL}
                        else {}
                    ),
                }
            )


def deduplicate_dependency_facts(context):
    """A selected dependency fact and its global index entry are the same source.

    Only remove exact duplicates at the same section revision. Conflicting values
    and different versions remain visible; the full snapshots stay in storage.
    """

    def identity(section_id, revision, fact):
        return json.dumps([section_id, revision, fact], ensure_ascii=False, sort_keys=True)

    provided = {
        identity(s["section_id"], s.get("revision"), f)
        for dep in context.get("dependencies", [])
        for s in dep.get("sections", [])
        for f in s.get("supported_facts", [])
    }
    if provided and "global_facts" in context:
        context["global_facts"] = [
            f
            for f in context["global_facts"]
            if identity(
                f.get("section_id"),
                f.get("section_revision"),
                {
                    k: v
                    for k, v in f.items()
                    if k not in {"record_id", "section_id", "section_revision"}
                },
            )
            not in provided
        ]
