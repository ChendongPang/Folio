"""User questions survive continuation until explicitly answered and rechecked."""

import json

import pytest
from test_confirmations import AnswerRuntime, Interrupted, submit, waiting_job

from folio.confirmations import confirmation_view, open_business_questions, rounds
from folio.models import Finding, digest
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


@pytest.fixture(autouse=True)
def no_office(monkeypatch):
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


def business_question(store, job_id):
    question = Finding(
        kind="pending",
        key="cross-section-approval-limit",
        description="审批上限选择50万元还是100万元？",
        section_ids=[store.states(job_id)[-1].section_id],
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(question.model_dump())
    store.update(job_id, snapshot=snapshot)
    return question


@pytest.mark.parametrize("stale_snapshot", [False, True])
async def test_empty_retry_interrupted_again_preserves_original_business_question(
    store, inputs, stale_snapshot
):
    job_id = await waiting_job(store, inputs)
    question = business_question(store, job_id)
    original = store.states(job_id)
    submit(store, job_id)

    class InterruptAgain(Interrupted):
        async def global_review(self, states):
            if stale_snapshot:
                self.store.update(
                    self.job_id, snapshot={"state_digest": "obsolete", "blocking_issues": []}
                )
            return await super().global_review(states)

    result = await InterruptAgain(store, DemoRuntime()).run(job_id)
    assert result["status"] == "needs_confirmation"
    assert store.states(job_id) == original
    issues = confirmation_view(store, job_id)["issues"]
    assert sum(issue["issue_id"] == question.fingerprint for issue in issues) == 1
    assert any(issue["key"] == "review-incomplete:connection" for issue in issues)
    assert rounds(store, job_id)[0]["status"] == "applied"


async def test_partial_answer_keeps_other_business_question_until_next_round(store, inputs):
    job_id = await waiting_job(store, inputs)
    first = business_question(store, job_id)
    second = Finding(
        kind="pending",
        key="final-approval-role",
        description="最终审批角色由谁担任？",
        section_ids=[store.states(job_id)[0].section_id],
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(second.model_dump())
    store.update(job_id, snapshot=snapshot)
    submit(store, job_id, [{"issue_id": first.fingerprint, "answer": "审批上限为100万元。"}])
    partial = await Engine(store, AnswerRuntime()).run(job_id)
    assert partial["status"] == "needs_confirmation"
    assert not partial["snapshot"]["review_passed"]
    assert [issue["issue_id"] for issue in confirmation_view(store, job_id)["issues"]] == [
        second.fingerprint
    ]
    submit(store, job_id, [{"issue_id": second.fingerprint, "answer": "最终由财务主管审批。"}])
    completed = await Engine(store, AnswerRuntime()).run(job_id)
    assert completed["status"] == "published"
    assert completed["snapshot"]["review_passed"]
    assert not completed["snapshot"]["blocking_issues"]
    assert all(row["status"] == "applied" for row in rounds(store, job_id))


def question_history(findings, *, answers=(), status="applied"):
    return {
        "status": status,
        "data": {
            "answers": [{"finding": finding.model_dump()} for finding in answers],
            "previous_snapshot": {"blocking_issues": [f.model_dump() for f in findings]},
        },
    }


def test_only_latest_snapshot_is_carried_and_all_applied_rounds_count():
    first = Finding(kind="pending", key="limit", description="额度待定", section_ids=["s1"])
    second = Finding(kind="pending", key="role", description="审批人待定", section_ids=["s2"])
    history = [
        question_history([first, second], answers=[first], status="pending"),
        question_history([first, second], answers=[second], status="pending"),
    ]
    assert open_business_questions(history) == [first, second]
    history[0]["status"] = "applied"
    assert open_business_questions(history) == [second]
    history[1]["status"] = "applied"
    assert open_business_questions(history) == []
    # An old question absent from the latest complete snapshot is not resurrected.
    history = [question_history([first]), question_history([second])]
    assert open_business_questions(history) == [second]


@pytest.mark.parametrize("prefix", ["work-gap:", "pending_", "business:"])
def test_business_gaps_are_not_removed_by_empty_retry(prefix):
    finding = Finding(
        kind="pending", key=prefix + "1", description="确认业务限制", section_ids=["s1"]
    )
    assert open_business_questions([question_history([finding])]) == [finding]


def test_technical_and_deterministic_blockers_are_rebuilt_by_their_checks():
    findings = [
        Finding(kind="pending", key=prefix + "1", description="需重新检查", section_ids=["s1"])
        for prefix in (
            "review-incomplete:",
            "local-check-incomplete:",
            "page-layout:",
            "cover:",
            "source-comment:",
            "visual:",
            "required-visual:",
            "record-only:",
        )
    ]
    assert open_business_questions([question_history(findings)]) == []


@pytest.mark.parametrize("prefix", ["", "adjudication:", "repair-budget:", "repair-no-progress:"])
async def test_exact_wrapped_issue_reenters_adjudication_after_empty_retry(store, inputs, prefix):
    job_id = await waiting_job(store, inputs)
    original = Finding(
        kind="conflict",
        key="approval-review",
        description="跨章审批规则疑似矛盾",
        section_ids=[state.section_id for state in store.states(job_id)],
    )
    wrapped = Finding(
        kind="pending",
        key=prefix + original.fingerprint,
        description="原问题未处理完成",
        section_ids=original.section_ids,
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(original.model_dump())
    if prefix:
        snapshot["blocking_issues"].append(wrapped.model_dump())
    store.update(job_id, snapshot=snapshot)
    submit(store, job_id)

    class Recovered(DemoRuntime):
        def __init__(self):
            self.checked = []

        async def complete(self, task, context, schema, session_key):
            if task == "drill_down":
                self.checked.append(context["finding"])
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": False,
                            "needs_confirmation": False,
                            "reason": "完整原文已经包含审批规则，原疑点不成立。",
                        }
                    )
                )
            return await super().complete(task, context, schema, session_key)

    runtime = Recovered()
    completed = await Engine(store, runtime).run(job_id)
    assert completed["status"] == "published"
    assert runtime.checked and all(f["key"] == original.key for f in runtime.checked)
    assert not completed["snapshot"]["blocking_issues"]


