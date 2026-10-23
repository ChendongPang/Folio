"""Carry saved research into planning without treating notes as confirmed facts."""

from __future__ import annotations

from copy import deepcopy


def planning_research_context(research: dict) -> dict:
    """Preserve delivered originals and verified notes even without a finish call."""
    context = {key: deepcopy(research[key]) for key in (
        "status", "summary", "requirements", "unresolved", "blocking_gaps",
        "termination_reason", "failure", "conclusion_submitted", "conclusion_validated",
    ) if key in research}
    evidence = {item["evidence_id"]: item for item in research.get("evidence", [])}
    memory = research.get("research_memory") or {}
    delivered = {key for receipt in research.get("model_deliveries", [])
                 for key in receipt.get("evidence_ids", [])}
    for batch in memory.get("batches", {}).values():
        for key in batch.get("delivered_evidence_ids", []):
            source, reference = evidence.get(key), batch.get("references", {}).get(key)
            if source and reference and all(source.get(field, "") == reference.get(field, "")
                                           for field in ("source_id", "source_version", "locator")):
                delivered.add(key)
    # Older ready receipts predate the delivery ledger; their validated formal
    # requirements remain compatible. New receipts never infer delivery from fetches.
    has_receipts = "model_deliveries" in research or "research_memory" in research
    requirements = context.get("requirements", [])
    if has_receipts:
        requirements = [item for item in requirements
                        if set(item.get("evidence_ids", [])) <= delivered]
        context["requirements"] = requirements
    cited = {key for item in requirements for key in item.get("evidence_ids", [])}
    notes = []
    for note in research.get("working_findings", memory.get("notes", [])):
        refs = note.get("evidence_ids", [])
        if not refs or not set(refs) <= delivered or not set(refs) <= evidence.keys():
            continue
        # The saved reference identity protects against stale notes for a replaced
        # source. A finding without these receipts is not a validated research note.
        references = note.get("references", {})
        if any(key not in references or any(
            evidence[key].get(field, "") != references[key].get(field, "")
            for field in ("source_id", "source_version", "locator")
        ) for key in refs):
            continue
        notes.append({key: deepcopy(note[key]) for key in (
            "note_id", "topic", "text", "kind", "evidence_ids", "unresolved",
        ) if key in note})
        cited.update(refs)
    if research.get("status") not in {"ready", "no_sources"}:
        # An interrupted session may have delivered originals without having had
        # a chance to save their findings. They remain useful draft inputs.
        cited.update(delivered)
    context["working_findings"] = notes
    context["evidence"] = [deepcopy(item) for key, item in evidence.items() if key in cited]
    context["evidence_note"] = (
        "Original text comes from materials actually delivered during this research; working_findings are attributed research notes. "
        "Distinguish source statements, model inferences and items awaiting verification; do not automatically treat them as confirmed facts or formal directory requirements. "
        "If research is incomplete, draft from available notes and originals and identify unresolved scope. "
        "Do not claim that no materials were read merely because formal requirements is empty."
    )
    if "web_research" in research:
        from folio.research.planning_handoff import validated_web_research

        context["web_research"] = validated_web_research(research["web_research"])
        context["web_research_note"] = (
            "Public-web excerpts, findings and URLs are external reference material. Keep them distinct from uploaded "
            "requirements and working_findings; they cannot establish missing internal tender terms, bidder facts or "
            "complete coverage of uploaded originals. Use relevant public information as attributed background, "
            "technical reference or a proposed design, preserving source URLs and retrieval times."
        )
    return context


def research_readiness_issue(research: dict) -> dict | None:
    """Explain an upstream block without asking users to supply files by default."""
    if research.get("status") in {"ready", "no_sources"}:
        return None
    reason = research.get("termination_reason", "")
    failure = research.get("failure") or {}
    if reason == "tool_error":
        return {"code": "tool_error", "message":
                "研究工具执行失败，已有阅读结果和笔记已保留；需要修复或恢复研究后确认目录。"}
    if reason in {"model_error", "model_timeout"}:
        return {"code": "model_error", "message":
                "研究模型请求失败或超时，已有成果已保留；需要恢复研究后确认目录。"}
    validation = set(research.get("validation_issues", []))
    source_gaps = [gap for gap in research.get("blocking_gaps", []) if gap not in validation]
    if (research.get("conclusion_submitted") and research.get("conclusion_validated", True) and source_gaps
            and reason not in {"context_limit", "cancelled"} and not failure):
        return {"code": "source_insufficient", "message":
                "研究结论仍有资料或范围缺口，请查看具体缺口后补充资料或明确范围。"}
    if reason:
        return {"code": "research_incomplete", "message":
                "研究尚未完成，已有阅读结果和笔记已用于目录初稿；需要完成研究后再确认。"}
    if failure:
        return {"code": "research_incomplete", "message":
                "研究执行未完成，已有成果已保留；请查看研究记录中的具体失败原因。"}
    return {"code": "research_incomplete", "message":
            "资料研究结论尚未完成，不能确认目录已覆盖全部来源要求；已有成果已保留。"}


def directory_confirmation_issues(research: dict | None, review: dict | None) -> list[dict]:
    issues = []
    if research is not None:
        issue = research_readiness_issue(research)
        if issue:
            issues.append(issue)
    if review and review.get("status") != "passed":
        kind = review.get("issue_kind", "research_incomplete")
        if not issues or kind in {"directory_coverage", "review_capacity"}:
            messages = review.get("issues") or ["目录覆盖检查尚未完成。"]
            issues.append({"code": kind, "message": "；".join(messages)})
    return issues
