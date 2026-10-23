"""Deterministic section checks that model adjudication cannot waive."""

from folio.document.presentation import missing_provenance
from folio.models import Finding, digest
from folio.research.artifacts import derived_evidence_error


def finding_version(finding, states, work_plan=None, work_states=None):
    revisions = sorted(
        (state.section_id, state.revision)
        for state in states
        if state.section_id in finding.section_ids
    )
    work_states = work_states or {}
    work_versions = sorted(
        (task.task_id, work_states[task.task_id].review_key)
        for task in (work_plan.tasks if work_plan else [])
        if task.task_id in work_states and set(task.section_ids).intersection(finding.section_ids)
    )
    return finding.fingerprint + ":" + digest(str([revisions, work_versions]))


def hard_findings(section, content, evidence, extraction, required_sources=()):
    evidence_ids = {e.evidence_id for e in evidence}
    block_ids = {b.block_id for b in content.blocks}
    required_sources = set(required_sources)
    findings = []

    def issue(kind, description, key, *, block_id=None):
        findings.append(
            Finding(
                kind=kind,
                description=description,
                key=key,
                section_ids=[section.section_id],
                block_refs=(
                    [{"section_id": section.section_id, "block_id": block_id}]
                    if block_id is not None
                    else []
                ),
            )
        )

    if not content.blocks:
        issue("coverage", 'Content sections cannot be empty', "empty-section")
    if section.expected_table_headers and not any(
        b.kind == "table" and b.rows[0] == section.expected_table_headers for b in content.blocks
    ):
        issue("coverage", 'The required table and column headings are missing from this section', "required-table")
    # A physical template can have several header rows, but the writing contract
    # has one logical header row. Match distinct generated tables, not repeated
    # claims of coverage by the same block; stricter row requirements go first.
    remaining_tables = [b for b in content.blocks if b.kind == "table"]
    for required in sorted(section.required_tables, key=lambda t: t.min_data_rows, reverse=True):
        candidates = [b for b in remaining_tables if b.rows[0] == required.headers]
        matching = next(
            (
                b for b in candidates
                if sum(any(cell.strip() for cell in row) for row in b.rows[1:]) >= required.min_data_rows
            ), None
        )
        if matching is not None:
            remaining_tables.remove(matching)
            continue
        issue(
            "coverage",
            f"Required standalone template table {required.table_id} is missing. It must have columns {' / '.join(required.headers)} and at least {required.min_data_rows} data rows",
            "required-table:" + required.table_id,
        )
    cited = {eid for b in content.blocks for eid in b.evidence_ids}
    used_evidence = cited | {eid for claim in extraction.claims for eid in claim.evidence_ids}
    for e in evidence:
        if e.evidence_id in used_evidence and (error := derived_evidence_error(e)):
            issue(
                "unsupported",
                'Derived research evidence is unavailable: ' + error,
                "derived-evidence:" + e.evidence_id,
            )
        if e.evidence_id in required_sources and e.evidence_id not in cited:
            issue("coverage", 'Project source not covered: ' + e.locator, e.evidence_id)
    previous_heading_level = section.level
    for block in content.blocks:
        if block.kind == "heading" and not section.allow_subheadings:
            issue(
                "coverage",
                "This section's template directory is fixed; do not add body subheadings. Preserve their content as paragraphs or tables.",
                "template-heading:" + block.block_id,
                block_id=block.block_id,
            )
        if block.kind == "heading" and block.heading_level > previous_heading_level + 1:
            issue("coverage", 'Body subheadings cannot skip levels', block.block_id)
        if block.kind == "heading":
            previous_heading_level = block.heading_level
        if block.kind == "heading" and block.heading_level <= section.level:
            issue("coverage", 'Body subheadings cannot exceed the current section hierarchy', block.block_id)
        if not set(block.evidence_ids).issubset(evidence_ids):
            issue("unsupported", 'The body cites evidence that was not retrieved', block.block_id)
        if missing_provenance(block):
            issue(
                "unsupported",
                "Body text is marked evidence-backed but lacks citations; verify the evidence or treat it as model-proposed content.",
                block.block_id,
                block_id=block.block_id,
            )
    for claim in extraction.claims:
        if not set(claim.block_ids).issubset(block_ids):
            issue("extraction", 'A fact points to a nonexistent body block', claim.claim_id)
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(evidence_ids):
            issue("unsupported", 'A fact lacks valid evidence citations', claim.claim_id)
    for index, pending in enumerate(content.pending_items):
        issue("pending", pending, f"pending_{index}")
    for pending in content.record_only_pending:
        issue(
            "pending",
            'Metadata to fill in: ' + pending.field_name + "。" + pending.description,
            "record-only:" + digest(pending.model_dump_json()),
        )
    return findings
