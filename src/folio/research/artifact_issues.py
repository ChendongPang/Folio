"""Keep failed research claims open until verified or independently ruled unused."""

from __future__ import annotations

import json
from copy import deepcopy

from pydantic import Field, model_validator

from folio.models import Evidence, Finding, FindingBlockRef, Model, digest
from folio.research.artifacts import ArtifactError, derived_evidence_error

ISSUES_KEY = "unresolved_artifact_issues"
REVIEW_INSTRUCTION = (
    "Independently check whether an unverified research calculation no longer affects delivery. Inspect whether the actual "
    "body still expresses its claim, and compare each original task and confirmed section requirement with other original "
    "evidence used in the body. Set resolved_as_unused=true only if the claim is absent from the body and other valid "
    "original evidence satisfies the task requirements. A failed calculation, the agent saying it is unnecessary, omitted "
    "requirements, or a finish result without gaps is not grounds for closure. If the body still expresses the claim, "
    "leave it unresolved even if plausible. Cite actual body blocks and their original evidence; model proposals, unverified "
    "OCR, and other derived calculations cannot substitute for this verification. Keep uncertain cases unresolved."
)


class UnusedArtifactReview(Model):
    artifact_id: str
    body_version: str
    resolved_as_unused: bool
    claim_present: bool
    task_requirements_met: bool
    alternative_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    block_refs: list[FindingBlockRef] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def closure_requires_positive_basis(self):
        if self.resolved_as_unused and (
            self.claim_present
            or not self.task_requirements_met
            or not self.alternative_evidence_ids
            or not self.block_refs
        ):
            raise ValueError('Closing an unverified calculation requires confirming that its claim is unused and that actual body text and original evidence satisfy the existing requirements')
        return self


def _body_version(states) -> str:
    return digest(json.dumps([
        {"section_id": state.section_id, "revision": state.revision,
         "content": state.content.model_dump(mode="json"),
         "evidence": [e.model_dump(mode="json") for e in state.evidence]}
        for state in sorted(states, key=lambda item: item.section_id)
    ], ensure_ascii=False, sort_keys=True))


def record_artifact_issue(session, artifact_id, claim, reason, evidence_ids=()):
    if not artifact_id or not str(claim).strip() or not str(reason).strip():
        raise ArtifactError('An unverified research issue requires an artifact identity, claim, and reason')
    issues = session.setdefault(ISSUES_KEY, {})
    existing = issues.get(artifact_id)
    if existing and existing["claim"] != claim:
        raise ArtifactError('The same research artifact identity cannot be rewritten to represent a different business claim')
    issue = {
        "artifact_id": artifact_id, "claim": claim, "reason": reason,
        "evidence_ids": list(dict.fromkeys(evidence_ids)), "status": "open",
    }
    if existing and existing.get("resolution"):
        issue["previous_resolution"] = existing["resolution"]
    issues[artifact_id] = issue
    return issue


def unresolved_artifact_issues(session, states=None):
    issues = session.get(ISSUES_KEY, {})
    version = _body_version(states) if states is not None else None
    result = []
    for issue in issues.values():
        if issue.get("status", "open") == "verified":
            try:
                evidence = Evidence.model_validate(issue["resolution"]["evidence"])
                error = derived_evidence_error(evidence)
            except (KeyError, ValueError, TypeError):
                error = 'The verified artifact lacks a verifiable source record'
            if not error:
                continue
            result.append(issue | {"status": "open", "reason": 'A previously verified research artifact is no longer valid: ' + error})
        elif issue.get("status") == "unused":
            if version is None or issue.get("resolution", {}).get("body_version") == version:
                continue
            result.append(issue | {"status": "open", "reason": 'The body or citation version changed. Reconfirm that the unverified claim is still unused. Original issue: ' + issue["reason"]})
        else:
            result.append(issue)
    return result


def resolve_verified_artifact(session, evidence: Evidence):
    derivation = evidence.metadata.get("derivation", {})
    artifact_id = derivation.get("artifact_id")
    issue = session.get(ISSUES_KEY, {}).get(artifact_id)
    if issue is None:
        return False
    if error := derived_evidence_error(evidence):
        raise ArtifactError('An unverified artifact cannot close a research issue: ' + error)
    if not evidence.evidence_id.startswith("derived:"):
        raise ArtifactError('A research issue must be closed by independent verification of the same calculation artifact')
    issue["status"] = "verified"
    issue["resolution"] = {"method": "verified", "evidence": evidence.model_dump(mode="json")}
    return True


def _original_evidence(evidence):
    return (
        not evidence.evidence_id.startswith("derived:")
        and evidence.metadata.get("provenance") != "derived"
        and not evidence.metadata.get("derivation")
        and derived_evidence_error(evidence) is None
    )


