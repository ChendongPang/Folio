"""Recognize a drafted supplement without re-editing identical body content."""

from collections.abc import Sequence

from folio.models import SectionContent, digest
from folio.orchestration.work import GapProposal


def reuse_exact_written_proposals(
    content: SectionContent,
    proposals: Sequence[GapProposal],
    section_id: str,
    *,
    completion_allowed: bool,
) -> tuple[SectionContent, list[dict[str, str]]]:
    """Remove only a repeated writing gap whose complete proposal is already present.

    This is a writing-progress check, not evidence confirmation or review. The
    caller retains the task's missing information and proposals for those checks.
    No whitespace, synonyms, split paragraphs, or provenance are normalized here.
    """
    if not completion_allowed:
        return content, []
    candidates = {
        proposal.gap_id: proposal
        for proposal in proposals
        if proposal.disposition == "body" and section_id in proposal.section_ids
    }
    written = {
        block.text: block.block_id
        for block in content.blocks
        if block.kind == "paragraph" and block.origin == "model_proposal" and not block.evidence_ids
    }
    pending, matches = [], []
    for question in content.pending_items:
        proposal = candidates.get(digest(question))
        block_id = written.get(proposal.content) if proposal else None
        if block_id is None:
            pending.append(question)
            continue
        matches.append(
            {
                "gap_id": proposal.gap_id,
                "section_id": section_id,
                "block_id": block_id,
                "pending_item": question,
            }
        )
    if not matches:
        return content, []
    return content.model_copy(update={"pending_items": pending}), matches
