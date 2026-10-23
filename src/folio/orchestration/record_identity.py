"""Repair only identifier collisions in newly returned research records."""

import json
from collections import Counter
from copy import deepcopy

from folio.models import digest


def normalize_record_ids(value):
    """Fact IDs are local labels, not evidence; preserve every distinct statement.

    Run only before parsing a new understand_work response. Stored records and
    references are never migrated. Malformed facts still reach normal validation.
    """
    facts = value.get("facts") if isinstance(value, dict) else None
    if not isinstance(facts, list) or not facts:
        return value
    for fact in facts:
        if (
            not isinstance(fact, dict)
            or set(fact) != {"fact_id", "statement", "evidence_ids"}
            or not isinstance(fact["fact_id"], str)
            or not fact["fact_id"].strip()
            or not isinstance(fact["statement"], str)
            or not isinstance(fact["evidence_ids"], list)
            or not fact["evidence_ids"]
            or any(not isinstance(eid, str) or not eid for eid in fact["evidence_ids"])
        ):
            return value
    counts = Counter(fact["fact_id"] for fact in facts)
    if max(counts.values()) == 1:
        return value
    result = deepcopy(value)
    kept, seen = [], set()
    used = set(counts)
    for fact in result["facts"]:
        original_id = fact["fact_id"]
        if counts[original_id] == 1:
            kept.append(fact)
            continue
        identity = json.dumps(
            [fact["statement"], sorted(set(fact["evidence_ids"]))], ensure_ascii=False
        )
        # Only exact repeats within the collided ID may be removed.
        if (original_id, identity) in seen:
            continue
        seen.add((original_id, identity))
        replacement = "fact_" + digest(identity)
        candidate, suffix = replacement, 0
        while candidate in used:
            suffix += 1
            candidate = replacement + "_" + str(suffix)
        used.add(candidate)
        fact["fact_id"] = candidate
        kept.append(fact)
    result["facts"] = kept
    return result
