"""Carry assigned originals and separate public references from directory research into writing."""

import json
from copy import deepcopy
from pathlib import Path

from folio.models import DocumentPlan, Evidence
from folio.research.workspace import WorkspaceManager


def validated_web_research(value):
    """Keep public reference receipts separate from uploaded requirements."""
    from folio.research.planning_web import validate_web_evidence

    if not isinstance(value, dict):
        raise ValueError('Outline web-reference records must be an object')
    records = value.get("evidence", [])
    if not isinstance(records, list):
        raise ValueError('Outline web-reference evidence must be a list')
    evidence = {}
    for raw in records:
        item = validate_web_evidence(raw)
        if item.evidence_id in evidence:
            raise ValueError('Duplicate outline web-reference evidence')
        evidence[item.evidence_id] = item.model_dump(mode="json")
    searches, findings = value.get("searches", []), value.get("findings", [])
    if not isinstance(searches, list) or not isinstance(findings, list):
        raise ValueError('Outline web-search records and conclusions must be lists')
    for record in searches:
        if (not isinstance(record, dict) or not isinstance(record.get("query"), str)
                or not isinstance(record.get("purpose"), str)
                or not isinstance(record.get("evidence_ids"), list)
                or any(not isinstance(ref, str) or ref not in evidence for ref in record["evidence_ids"])):
            raise ValueError('An outline web-search record cites a nonexistent public source')
    delivered = value.get("delivered_evidence_ids")
    if delivered is not None and (not isinstance(delivered, list)
                                   or any(not isinstance(ref, str) or ref not in evidence for ref in delivered)):
        raise ValueError('Invalid delivery record for an outline web source')
    for finding in findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get("text"), str)
                or not finding["text"].strip() or not isinstance(finding.get("evidence_ids"), list)
                or not finding["evidence_ids"]
                or any(not isinstance(ref, str) or ref not in evidence for ref in finding["evidence_ids"])):
            raise ValueError('Outline web conclusions must cite saved public sources')
        if delivered is not None and not set(finding["evidence_ids"]) <= set(delivered):
            raise ValueError('An outline web conclusion cites a source not yet delivered to the model')
    return deepcopy(value) | {"evidence": list(evidence.values())}


def web_research_navigation(value):
    """Reuse findings and source locations; originals remain readable in the evidence pool."""
    return {key: deepcopy(value[key]) for key in ("available", "searches", "findings") if key in value} | {
        "sources": [{"evidence_id": item["evidence_id"], "url": item["source_id"],
                     "title": item["metadata"].get("title", ""), "retrieved_at": item["retrieved_at"]}
                    for item in value["evidence"]],
        "note": "These are public-web reference findings and prior searches, not uploaded requirements or confirmed project facts. "
        "Their complete saved excerpts are already in the task evidence pool. Read an excerpt using fs_read(scope='evidence', evidence_id=...) "
        "before making a new source note; this reads the saved text without another web request. Reuse previous findings and URLs, "
        "and search again only for a specific unanswered question or a necessary freshness/source check.",
    }