def unused_artifact_review_context(issue, task, states, *, sections=()):
    states = [state for state in states if state.section_id in task.section_ids]
    sources = {}
    body = []
    for state in states:
        cited = {eid for block in state.content.blocks for eid in block.evidence_ids}
        for evidence in state.evidence:
            if evidence.evidence_id in cited and _original_evidence(evidence):
                sources[evidence.evidence_id] = evidence.model_dump(mode="json")
        body.append({"section_id": state.section_id, "revision": state.revision,
                     "content": state.content.model_dump(mode="json")})
    return {
        "instruction": REVIEW_INSTRUCTION,
        "issue": {key: issue[key] for key in ("artifact_id", "claim", "reason", "evidence_ids")},
        "work_task": task.model_dump(mode="json"),
        "sections": [section.model_dump(mode="json") for section in sections],
        "body_version": _body_version(states), "actual_body": body,
        "alternative_original_evidence": list(sources.values()),
    }


def resolve_unused_artifact(session, review: UnusedArtifactReview, states, review_key):
    issue = session.get(ISSUES_KEY, {}).get(review.artifact_id)
    if issue is None:
        raise ArtifactError('No matching research issue was found')
    if not review.resolved_as_unused:
        return False
    if not review_key or review.body_version != _body_version(states):
        raise ArtifactError('The independent review of the unverified research issue does not match the current body version')
    alternatives = set(review.alternative_evidence_ids)
    valid = set()
    states_by_id = {state.section_id: state for state in states}
    for reference in review.block_refs:
        state = states_by_id.get(reference.section_id)
        block = next((item for item in state.content.blocks if item.block_id == reference.block_id), None) if state else None
        if block is None or block.origin == "model_proposal":
            raise ArtifactError('The body block cited to close the research issue is missing or contains a model suggestion')
        in_block = {
            evidence.evidence_id for evidence in state.evidence
            if evidence.evidence_id in block.evidence_ids and _original_evidence(evidence)
        }
        if not in_block.intersection(alternatives):
            raise ArtifactError('The body block used to close the research issue does not cite the selected original evidence')
        valid.update(in_block)
    if not alternatives <= valid:
        raise ArtifactError('The current body does not actually cite the alternative evidence used to close the research issue')
    issue["status"] = "unused"
    issue["resolution"] = {"method": "independent_unused_review", "review_key": review_key,
                           "body_version": review.body_version, "review": review.model_dump(mode="json")}
    return True


def findings(session, section_ids, *, states=None):
    return [Finding(
        kind="pending", key="research-artifact:" + issue["artifact_id"],
        description='Unresolved research calculation: ' + issue["claim"][:1200] + "。" + issue["reason"][:700],
        section_ids=list(section_ids),
    ) for issue in unresolved_artifact_issues(session, states)]


async def reconcile_artifact_issues(engine, task, work, states):
    """Review abandoned calculations after real prose exists; persist both views."""
    from folio.research.ledger import ResearchLedger
    from folio.runtime.errors import (
        ModelCallBudgetExceeded,
        ModelCallFailed,
        PlanningContextTooLarge,
        TaskRetriesExhausted,
    )

    states = [state for state in states if state.section_id in task.section_ids]
    before = deepcopy(work.research.get(ISSUES_KEY, {}))
    for issue in unresolved_artifact_issues(work.research, states):
        # Reopen a superseded receipt before review, including when the model is
        # unavailable. The original question must survive another interruption.
        work.research.setdefault(ISSUES_KEY, {})[issue["artifact_id"]] = deepcopy(issue)
        context = unused_artifact_review_context(
            issue, task, states, sections=engine.task_sections(task)
        )
        if not context["alternative_original_evidence"]:
            continue
        try:
            capacity = await engine.request_capacity("review_unused_artifact", context, UnusedArtifactReview)
            if not capacity.get("fits", False):
                continue
            review = await engine.call("review_unused_artifact", context, UnusedArtifactReview)
            cache_context = engine.request_context("review_unused_artifact", context)
            key = engine.request_cache_key(
                "review_unused_artifact", cache_context,
                engine.request_schema("review_unused_artifact", cache_context, UnusedArtifactReview),
            )
            if review.artifact_id != issue["artifact_id"]:
                continue
            resolve_unused_artifact(work.research, review, states, key)
        except (ArtifactError, ModelCallBudgetExceeded, ModelCallFailed, PlanningContextTooLarge, TaskRetriesExhausted):
            continue
    if work.research.get(ISSUES_KEY, {}) != before:
        engine.store.save_work_state(engine.job_id, work)
        round_id = work.research.get("round_id")
        if round_id:
            ledger = ResearchLedger(engine.store.root / "jobs" / engine.job_id / "research" / "ledger.sqlite")
            session = ledger.load_session(task.task_id, round_id)
            session[ISSUES_KEY] = deepcopy(work.research[ISSUES_KEY])
            ledger.save_session(task.task_id, round_id, session)
    return findings(work.research, task.section_ids, states=states)
