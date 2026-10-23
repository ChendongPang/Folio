"""Separate optional writing advice from actionable content diagnoses.

This is routing of an explicit model classification, not a second semantic
review. No text matching or model calls are used. A guide cannot dismiss a
conflict, an unsupported statement or a broken extraction index.
"""

from folio.models import Finding, LocalReview, Review


def partition_advisory_findings(
    result: Review | LocalReview, *, enabled: bool
) -> tuple[list[Finding], list[Finding]]:
    """Return actionable findings and optional notes without changing the input.

    Legacy jobs and unclassified findings retain their existing behavior.
    Only coverage/pending suggestions explicitly based solely on the writing
    guide are advisory. Concrete content error categories remain actionable
    even when the model has inconsistently labelled their basis as guidance.
    The caller archives notes separately, outside repair and delivery status.
    """
    actionable = []
    notes = []
    for finding in result.findings:
        if (
            enabled
            and finding.basis == "guidance"
            and finding.kind in {"coverage", "pending"}
        ):
            notes.append(finding)
        else:
            actionable.append(finding)
    return actionable, notes
