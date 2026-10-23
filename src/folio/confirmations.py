"""Human answers and bounded continuation rounds, preserving prior deliveries."""

import json
import shutil
from pathlib import Path

from folio.document.references import ReferenceDocuments
from folio.models import Evidence, Finding, JobInput, digest
from folio.orchestration.work import WorkState
from folio.requirements import RequirementEntry, RequirementSnapshot
from folio.runtime.language import output_language

NON_BODY_ISSUES = ("cover:", "page-layout:", "review-incomplete:", "local-check-incomplete:")

# These blockers have a dedicated check which reconstructs their current state.
# Business gaps (including work-gap and pending_N) are deliberately not listed:
# a later reviewer omitting a question does not constitute a human decision.
RECHECKED_PENDING_PREFIXES = (
    "research-incomplete:",
    "research-coverage:",
    "research-artifact:",
    "review-incomplete:",
    "local-check-incomplete:",
    "page-layout:",
    "cover:",
    "source-comment:",
    "visual:",
    "required-visual:",
    "record-only:",
)
RETRY_PENDING_PREFIXES = ("adjudication:", "repair-budget:", "repair-no-progress:")


def rounds(store, job_id):
    with store.connect() as conn:
        return [
            dict(row) | {"data": json.loads(row["data"])}
            for row in conn.execute(
                "SELECT * FROM confirmation_rounds WHERE job_id=? ORDER BY id", (job_id,)
            )
        ]


def open_business_questions(history):
    """Carry the latest round's unanswered questions and candidates into review.

    Applied answers from older rounds count too: a retry may have archived an
    already answered question while its fresh review was still interrupted.
    Older snapshots are audit, not an accumulating source of resurrected issues.
    """
    if not history:
        return []
    applied = [
        Finding.model_validate(answer["finding"])
        for row in history
        if row["status"] == "applied"
        for answer in row["data"]["answers"]
    ]
    answered = {finding.fingerprint for finding in applied}
    answered.update(
        finding.key.split(":", 1)[1]
        for finding in applied
        if finding.key.startswith(RETRY_PENDING_PREFIXES)
    )
    previous = [
        Finding.model_validate(raw)
        for raw in history[-1]["data"]["previous_snapshot"].get("blocking_issues", [])
    ]
    original_candidates = {
        finding.fingerprint: finding for finding in previous if finding.kind != "pending"
    }
    result = {}
    for finding in previous:
        if (
            finding.key.startswith(RECHECKED_PENDING_PREFIXES)
            or finding.fingerprint in answered
        ):
            continue
        if finding.key.startswith(RETRY_PENDING_PREFIXES):
            original_id = finding.key.split(":", 1)[1]
            if original_id in answered:
                continue
            # Retry the exact stored candidate through normal adjudication. A
            # transport failure or exhausted repair allowance is not a forever
            # human question, nor permission to forget the underlying concern.
            finding = original_candidates.get(original_id, finding)
        result[finding.fingerprint] = finding
    return list(result.values())


def confirmation_history(store, job_id):
    history = rounds(store, job_id)
    if not history:
        return []
    inputs = store.get(job_id)["input"]
    english = (inputs.get("language") or output_language(inputs.get("user_request"))) == "en"
    return [
        {
            "round": index,
            "request_id": row["request_id"],
            "download_url": f"/jobs/{job_id}/confirmations/{row['request_id']}/artifact",
            "filename": f"draft-before-answers-round-{index}.docx" if english else f"补答前草稿（第{index}轮）.docx",
        }
        for index, row in enumerate(history, 1)
        if row["data"].get("previous_artifact") and Path(row["data"]["previous_artifact"]).is_file()
    ]


