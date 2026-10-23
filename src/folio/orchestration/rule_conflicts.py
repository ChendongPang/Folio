"""Combine a small same-key comparison without dropping any original record."""

import json
from collections import defaultdict

from folio.models import Finding, digest


def _candidate(key, records):
    records = sorted(
        records,
        key=lambda record: tuple(record[field] for field in ("section_id", "claim_id", "value", "statement")),
    )
    identity = sorted((r["section_id"], r["claim_id"], r["value"]) for r in records)
    stable_key = key if len(key) <= 300 else "fact-key:" + digest(key)
    refs = list(dict.fromkeys((r["section_id"], b) for r in records for b in r["block_ids"]))
    sources = list(dict.fromkeys(e for r in records for e in r.get("evidence_ids", [])))
    details = [{k: r[k] for k in ("section_id", "claim_id", "statement", "value")} for r in records]
    instruction = (
        "Different values under the same fact key do not yet prove a conflict. Check every record's actual subject, applicability conditions and mutual exclusivity. An extracted single marker is not proof of conflict; different subjects and coexisting values are not conflicting."
    )
    description = instruction + json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    if len(description) > 3500:
        # Large statements remain in the targeted original blocks; do not cut
        # their qualifiers merely to fit the candidate's short description.
        description = instruction + 'Compare the located full body text with original evidence and check fact key: ' + stable_key
    return Finding(
        kind="conflict",
        key=stable_key,
        # Several claims can share a paragraph and its citations. Preserve the
        # actual records being compared across fingerprint-based deduplication.
        comparison_scope="fact-records:" + digest(json.dumps(identity, ensure_ascii=False)),
        description=description,
        section_ids=sorted({r["section_id"] for r in records}),
        block_refs=[{"section_id": sid, "block_id": bid} for sid, bid in refs]
        if len(refs) <= 12
        else [],
        evidence_ids=sources if len(sources) <= 12 else [],
    )


def conflict_candidates(view, *, exclude_keys=()):
    result = []
    for key in view["disputed_keys"]:
        if key in exclude_keys:
            continue
        records = [r for r in view["fact_index"][key] if r.get("cardinality") == "single"]
        if len({r["value"] for r in records}) < 2:
            continue
        # Keep the entire scope. The bounded adjudicator must return pending if
        # it cannot examine it; absence of a pairwise call never means "passed".
        result.append(_candidate(key, records))
    return result


def wide_fact_candidates(states, width):
    """Cover distant values even when extraction has not established support.

    These are review questions, never facts or automatically confirmed conflicts.
    Every original record remains reachable through the full section scope.
    """
    groups = defaultdict(list)
    for state in states:
        for claim in state.extraction.claims:
            groups[claim.key].append(claim.model_dump() | {"section_id": state.section_id})
    wide = {
        key: records
        for key, records in groups.items()
        if len({r["value"] for r in records}) > max(2, width)
    }
    return set(wide), [_candidate(key, records) for key, records in sorted(wide.items())]
