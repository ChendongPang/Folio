"""Continue the same confirmed body job without replanning or losing later edits."""

import json

import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.decisions import IntentDecision
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.demo import DemoRuntime


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", lambda _: DemoRuntime())
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *args, **kwargs: {"status": "requires_office"}
    )
    runs = []

    async def execute(store, job_id):
        runs.append(job_id)
        if len(runs) == 1:
            store.update(job_id, status="failed", stage="generation", error="test HTTP 502")
        else:
            store.audit_event(job_id, "work_update", {"text": "继续已保存的业务研究"})
            store.update(job_id, status="paused", stage="generation")

    monkeypatch.setattr(workspace, "execute", execute)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        yield client, app.state.store, runs


def failed_job(setup):
    client, store, runs = setup
    cid = client.post("/conversations").json()["id"]
    client.post(f"/conversations/{cid}/sources?filename=facts.md", content="审批规则需要覆盖一期。")
    planned = client.post(
        f"/conversations/{cid}/messages",
        json={"text": "编写一期审批需求说明书", "action": "generate"},
    )
    assert "directory.review" in planned.text
    state = client.get(f"/conversations/{cid}").json()
    confirmed = client.post(
        f"/conversations/{cid}/messages",
        json={
            "text": "确认目录", "action": "confirm_directory",
            "selection_revision": state["directory_review"]["revision"],
        },
    )
    assert "job.failed" in confirmed.text
    state = client.get(f"/conversations/{cid}").json()
    assert state["active_job_status"] == "failed"
    return cid, state


def intent(monkeypatch, name="resume_generation"):
    workflows = []

    async def detect(config, messages, workflow):
        workflows.append(workflow)
        return IntentDecision(intent=name, summary="按当前任务状态识别请求")

    async def no_planning(*args, **kwargs):
        raise AssertionError("Resuming a confirmed body must not plan a new directory")

    monkeypatch.setattr(workspace, "detect_intent", detect)
    monkeypatch.setattr(workspace.SourceDirectoryDrafts, "create_from_sources", no_planning)
    return workflows


def test_chat_resume_reuses_job_directory_and_requirements(setup, monkeypatch):
    client, store, runs = setup
    cid, before = failed_job(setup)
    workflows = intent(monkeypatch)
    response = client.post(
        f"/conversations/{cid}/messages", json={"text": "接着上次的任务接着干"}
    )
    job_id = before["active_job"]
    assert "event: job.created" in response.text and '"resumed": true' in response.text
    assert "继续已保存的业务研究" in response.text and "event: job.paused" in response.text
    assert "directory.update" not in response.text and "directory.review" not in response.text
    after = client.get(f"/conversations/{cid}").json()
    assert runs == [job_id, job_id]
    assert after["active_job"] == job_id and after["active_job_status"] == "paused"
    assert after["directory_review"] == before["directory_review"]
    assert after["requirements"] == before["requirements"]
    assert after["messages"][-2]["requirement"]["status"] == "command"
    assert after["messages"][-2]["action"] == "resume_generation"
    assert workflows[0]["active_job"] == {
        "id": job_id, "status": "failed", "stage": "generation", "resumable": True,
    }
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    # The stream released the input lock; ordinary controls remain available.
    assert client.post(f"/jobs/{job_id}/pause", json={"conversation_id": cid}).status_code == 200


@pytest.mark.parametrize("change", ["requirements", "attachment", "role", "guide"])
def test_resume_refuses_changed_inputs_in_chat_and_button(setup, monkeypatch, change):
    client, store, runs = setup
    cid, before = failed_job(setup)
    job_id = before["active_job"]
    if change == "requirements":
        intent(monkeypatch, "requirements")
        client.post(f"/conversations/{cid}/messages", json={"text": "增加二期支付要求"})
    elif change == "attachment":
        client.post(f"/conversations/{cid}/sources?filename=extra.md", content="新的二期支付规则")
    elif change == "role":
        with store.connect() as conn:
            state = json.loads(conn.execute(
                "SELECT data FROM web_conversations WHERE id=?", (cid,)
            ).fetchone()[0])
            state["files"][0]["role"] = "style_reference"
            state.pop("directory_review", None)
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
    else:
        assert client.put(f"/conversations/{cid}/writing-guide", json={"guide_id": "srs"}).status_code == 200
    intent(monkeypatch)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "继续上次正文任务"})
    assert "已改变" in response.text and "event: job.created" not in response.text
    control = client.post(f"/jobs/{job_id}/resume", json={"conversation_id": cid})
    assert control.status_code == 409 and "已改变" in control.json()["detail"]
    assert runs == [job_id] and store.get(job_id)["status"] == "failed"


def test_generate_misclassification_cannot_replan_confirmed_job(setup, monkeypatch):
    client, store, runs = setup
    cid, before = failed_job(setup)
    intent(monkeypatch, "generate")
    response = client.post(f"/conversations/{cid}/messages", json={"text": "接着上次的任务接着干"})
    assert "已有按确认目录启动的正文任务" in response.text
    after = client.get(f"/conversations/{cid}").json()
    assert after["active_job"] == before["active_job"]
    assert after["directory_review"] == before["directory_review"]
    assert after["requirements"] == before["requirements"]
    assert runs == [before["active_job"]]
    intent(monkeypatch)
    resumed = client.post(f"/conversations/{cid}/messages", json={"text": "继续上次正文任务"})
    assert '"resumed": true' in resumed.text
    assert runs == [before["active_job"], before["active_job"]]


def test_resume_without_existing_job_is_not_generation(setup, monkeypatch):
    client, store, runs = setup
    cid = client.post("/conversations").json()["id"]
    intent(monkeypatch)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "继续上次正文任务"})
    assert "没有可以继续的正文任务" in response.text
    assert "directory.update" not in response.text and not runs


def test_explicit_resume_action_supports_paused_job_without_intent_call(setup, monkeypatch):
    client, store, runs = setup
    cid, before = failed_job(setup)
    job_id = before["active_job"]
    store.update(job_id, status="paused")

    async def no_intent(*args, **kwargs):
        raise AssertionError("Explicit resume is a control, not a model decision")

    monkeypatch.setattr(workspace, "detect_intent", no_intent)
    response = client.post(
        f"/conversations/{cid}/messages",
        json={"text": "继续正文任务", "action": "resume_generation"},
    )
    assert '"resumed": true' in response.text and runs == [job_id, job_id]
    assert client.get(f"/conversations/{cid}").json()["requirements"] == before["requirements"]


async def test_job_event_replay_starts_at_latest_resume_boundary():
    class Store:
        def events(self, job_id, after):
            events = [
                {"id": 1, "kind": "work_update", "data": {"text": "旧的工作步骤"}},
                {"id": 2, "kind": "task_failed", "data": {}},
                {"id": 3, "kind": "job_control", "data": {"status": "queued", "action": "resume"}},
                {"id": 4, "kind": "work_update", "data": {"text": "恢复后的工作步骤"}},
            ]
            return [event for event in events if event["id"] > after]

        def get(self, job_id):
            return {"status": "paused"}

    text = "".join([event async for event in workspace.job_events(Store(), "job")])
    assert "旧的工作步骤" not in text and "恢复后的工作步骤" in text
    assert "job.paused" in text and "job.failed" not in text