def confirmation_view(store, job_id):
    job = store.get(job_id)
    history = rounds(store, job_id)
    token = digest(
        json.dumps(
            [
                job["snapshot"],
                job["input"],
                [(s.section_id, s.revision) for s in store.states(job_id)],
                history[-1]["id"] if history else None,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    issues = []
    seen = set()
    for value in job["snapshot"].get("blocking_issues", []):
        finding = Finding.model_validate(value)
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        issues.append({"issue_id": finding.fingerprint, **finding.model_dump()})
    return {
        "job_id": job_id,
        "status": job["status"],
        "revision": token,
        "issues": issues,
        "used_calls": job["calls"],
        "default_additional_calls": 200,
        "rounds": len(history),
        "history": confirmation_history(store, job_id),
    }


def submit_confirmation(store, job_id, *, request_id, revision, answers, additional_calls):
    """Caller holds the job lock; identical submissions never grant budget twice."""
    payload = {"revision": revision, "answers": answers, "additional_calls": additional_calls}
    existing = next((r for r in rounds(store, job_id) if r["request_id"] == request_id), None)
    if existing:
        if existing["data"]["submission"] != payload:
            raise ValueError("同一次提交的内容已改变，请刷新后重试。")
        return store.get(job_id)
    job = store.get(job_id)
    view = confirmation_view(store, job_id)
    if job["status"] != "needs_confirmation" or revision != view["revision"]:
        raise ValueError("待确认内容已更新，请刷新后再提交。")
    if not 1 <= additional_calls <= 10000 or job["calls"] + additional_calls > 100000:
        raise ValueError("本轮调用额度无效或累计额度超过上限。")
    issue_map = {issue["issue_id"]: issue for issue in view["issues"]}
    if len({a["issue_id"] for a in answers}) != len(answers):
        raise ValueError("同一问题不能重复提交答案。")
    if any(a["issue_id"] not in issue_map or not a["answer"].strip() for a in answers):
        raise ValueError("答案必须对应当前问题，且不能为空。")
    resolved = [
        {
            "finding": {k: v for k, v in issue_map[a["issue_id"]].items() if k != "issue_id"},
            "answer": a["answer"].strip(),
            "source_id": f"confirmation:{request_id}:{a['issue_id']}",
            "evidence_text_format": 2,
            "close_comment": a.get("close_comment", False),
        }
        for a in answers
    ]
    request = JobInput.model_validate(job["input"])
    content_ids = {
        s["section_id"]
        for s in (job["plan"] or {}).get("sections", [])
        if s.get("content_mode", "generate") != "container"
    }
    for item in resolved:
        finding = item["finding"]
        if finding["key"].startswith("source-comment:") and not content_ids.intersection(
            finding["section_ids"]
        ):
            if not content_ids:
                raise ValueError("批注没有可修订的正文，请先确认文档结构。")
            item["original_section_ids"] = finding["section_ids"]
            finding["section_ids"] = sorted(content_ids)
    requirements = request.requirements or RequirementSnapshot.from_text(request.user_request)
    entries = list(requirements.entries)
    for item in resolved:
        finding = item["finding"]
        entries.append(
            RequirementEntry(
                source_id=item["source_id"],
                kind="revision",
                text=(
                    "User confirmation (only for sections "
                    + ", ".join(finding["section_ids"])
                    + "):\n"
                    + finding["description"]
                    + "\nUser answer: "
                    + item["answer"]
                ),
            )
        )
        if finding["key"].startswith("cover:"):
            request.cover[finding["key"].removeprefix("cover:")] = item["answer"]
    requirements = RequirementSnapshot.from_entries(entries)
    requirements.ensure_capacity()
    request = JobInput.model_validate(
        request.model_dump()
        | {
            "requirements": requirements.model_dump(),
            "user_request": requirements.text,
            "limits": request.limits.model_dump() | {"max_calls": job["calls"] + additional_calls},
        }
    )
    revisions = {s.section_id: s.revision for s in store.states(job_id)}
    if not job["plan"] or set(revisions) != {s["section_id"] for s in job["plan"]["sections"]}:
        raise ValueError("正文检查点不完整，无法从待确认状态接续。")
    folder = store.root / "jobs" / job_id / "confirmations" / request_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "input.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    (folder / "requirements.json").write_text(
        requirements.model_dump_json(indent=2), encoding="utf-8"
    )
    archived_artifact = None
    if job["artifact"] and Path(job["artifact"]).is_file():
        archived_artifact = str(folder / "draft.docx")
        shutil.copyfile(job["artifact"], archived_artifact)
    data = {
        "submission": payload,
        "answers": resolved,
        "section_revisions": revisions,
        "previous_input": job["input"],
        "previous_snapshot": job["snapshot"],
        "previous_artifact": archived_artifact,
        "used_calls": job["calls"],
    }
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT status,snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (
            current["status"] != "needs_confirmation"
            or json.loads(current["snapshot"]) != job["snapshot"]
        ):
            raise ValueError("任务状态已变化，请刷新后重试。")
        conn.execute(
            "INSERT INTO confirmation_rounds(job_id,request_id,status,data) VALUES (?,?,?,?)",
            (job_id, request_id, "pending", json.dumps(data, ensure_ascii=False)),
        )
        # Failed attempts remain fully recorded in events and the continuation audit.
        failed = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tasks WHERE job_id=? AND result IS NULL", (job_id,)
            )
        ]
        store._event(
            conn,
            job_id,
            "confirmation_submitted",
            {
                "request_id": request_id,
                "answers": resolved,
                "additional_calls": additional_calls,
                "previous_calls": job["calls"],
                "reset_attempts": failed,
            },
        )
        conn.execute("DELETE FROM tasks WHERE job_id=? AND result IS NULL", (job_id,))
        # User amendments must be reconsidered even when the body has not changed.
        if resolved:
            for row in conn.execute(
                "SELECT * FROM work_states WHERE job_id=?", (job_id,)
            ).fetchall():
                work = WorkState.model_validate_json(row["state"])
                work.review_key = ""
                conn.execute(
                    "UPDATE work_states SET state=? WHERE job_id=? AND task_id=?",
                    (work.model_dump_json(), job_id, work.task_id),
                )
        conn.execute(
            "UPDATE jobs SET input=?,status='queued',stage='review',snapshot='{}',artifact=NULL,"
            "error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (request.model_dump_json(), job_id),
        )
    return store.get(job_id)