def test_wrapped_question_answer_resolves_exact_original_but_missing_original_stays_pending():
    original = Finding(kind="coverage", key="role", description="缺少审批人", section_ids=["s1"])
    wrapped = Finding(
        kind="pending",
        key="adjudication:" + original.fingerprint,
        description="问题精查中断",
        section_ids=["s1"],
    )
    assert open_business_questions([question_history([original, wrapped])]) == [original]
    assert open_business_questions([question_history([wrapped])]) == [wrapped]
    for answered in (original, wrapped):
        assert (
            open_business_questions(
                [
                    question_history([original, wrapped], answers=[answered]),
                ]
            )
            == []
        )


async def test_applied_answer_stays_unverified_when_global_review_is_interrupted(store, inputs):
    job_id = await waiting_job(store, inputs)
    question = business_question(store, job_id)
    original = {state.section_id: state for state in store.states(job_id)}
    submit(store, job_id, [{"issue_id": question.fingerprint, "answer": "审批上限为100万元。"}])
    result = await Interrupted(store, AnswerRuntime()).run(job_id)
    updated = {state.section_id: state for state in store.states(job_id)}
    target = question.section_ids[0]
    assert updated[target].revision == original[target].revision + 1
    assert any("100万元" in block.text for block in updated[target].content.blocks)
    assert rounds(store, job_id)[0]["status"] == "applied"
    assert result["status"] == "needs_confirmation"
    assert any(
        issue["issue_id"] == question.fingerprint
        for issue in confirmation_view(store, job_id)["issues"]
    )


async def test_completed_fresh_review_prevents_historical_question_from_reappearing(store, inputs):
    job_id = await waiting_job(store, inputs)
    question = business_question(store, job_id)
    submit(store, job_id, [{"issue_id": question.fingerprint, "answer": "审批上限为100万元。"}])

    class InterruptDelivery(Engine):
        async def finish(self, states, **kwargs):
            if kwargs.get("allow_model_calls", True):
                current = self.store.get(self.job_id)["snapshot"]
                assert current["review_passed"] and not current["blocking_issues"]
                assert current["state_digest"] == digest(
                    "".join(state.model_dump_json() for state in states)
                )
                raise ModelCallFailed("delivery service unavailable", "connection")
            return await super().finish(states, **kwargs)

    result = await InterruptDelivery(store, AnswerRuntime()).run(job_id)
    assert result["status"] == "needs_confirmation"
    issues = confirmation_view(store, job_id)["issues"]
    assert all(issue["issue_id"] != question.fingerprint for issue in issues)
    assert any(issue["key"] == "review-incomplete:connection" for issue in issues)
