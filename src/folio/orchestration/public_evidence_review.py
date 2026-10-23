"""Locate public-only citations for adjudication, without declaring them invalid.

Public facts and standards can be valid evidence. A public source alone does not
establish that this project adopted a product, process or requirement. These
candidates ask the existing original-material drill-down to distinguish the two;
they are deliberately separate from non-waivable structural/citation checks.
"""

from collections import defaultdict
from collections.abc import Iterable

from folio.models import Finding, FindingBlockRef, SectionState, digest

_MAX_ANCHORS = 12
_DESCRIPTION = (
    "Source applicability requires verification; this is a suspicion, not a confirmed body error. "
    "These blocks are marked evidence-backed, but all cited sources are explicitly public web material "
    "(public_web, project_fact=false). Compare each block with complete originals, full citations and "
    "applicable project sources. Distinguish verifiable public facts, terminology and standards from "
    "claims that this project has adopted a technology, product, process or requirement. Public sources "
    "may support the former: do not reject a statement merely because its source is public. If supported "
    "without claiming project adoption, dismiss the suspicion. Adoption claims require project evidence; "
    "a public description alone is not an approved project requirement. For mixed content, locate only "
    "unsupported claims rather than rejecting the whole section or automatically converting everything "
    "to red proposals. Candidate anchors are for locating content only; preserve and check the target "
    "blocks' complete original text and citations. If still uncertain, keep the issue unresolved."
)


def public_evidence_candidates(states: Iterable[SectionState]) -> list[Finding]:
    """Merge candidates within each section, with at most 12 blocks/source IDs.

    Only explicit, consistent public-source metadata qualifies. Unknown IDs,
    absent metadata, mixed project/public citations and uncited blocks remain
    the responsibility of existing checks. Inputs and source archives are never
    changed. A block with more than 12 citations appears in multiple batches:
    their ID union is complete, and each block anchor still retrieves its full
    body and all its citations during drill-down.
    """
    return [
        candidate
        for state in sorted(states, key=lambda value: value.section_id)
        for candidate in _section_candidates(state)
    ]


def _section_candidates(state: SectionState) -> list[Finding]:
    findings = []
    sources = defaultdict(list)
    for evidence in state.evidence:
        sources[evidence.evidence_id].append(evidence)

    def public_only(evidence_id):
        versions = sources.get(evidence_id)
        return bool(versions) and all(
            evidence.metadata.get("role") == "public_web"
            and evidence.metadata.get("project_fact") is False
            for evidence in versions
        )

    blocks, cited = set(), set()

    def flush():
        if not blocks:
            return
        identity = str([state.section_id, sorted(blocks), sorted(cited)])
        findings.append(
            Finding(
                kind="unsupported",
                rule="other",
                key="public-evidence-scope:" + digest(identity),
                description=_DESCRIPTION,
                section_ids=[state.section_id],
                block_refs=[
                    FindingBlockRef(section_id=state.section_id, block_id=block_id)
                    for block_id in sorted(blocks)
                ],
                evidence_ids=sorted(cited),
            )
        )
        blocks.clear()
        cited.clear()

    for block in sorted(state.content.blocks, key=lambda value: value.block_id):
        refs = sorted(set(block.evidence_ids))
        if (
            block.kind == "heading"
            or block.origin != "evidence"
            or not refs
            or not all(public_only(ref) for ref in refs)
        ):
            continue
        for start in range(0, len(refs), _MAX_ANCHORS):
            batch = refs[start : start + _MAX_ANCHORS]
            if (
                len(blocks | {block.block_id}) > _MAX_ANCHORS
                or len(cited | set(batch)) > _MAX_ANCHORS
            ):
                flush()
            blocks.add(block.block_id)
            cited.update(batch)
    flush()
    return findings