class ConfirmedReferences(ReferenceDocuments):
    """User answers are scoped project evidence; original source files stay immutable."""

    def __init__(self, paths, view, history):
        super().__init__(paths, view)
        # The newest answer to the exact same question replaces its earlier answer
        # as an active source; all originals remain in the immutable round audit.
        latest = {}
        self.closed_comments = {}
        for row in history:
            for answer in row["data"]["answers"]:
                latest[Finding.model_validate(answer["finding"]).fingerprint] = answer
                if row["status"] == "applied" and answer.get("close_comment"):
                    self.closed_comments[answer["finding"]["key"]] = answer
        self.answers = list(latest.values())

    def evidence(self, section, max_chars):
        result = super().evidence(section, None)
        for answer in self.answers:
            finding = answer["finding"]
            if section.section_id not in finding["section_ids"] or finding["key"].startswith(
                NON_BODY_ISSUES
            ):
                continue
            if answer.get("evidence_text_format") == 2:
                text = "Question requiring confirmation: " + finding["description"] + "\nUser answer: " + answer["answer"]
            else:
                # Keep the original bytes and source_version for saved evidence.
                text = "待确认问题：" + finding["description"] + "\n用户答复：" + answer["answer"]
            result.append(
                Evidence(
                    evidence_id="ev_" + digest(answer["source_id"]),
                    # This historical identifier is part of evidence identity.
                    source_id="用户补充确认",
                    source_version=digest(text),
                    locator=answer["source_id"],
                    text=text,
                    metadata={
                        "role": "project_requirement",
                        "section_ids": finding["section_ids"],
                        "human_confirmation": True,
                    },
                )
            )
        if max_chars is not None and sum(len(e.text) for e in result) > max_chars:
            raise ValueError("Project source and human answers exceed evidence budget")
        return result

    def comments(self, section=None):
        result = []
        for comment in super().comments(section):
            answer = self.closed_comments.get(
                f"source-comment:{comment['source']}:{comment['comment_id']}"
            )
            result.append(
                comment | ({"resolved": True, "human_answer": answer["answer"]} if answer else {})
            )
        return result


async def apply_pending_confirmations(engine, history):
    from folio.research.artifact_issues import unresolved_artifact_issues

    pending = [row for row in history if row["status"] == "pending"]
    if not pending:
        return
    answers = [a for row in pending for a in row["data"]["answers"]]
    sections = {s.section_id: s for s in engine.plan.sections}
    by_section = {}
    for answer in answers:
        if answer["finding"]["key"].startswith(NON_BODY_ISSUES):
            continue
        for sid in answer["finding"]["section_ids"]:
            if sections[sid].content_mode != "container":
                by_section.setdefault(sid, []).append(answer)
    states = engine.store.states(engine.job_id)
    previous = {s.section_id: s for s in states}
    updates = []
    # Each owner re-reads new user evidence once; existing retrieval allowances stay intact.
    for task in engine.work_plan.tasks:
        affected = set(task.section_ids) & by_section.keys()
        work_state = engine.store.work_states(engine.job_id).get(task.task_id)
        research = work_state.research if work_state else {}
        research_pending = (
            research.get("incomplete") or research.get("coverage_unresolved")
            or unresolved_artifact_issues(research, [s for s in states if s.section_id in task.section_ids])
            or any(f.key.startswith("research-coverage:") for f in (work_state.findings if work_state else []))
        )
        if affected:
            instruction = (
                f"Recheck existing business records and gaps using user confirmation round {pending[-1]['id']}.\n"
                + "\n".join(
                    a["finding"]["description"] + ": " + a["answer"]
                    for sid in sorted(affected)
                    for a in by_section[sid]
                )
            )
            await engine.research_work(task, instruction)
        elif research_pending:
            await engine.research_work(task, f"In confirmation round {pending[-1]['id']}, the user explicitly resumed the previously incomplete source research.")
    for sid, items in by_section.items():
        finding = Finding(
            kind="pending",
            key="human-confirmation:" + str(pending[-1]["id"]),
            description="Revise this section using the user's supplemental confirmation",
            section_ids=[sid],
        )
        instruction = "Apply the user's explicit answers to these questions, preserving unrelated body text and sources:\n" + "\n".join(
            a["finding"]["description"] + "\nAnswer: " + a["answer"] for a in items
        )
        revised = await engine.generate(sections[sid], previous[sid], finding, instruction)
        if revised.section_id != sid or revised.revision != previous[sid].revision + 1:
            raise ValueError("Confirmation repair returned an invalid section revision")
        updates.append(revised)
    for state in states:
        if state.section_id in by_section or not any(
            f.key.startswith("local-check-incomplete:") for f in state.local_findings
        ):
            continue
        updates.append(
            await engine.validate_section(
                sections[state.section_id],
                state.content,
                state.evidence,
                state.revision + 1,
                feedback="The user explicitly resumed checking. Preserve the original body and complete the previously interrupted fact extraction and verification.",
            )
        )
    engine.store.save_sections_batch(
        engine.job_id,
        updates,
        expected_revisions={s.section_id: s.revision for s in states},
        confirmation_ids=[row["id"] for row in pending],
    )
    engine.store.work_event(engine.job_id, "用户补充已应用，正在重新检查正文、配图与交付版式。")
