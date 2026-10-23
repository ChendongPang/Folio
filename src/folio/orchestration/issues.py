"""One revision-aware view of issues; original findings remain immutable audit data."""

import json
from pathlib import Path

from folio.orchestration.local_checks import finding_version
from folio.orchestration.review_candidates import deduplicate_review_findings


class IssueLedger:
    """Durable adjudications, including historical entries retained for audit.

    Current finding versions carry their identity schema (``v2:``). Historical
    unversioned identities remain readable but must never be upgraded by matching
    only a subject key: their issue descriptions were not recorded in the identity.
    """

    def __init__(self, path: Path):
        self.path = path

    def dismissed(self) -> dict:
        if not self.path.exists():
            return {}
        values = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("Invalid adjudication ledger")
        return values

    def dismiss(self, version: str, reason: str) -> None:
        values = self.dismissed()
        values[version] = reason
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


def effective_findings(
    findings, states, *, dismissed=None, work_plan=None, work_states=None, hard_keys=()
):
    """A waiver applies only to the exact issue and the content/review it examined."""
    dismissed = dismissed or {}
    hard_keys = set(hard_keys)
    return [
        finding
        for finding in deduplicate_review_findings(findings).findings
        if finding.fingerprint in hard_keys
        or finding.key.startswith(("research-coverage:", "research-incomplete:", "research-artifact:"))
        or finding_version(finding, states, work_plan, work_states) not in dismissed
    ]
