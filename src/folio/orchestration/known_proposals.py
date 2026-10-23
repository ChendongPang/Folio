"""Audit exact reuse of known proposals as facts without deciding source support."""

import re
from collections import defaultdict
from collections.abc import Iterable

from folio.models import Finding, FindingBlockRef, SectionState, digest
from folio.orchestration.work import GapProposal

# The observed T+1 requirement is 19 characters; short field labels must still
# not become provenance allegations. All matches are reviewed, never rejected.
_MIN_PASSAGE = 16
_MAX_ANCHORS = 12
_DESCRIPTION = (
    "Provenance of known supplemental content requires verification; this is a suspicion, not a "
    "confirmed body error. The following origin=evidence text exactly overlaps known disposition=body "
    "model proposals for this section. A valid evidence_id proves the citation exists, not that it "
    "supports this design. Use the listed zero-based, end-exclusive character ranges to locate the "
    "claims, then check the full body, named proposals and all original citations for subjects, "
    "conditions and conclusions; ranges do not replace originals. If subsequently acquired original "
    "evidence actually supports the content, including semantic equivalents, dismiss the suspicion "
    "and retain the facts and citations. Overlap alone does not establish an error. If only a proposal "
    "supports the content, separate the proposed content from evidence-backed facts with their "
    "respective provenance. Do not automatically relabel an entire mixed paragraph as a proposal. "
    "If evidence remains insufficient, keep the issue unresolved rather than deciding automatically. "
    "Do not delete necessary business design or change unrelated correct text."
)


def _passages(text):
    if len(text.strip()) < _MIN_PASSAGE:
        return
    yield 0, len(text), text
    for match in re.finditer(r"[^。！？；\n]+[。！？；]?", text):
        raw = match.group()
        passage = raw.strip()
        start = match.start() + len(raw) - len(raw.lstrip())
        if len(passage) >= _MIN_PASSAGE and passage != text:
            yield start, start + len(passage), passage


def _matches(block, proposals, sources):
    text = block.text
    if any(text.strip() == source.text.strip() for source in sources):
        return
    seen = set()
    for start, end, passage in _passages(text):
        # Only complete exact source text provides a directly checkable new
        # basis here. Similar wording still receives semantic review; no valid
        # citation is rejected by this conservative candidate collector.
        if any(passage == source.text.strip() for source in sources):
            continue
        for proposal in proposals:
            offset = proposal.content.find(passage)
            if offset < 0:
                continue
            identity = (proposal.gap_id, start, end)
            if identity not in seen:
                seen.add(identity)
                yield {
                    "block_id": block.block_id,
                    "gap_id": proposal.gap_id,
                    "block_span": [start, end],
                    "proposal_span": [offset, offset + len(passage)],
                }
            break  # One exact original suffices to locate the provenance issue.
        else:
            continue
        if start == 0 and end == len(text):
            return  # The whole paragraph subsumes its component sentences.


def known_proposal_candidates(
    states: Iterable[SectionState], proposals: Iterable[GapProposal]
) -> list[Finding]:
    """Locate whole paragraphs and mixed sentences for existing drill-down.

    Only substantial paragraph text, explicit owning sections, body proposals and
    known nonempty citations qualify. No fuzzy matching, table rewriting, source
    recoloring or removal occurs. These findings are not non-waivable rules.
    """
    by_section = defaultdict(dict)
    for proposal in proposals:
        if proposal.disposition == "body":
            for sid in proposal.section_ids:
                by_section[sid][(proposal.gap_id, proposal.content)] = proposal
    findings = []
    for state in sorted(states, key=lambda value: value.section_id):
        applicable = [p for _, p in sorted(by_section[state.section_id].items())]
        if not applicable:
            continue
        sources = defaultdict(list)
        for source in state.evidence:
            sources[source.evidence_id].append(source)
        anchors, refs, locations = set(), set(), []

        def flush(state=state, anchors=anchors, refs=refs, locations=locations):
            if not locations:
                return
            identity = str([state.section_id, locations, sorted(refs)])
            findings.append(
                Finding(
                    kind="unsupported",
                    rule="other",
                    key="known-proposal-provenance:" + digest(identity),
                    description=_DESCRIPTION + "\n".join(locations),
                    section_ids=[state.section_id],
                    block_refs=[
                        FindingBlockRef(section_id=state.section_id, block_id=bid)
                        for bid in sorted(anchors)
                    ],
                    evidence_ids=sorted(refs),
                )
            )
            anchors.clear()
            refs.clear()
            locations.clear()

        for block in state.content.blocks:
            cited = sorted(set(block.evidence_ids))
            if (
                block.kind != "paragraph"
                or block.origin != "evidence"
                or not cited
                or not all(ref in sources for ref in cited)
            ):
                continue
            originals = [source for ref in cited for source in sources[ref]]
            for match in _matches(block, applicable, originals):
                location = (
                    f"block_id={match['block_id']}; gap_id={match['gap_id']}; "
                    f"block_span={match['block_span']}; proposal_span={match['proposal_span']}"
                )
                for start in range(0, len(cited), _MAX_ANCHORS):
                    batch = cited[start : start + _MAX_ANCHORS]
                    if (
                        len(anchors | {block.block_id}) > _MAX_ANCHORS
                        or len(refs | set(batch)) > _MAX_ANCHORS
                        or len(_DESCRIPTION) + len("\n".join([*locations, location])) > 3500
                    ):
                        flush()
                    anchors.add(block.block_id)
                    refs.update(batch)
                    if location not in locations:
                        locations.append(location)
        flush()
    return findings
