"""Derived views are rebuilt from section revisions; never append stale facts."""

from collections import defaultdict

from folio.knowledge.local import terms
from folio.models import DocumentPlan, Section, SectionState
from folio.orchestration.issues import effective_findings
from folio.orchestration.local_checks import hard_findings
from folio.research.artifacts import derived_evidence_error


def registry(
    states: list[SectionState], *, dismissed=None, work_plan=None, work_states=None, plan=None
) -> tuple[dict, dict]:
    """Publish supported, current facts without modifying the saved extraction.

    Dismissal removes a blocker; it never supplies missing evidence or reverses a
    negative claim verdict. Legacy supported claims remain readable on recovery.
    """
    records, by_key = {}, defaultdict(list)
    work_states = work_states or {}
    sections = {section.section_id: section for section in plan.sections} if plan else {}
    for state in states:
        # Recovery may retain obsolete nodes or unrelated stored revisions. The
        # confirmed directory defines the document, including its fact sources.
        if plan is not None and state.section_id not in sections:
            continue
        findings = list(state.local_findings)
        hard = (
            hard_findings(
                sections[state.section_id], state.content, state.evidence, state.extraction
            )
            if state.section_id in sections
            and sections[state.section_id].content_mode != "container"
            else []
        )
        owner = (
            next((t for t in work_plan.tasks if state.section_id in t.section_ids), None)
            if work_plan
            else None
        )
        if work_plan is not None and owner is None:
            continue
        if owner:
            work = work_states.get(owner.task_id)
            if not work or not work.review_key:
                continue
            if (
                work.review_revisions
                and work.review_revisions.get(state.section_id) != state.revision
            ):
                continue
            findings.extend(f for f in work.findings if state.section_id in f.section_ids)
        if effective_findings(
            [*findings, *hard],
            states,
            dismissed=dismissed,
            work_plan=work_plan,
            work_states=work_states,
            hard_keys={f.fingerprint for f in hard},
        ):
            continue
        verdicts = {v.claim_id: v.supported for v in state.claim_verdicts}
        blocks = {block.block_id: block for block in state.content.blocks}
        evidence_ids = {
            e.evidence_id for e in state.evidence if derived_evidence_error(e) is None
        }
        for claim in state.extraction.claims:
            supported = (
                verdicts.get(claim.claim_id) is True
                if state.validation_completed
                else claim.status == "supported" and verdicts.get(claim.claim_id) is not False
            )
            sourced = (
                bool(claim.block_ids)
                and bool(claim.evidence_ids)
                and set(claim.evidence_ids) <= evidence_ids
                and all(
                    bid in blocks and blocks[bid].origin != "model_proposal"
                    for bid in claim.block_ids
                )
            )
            if supported and sourced:
                key = f"{state.section_id}:{claim.claim_id}"
                record = claim.model_dump() | {
                    "status": "supported",
                    "record_id": key,
                    "section_id": state.section_id,
                    "section_revision": state.revision,
                }
                records[key] = record
                by_key[claim.key].append(record)
    return records, dict(by_key)


def hierarchical_state(plan: DocumentPlan, states: list[SectionState], **issue_context) -> dict:
    records, by_key = registry(states, plan=plan, **issue_context)
    sections = {s.section_id: s for s in plan.sections}
    chapters: dict = {}
    for state in states:
        if state.section_id not in sections:
            continue
        root = sections[state.section_id]
        while root.parent_id:
            root = sections[root.parent_id]
        chapter = chapters.setdefault(
            root.section_id, {"sections": [], "claim_refs": [], "pending_items": []}
        )
        chapter["sections"].append(
            {
                "section_id": state.section_id,
                "revision": state.revision,
                "summary": state.extraction.summary,
            }
        )
        chapter["claim_refs"].extend(
            k for k, v in records.items() if v["section_id"] == state.section_id
        )
        chapter["pending_items"].extend(state.content.pending_items)
        chapter["pending_items"].extend(
            'Metadata to fill in: ' + item.field_name + "。" + item.description
            for item in state.content.record_only_pending
        )
    conflicts = {
        key
        for key, values in by_key.items()
        if len({v["value"] for v in values if v.get("cardinality") == "single"}) > 1
    }
    return {
        "chapters": chapters,
        "records": records,
        "fact_index": by_key,
        "disputed_keys": sorted(conflicts),
    }


def relevant_facts(
    section: Section,
    states: list[SectionState],
    max_chars: int = 600000,
    *,
    exclude_sections=(),
    **issue_context,
) -> list[dict]:
    _, groups = registry(states, **issue_context)
    tokens = set(terms(section.title + " " + section.instruction))
    ranked = []
    for records in groups.values():
        if len({r["value"] for r in records if r.get("cardinality") == "single"}) > 1:
            continue  # A disputed conclusion is not a generation constraint.
        for record in records:
            if record["section_id"] in exclude_sections:
                continue
            score = len(tokens & set(terms(record["statement"] + " " + record["subject"])))
            if score:
                ranked.append((score, record))
    result, size = [], 0
    for _, record in sorted(ranked, key=lambda pair: -pair[0]):
        cost = len(str(record))
        if size + cost <= max_chars:
            result.append(record)
            size += cost
    return result