def freeze_directory_research(source_folder, inputs, sources, plan, *, cache_root=None):
    """Freeze and revalidate the receipt; model summaries never become global facts."""
    source_folder, inputs = Path(source_folder), Path(inputs)
    reviewed_plan = DocumentPlan.model_validate_json(
        (source_folder / "document-plan.json").read_text(encoding="utf-8")
    )
    if reviewed_plan.sections != plan.sections:
        raise ValueError('The outline differs from the researched and reviewed version. Generate the outline again')
    report = json.loads((source_folder / "research" / "report.json").read_text(encoding="utf-8"))
    coverage = json.loads((source_folder / "research-review.json").read_text(encoding="utf-8"))
    if report.get("status") not in {"ready", "no_sources"} or coverage.get("status") != "passed":
        raise ValueError('Outline source research or requirement coverage is incomplete; body generation cannot begin')
    destination = inputs / "directory-research"
    workspace = WorkspaceManager(sources, destination / "verification", cache_root=cache_root)
    if report.get("source_snapshot_id") != workspace.source_snapshot_id:
        raise ValueError('Outline research and body-source snapshots differ. Generate the outline again')
    evidence = {
        item.evidence_id: workspace.resolve_evidence(item).model_dump(mode="json")
        for raw in report.get("evidence", [])
        for item in [Evidence.model_validate(raw)]
    }
    requirements = {item["requirement_id"]: item for item in report.get("requirements", [])}
    checks = coverage.get("checks", [])
    if len(checks) != len(requirements) or {item["requirement_id"] for item in checks} != set(requirements):
        raise ValueError('The outline research coverage record is incomplete')
    node_ids = {section.node_id for section in plan.sections}
    for check in checks:
        requirement = requirements[check["requirement_id"]]
        if not requirement["evidence_ids"] or not set(requirement["evidence_ids"]) <= evidence.keys():
            raise ValueError('An outline research requirement cites unverified evidence')
        if check["disposition"] not in {"covered", "not_applicable"}:
            raise ValueError('Outline research still has uncovered requirements')
        if not set(check["node_ids"]) <= node_ids:
            raise ValueError('Outline research references outline nodes from another version')
    if "web_research" in report:
        report["web_research"] = validated_web_research(report["web_research"])
        if evidence.keys() & {item["evidence_id"] for item in report["web_research"]["evidence"]}:
            raise ValueError('Outline web references cannot share evidence identities with uploaded sources')
    WorkspaceManager._atomic_json(destination / "report.json", report | {"evidence": list(evidence.values())})
    WorkspaceManager._atomic_json(destination / "coverage.json", {
        "status": coverage["status"], "checks": checks,
    })


def seed_directory_research(engine, task, work, workspace):
    """Reuse task-assigned originals and the attributed public-reference pool."""
    root = engine.store.root / "jobs" / engine.job_id / "inputs" / "directory-research"
    if not (root / "report.json").is_file():
        return {}
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    if workspace.source_snapshot_id != report["source_snapshot_id"]:
        raise ValueError('The source version for body research differs from the confirmed outline research')
    node_ids = {section.node_id for section in engine.task_sections(task)}
    requirement_ids = {
        item["requirement_id"] for item in coverage["checks"]
        if item["disposition"] == "covered" and set(item["node_ids"]) & node_ids
    }
    requirements = [item for item in report["requirements"] if item["requirement_id"] in requirement_ids]
    references = {reference for item in requirements for reference in item["evidence_ids"]}
    if not references <= {item["evidence_id"] for item in report["evidence"]}:
        raise ValueError('Uploaded-source requirements must cite local original text; web references cannot replace them')
    evidence = {item.evidence_id: item for item in work.evidence}
    for raw in report["evidence"]:
        if raw["evidence_id"] in references:
            item = workspace.resolve_evidence(raw)
            evidence[item.evidence_id] = item
    web_research = None
    if "web_research" in report:
        web_research = validated_web_research(report["web_research"])
        local_ids = {item["evidence_id"] for item in report["evidence"]}
        for raw in web_research["evidence"]:
            item = Evidence.model_validate(raw)
            if item.evidence_id in local_ids:
                raise ValueError('Outline web references cannot replace uploaded sources')
            evidence[item.evidence_id] = item
    work.evidence = list(evidence.values())
    result = {
        "source_snapshot_id": report["source_snapshot_id"],
        "requirements": requirements,
        "evidence_ids": sorted(references),
        "note": "These are same-version originals read during outline research and the writing scope, not verified global facts. Continue reading and verification as needed for this task.",
    }
    if web_research is not None and any(web_research.get(key) for key in ("searches", "evidence", "findings")):
        result["web_research"] = web_research_navigation(web_research)
    return result
