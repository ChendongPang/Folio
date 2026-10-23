"""Conservative duplicate handling; every distinct issue remains reviewable."""

from dataclasses import dataclass, field


@dataclass
class CandidateDeduplication:
    findings: list = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)


def deduplicate_review_findings(findings):
    """Deduplicate identical descriptions at identical locations, never by topic alone."""
    result = CandidateDeduplication()
    identities, subject_keys = {}, {}
    for original in findings:
        identity = original.issue_identity
        if identity in identities:
            result.audit.append(
                {
                    "action": "duplicate",
                    "original": original.model_dump(),
                    "retained_fingerprint": identities[identity].fingerprint,
                    "reason": 'Identical type, applicable location, citation set, and normalized description',
                }
            )
            continue
        subject_key = (*identity[:6], *identity[7:9], original.key)
        if subject_key in subject_keys:
            # The identity is already distinct before reaching this list. Keep
            # the source key intact and record reuse only as an audit event.
            result.audit.append(
                {
                    "action": "distinct_issue",
                    "original": original.model_dump(),
                    "retained_fingerprint": original.fingerprint,
                    "other_fingerprint": subject_keys[subject_key].fingerprint,
                    "reason": 'The same issue key refers to different content; retained as separate concerns',
                }
            )
        identities[identity] = original
        subject_keys[subject_key] = original
        result.findings.append(original)
    return result
