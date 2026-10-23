"""Human continuation uses real checkpoints, validation, and bounded new budgets."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from folio.api.app import create_app
from folio.confirmations import (
    ConfirmedReferences,
    confirmation_view,
    rounds,
    submit_confirmation,
)
from folio.document.references import ReferenceDocuments
from folio.models import Finding, JobInput, Section, digest
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


@pytest.fixture(autouse=True)
def no_office(monkeypatch):
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


class Interrupted(Engine):
    async def global_review(self, states):
        raise ModelCallFailed("temporarily unavailable", "connection")


class AnswerRuntime(DemoRuntime):
    def __init__(self):
        self.repairs = []

    async def complete(self, task, context, schema, session_key):
        if task == "repair" and any(
            e.get("source_id") == "用户补充确认" for e in context["evidence"]
        ):
            self.repairs.append(context["section"]["section_id"])
            return RuntimeResult(
                json.dumps(
                    {
                        "blocks": [
                            {
                                "block_id": f"source_{i}",
                                "kind": "paragraph",
                                "text": e["text"],
                                "origin": "evidence",
                                "evidence_ids": [e["evidence_id"]],
                            }
                            for i, e in enumerate(context["evidence"])
                        ],
                        "pending_items": [],
                    },
                    ensure_ascii=False,
                )
            )
        return await super().complete(task, context, schema, session_key)


def test_confirmation_translation_preserves_saved_evidence_identity():
    finding = Finding(kind="pending", key="limit", description="审批上限？", section_ids=["s1"])
    answer = {"finding": finding.model_dump(), "answer": "100万元", "source_id": "confirmation:old"}
    section = Section(section_id="s1", node_id="n1", display_number="1", title="审批", level=1, instruction="审批上限")

    def evidence(value):
        references = ConfirmedReferences([], "final", [{"status": "applied", "data": {"answers": [value]}}])
        return references.evidence(section, None)[0]

    old = evidence(answer)
    assert old.text == "待确认问题：审批上限？\n用户答复：100万元"
    assert old.source_version == digest(old.text)
    updated = evidence(answer | {"evidence_text_format": 2})
    assert updated.text == "Question requiring confirmation: 审批上限？\nUser answer: 100万元"
    assert updated.evidence_id == old.evidence_id
    assert updated.source_id == old.source_id
    assert updated.locator == old.locator


async def waiting_job(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    await Interrupted(store, DemoRuntime()).run(job_id)
    assert store.get(job_id)["status"] == "needs_confirmation"
    return job_id


def submit(store, job_id, answers=(), **extra):
    return submit_confirmation(
        store,
        job_id,
        request_id=extra.pop("request_id", uuid4().hex),
        revision=extra.pop("revision", confirmation_view(store, job_id)["revision"]),
        answers=list(answers),
        additional_calls=extra.pop("additional_calls", 200),
        **extra,
    )


async def test_service_retry_keeps_body_and_successful_cache_and_publishes(store, inputs):
    job_id = await waiting_job(store, inputs)
    original = store.get(job_id)
    states = store.states(job_id)
    with store.connect() as conn:
        cached = dict(
            conn.execute(
                "SELECT task_key,result FROM tasks WHERE job_id=? AND result IS NOT NULL", (job_id,)
            ).fetchall()
        )
    request_id = uuid4().hex
    revision = confirmation_view(store, job_id)["revision"]
    submit(store, job_id, request_id=request_id, revision=revision, additional_calls=80)
    queued = store.get(job_id)
    assert queued["calls"] == original["calls"]
    assert queued["input"]["limits"]["max_calls"] == original["calls"] + 80
    assert queued["artifact"] is None and not queued["snapshot"]
    archived = rounds(store, job_id)[0]["data"]["previous_artifact"]
    assert Path(archived).read_bytes() == Path(original["artifact"]).read_bytes()
    submit(store, job_id, request_id=request_id, revision=revision, additional_calls=80)
    assert len(rounds(store, job_id)) == 1
    result = await Engine(store, DemoRuntime()).run(job_id)
    assert result["status"] == "published"
    assert store.states(job_id) == states
    assert rounds(store, job_id)[0]["status"] == "applied"
    with store.connect() as conn:
        for key, result in cached.items():
            assert (
                conn.execute(
                    "SELECT result FROM tasks WHERE job_id=? AND task_key=?", (job_id, key)
                ).fetchone()[0]
                == result
            )


async def test_answer_repairs_only_selected_section_and_is_sourced(store, inputs):
    job_id = await waiting_job(store, inputs)
    before = store.states(job_id)
    target = before[-1]
    question = Finding(
        kind="pending",
        key="approval-limit",
        description="审批上限是多少？",
        section_ids=[target.section_id],
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(question.model_dump())
    store.update(job_id, snapshot=snapshot)
    submit(store, job_id, [{"issue_id": question.fingerprint, "answer": "审批上限为100万元。"}])
    runtime = AnswerRuntime()
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "published"
    assert runtime.repairs == [target.section_id]
    updated = {s.section_id: s for s in store.states(job_id)}
    assert updated[before[0].section_id] == before[0]
    assert updated[target.section_id].revision == target.revision + 1
    assert any("100万元" in b.text for b in updated[target.section_id].content.blocks)
    assert any(e.metadata.get("human_confirmation") for e in updated[target.section_id].evidence)
    assert job["input"]["requirements"]["entries"][-1]["kind"] == "revision"


async def test_stale_unknown_and_duplicate_answers_do_not_mutate(store, inputs):
    job_id = await waiting_job(store, inputs)
    before = store.get(job_id)
    with pytest.raises(ValueError, match="更新"):
        submit(store, job_id, revision="old")
    with pytest.raises(ValueError, match="当前问题"):
        submit(store, job_id, [{"issue_id": "unknown", "answer": "yes"}])
    issue = confirmation_view(store, job_id)["issues"][0]["issue_id"]
    with pytest.raises(ValueError, match="重复"):
        submit(store, job_id, [{"issue_id": issue, "answer": "yes"}] * 2)
    assert store.get(job_id) == before and rounds(store, job_id) == []


def test_source_comment_requires_explicit_closure_and_committed_round(monkeypatch):
    monkeypatch.setattr(
        ReferenceDocuments,
        "comments",
        lambda *_: [
            {
                "source": "source.docx",
                "comment_id": "1",
                "resolved": False,
                "text": "审批金额未确认",
            }
        ],
    )
    finding = Finding(
        kind="pending",
        key="source-comment:source.docx:1",
        description="审批金额未确认",
        section_ids=["s1"],
    )
    answer = {"finding": finding.model_dump(), "answer": "暂时不知道", "source_id": "a"}

    def references(status, close):
        return ConfirmedReferences(
            [],
            "final",
            [{"status": status, "data": {"answers": [answer | {"close_comment": close}]}}],
        )

    assert references("applied", False).comments()[0]["resolved"] is False
    assert references("pending", True).comments()[0]["resolved"] is False
    assert references("applied", True).comments()[0]["resolved"] is True


def test_latest_answer_to_identical_question_replaces_required_source():
    from folio.models import Section

    finding = Finding(kind="pending", key="limit", description="上限是多少？", section_ids=["s1"])
    history = [
        {
            "status": "applied",
            "data": {
                "answers": [
                    {
                        "finding": finding.model_dump(),
                        "answer": value,
                        "source_id": f"answer:{index}",
                    }
                ]
            },
        }
        for index, value in enumerate(["100万元", "改为50万元"])
    ]
    refs = ConfirmedReferences([], "final", history)
    section = Section(
        section_id="s1",
        node_id="n1",
        display_number="1",
        title="审批",
        level=1,
        instruction="审批上限",
    )
    active = refs.evidence(section, None)
    assert len(active) == 1 and "改为50万元" in active[0].text
    assert len(history) == 2


async def test_cover_answer_does_not_rewrite_business_body(store, inputs):
    job_id = await waiting_job(store, inputs)
    states = store.states(job_id)
    question = Finding(
        kind="pending",
        key="cover:author",
        description="编制人待填写",
        section_ids=[states[0].section_id],
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(question.model_dump())
    store.update(job_id, snapshot=snapshot)
    submit(store, job_id, [{"issue_id": question.fingerprint, "answer": "项目组"}])
    result = await Engine(store, AnswerRuntime()).run(job_id)
    assert result["status"] == "published"
    assert result["input"]["cover"]["author"] == "项目组"
    assert store.states(job_id) == states


async def test_interrupted_answer_can_resume_without_duplicate_commit(store, inputs):
    job_id = await waiting_job(store, inputs)
    states = store.states(job_id)
    question = Finding(
        kind="pending",
        key="approval",
        description="审批人是谁",
        section_ids=[states[-1].section_id],
    )
    snapshot = store.get(job_id)["snapshot"]
    snapshot["blocking_issues"].append(question.model_dump())
    store.update(job_id, snapshot=snapshot)
    submit(store, job_id, [{"issue_id": question.fingerprint, "answer": "由主管审批"}])

    class BeforeCommit(Engine):
        async def generate(self, *args, **kwargs):
            raise ModelCallFailed("offline", "connection")

    interrupted = await BeforeCommit(store, AnswerRuntime()).run(job_id)
    assert interrupted["status"] == "needs_confirmation"
    assert store.states(job_id) == states
    assert rounds(store, job_id)[0]["status"] == "pending"
    submit(store, job_id)
    completed = await Engine(store, AnswerRuntime()).run(job_id)
    assert completed["status"] == "published"
    assert all(row["status"] == "applied" for row in rounds(store, job_id))
    assert store.states(job_id)[-1].revision == states[-1].revision + 1


async def test_confirmation_and_section_commit_roll_back_together(store, inputs):
    job_id = await waiting_job(store, inputs)
    submit(store, job_id)
    row = rounds(store, job_id)[0]
    states = store.states(job_id)
    changed = states[0].model_copy(update={"revision": states[0].revision + 1})
    with pytest.raises(Exception, match="no longer pending"):
        store.save_sections_batch(
            job_id,
            [changed],
            expected_revisions={s.section_id: s.revision for s in states},
            confirmation_ids=[row["id"], -1],
        )
    assert store.states(job_id) == states
    assert rounds(store, job_id)[0]["status"] == "pending"


@pytest.mark.parametrize("task", ["extract", "validate"])
async def test_retry_rechecks_incomplete_local_validation_without_rewriting(store, inputs, task):
    from test_local_validation_failure import FailingCheck

    job_id = store.create(JobInput(**inputs, runtime="llm"))
    initial = await Engine(store, FailingCheck(task)).run(job_id)
    assert initial["status"] == "needs_confirmation"
    old = {s.section_id: s for s in store.states(job_id)}
    submit(store, job_id)
    result = await Engine(store, DemoRuntime()).run(job_id)
    assert result["status"] == "published"
    for state in store.states(job_id):
        assert state.content == old[state.section_id].content
        assert state.validation_completed
        assert not any(f.key.startswith("local-check-incomplete:") for f in state.local_findings)


@pytest.mark.parametrize("with_answer", [False, True])
async def test_api_checks_owner_and_repeated_submission_grants_budget_once(
    tmp_path, inputs, monkeypatch, with_answer
):
    async def no_run(*args):
        return None

    monkeypatch.setattr("folio.api.workspace.execute", no_run)
    app = create_app(tmp_path / "api")
    store = app.state.store
    job_id = await waiting_job(store, inputs)
    question = Finding(
        kind="pending",
        key="approval-amount",
        description="审批上限是多少？",
        section_ids=[store.states(job_id)[-1].section_id],
    )
    if with_answer:
        snapshot = store.get(job_id)["snapshot"]
        snapshot["blocking_issues"].append(question.model_dump())
        store.update(job_id, snapshot=snapshot)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        cid = (await client.post("/conversations")).json()["id"]
        with store.connect() as conn:
            state = json.loads(
                conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0]
            )
            state["active_job"] = job_id
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
        view = (
            await client.get(f"/jobs/{job_id}/confirmation", params={"conversation_id": cid})
        ).json()
        payload = {
            "conversation_id": cid,
            "request_id": uuid4().hex,
            "revision": view["revision"],
            "answers": [{"issue_id": question.fingerprint, "answer": "审批上限100万元"}]
            if with_answer
            else [],
            "additional_calls": 50,
        }
        wrong = (await client.post("/conversations")).json()["id"]
        denied = await client.post(
            f"/jobs/{job_id}/confirmation", json=payload | {"conversation_id": wrong}
        )
        assert denied.status_code == 409
        for _ in range(2):
            response = await client.post(f"/jobs/{job_id}/confirmation", json=payload)
            assert response.status_code == 200, response.text
        assert len(rounds(store, job_id)) == 1
        assert store.get(job_id)["input"]["limits"]["max_calls"] == view["used_calls"] + 50
        archived = await client.get(
            f"/jobs/{job_id}/confirmations/{payload['request_id']}/artifact"
        )
        assert archived.status_code == 200
        conversation = (await client.get(f"/conversations/{cid}")).json()
        entries = conversation["requirements"]["entries"]
        assert len(entries) == (1 if with_answer else 0)
        if with_answer:
            assert entries[0]["kind"] == "revision"
            assert "审批上限是多少？" in entries[0]["text"] and "100万元" in entries[0]["text"]
