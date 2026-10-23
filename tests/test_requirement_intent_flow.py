"""A consultation must not revoke a directory, while actual edits must do so."""

import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.decisions import IntentDecision
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.demo import DemoRuntime


@pytest.fixture
def client(tmp_path, monkeypatch):
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", lambda _: DemoRuntime())
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )

    async def chat(*args, **kwargs):
        yield "message.completed", {"text": "这是对原有目录的解释。"}

    async def execute(store, job_id):
        store.update(job_id, status="failed", error="test stops after confirmed job creation")

    monkeypatch.setattr(workspace, "chat_events", chat)
    monkeypatch.setattr(workspace, "execute", execute)
    with TestClient(create_app(tmp_path)) as instance:
        yield instance


def begin(client):
    cid = client.post("/conversations").json()["id"]
    client.post(f"/conversations/{cid}/sources?filename=facts.md", content="审批规则需要覆盖一期。")
    response = client.post(
        f"/conversations/{cid}/messages",
        json={"text": "编写一期审批需求说明书", "action": "generate"},
    )
    assert "directory.review" in response.text
    return cid, client.get(f"/conversations/{cid}").json()


def install_intent(monkeypatch, name):
    calls = []

    async def detect(config, messages, workflow):
        calls.append(messages[-1]["content"])
        return IntentDecision(intent=name, summary="分类依据")

    monkeypatch.setattr(workspace, "detect_intent", detect)
    return calls


def confirm(client, cid, selection):
    return client.post(
        f"/conversations/{cid}/messages",
        json={
            "text": "确认目录",
            "action": "confirm_directory",
            "selection_revision": selection["revision"],
        },
    )


def test_consultation_keeps_directory_confirmable_and_is_audited(client, monkeypatch):
    cid, initial = begin(client)
    calls = install_intent(monkeypatch, "chat")
    text = "为什么目录里有审批管理这一章？"
    response = client.post(f"/conversations/{cid}/messages", json={"text": text})
    assert "message.completed" in response.text
    current = client.get(f"/conversations/{cid}").json()
    assert current["requirements"] == initial["requirements"]
    assert current["directory_review"] == initial["directory_review"]
    audit = current["messages"][-2]["requirement"]
    assert audit == {
        "status": "discussion", "kind": "request", "source": "intent",
        "intent": "chat", "reason": "分类依据",
    }
    result = confirm(client, cid, initial["directory_review"])
    assert result.status_code == 200 and "job.created" in result.text
    assert calls == [text]  # No extra requirement-classification model call.


def test_explicit_revision_is_saved_but_requires_a_new_directory(client, monkeypatch):
    cid, initial = begin(client)
    calls = install_intent(monkeypatch, "requirements")
    text = "请把审批上限改为100万元可以吗？"
    response = client.post(f"/conversations/{cid}/messages", json={"text": text})
    assert "已记录这次写作要求" in response.text
    current = client.get(f"/conversations/{cid}").json()
    assert current["requirements"]["revision"] != initial["requirements"]["revision"]
    assert current["requirements"]["entries"][-1]["kind"] == "revision"
    assert current["requirements"]["entries"][-1]["text"] == text
    assert current["directory_review"] == initial["directory_review"]
    assert confirm(client, cid, initial["directory_review"]).status_code == 409
    updated = client.post(
        f"/conversations/{cid}/messages",
        json={
            "text": "根据最新要求重新生成目录",
            "action": "regenerate_directory",
            "selection_revision": initial["directory_review"]["revision"],
        },
    )
    assert "directory.review" in updated.text
    selected = client.get(f"/conversations/{cid}").json()["directory_review"]
    assert selected["requirements"] == current["requirements"]
    assert text in selected["request"]
    assert "job.created" in confirm(client, cid, selected).text
    assert calls == [text]


def test_unclear_edit_requests_explicit_instructions_without_changing_requirements(
    client, monkeypatch
):
    cid, initial = begin(client)
    install_intent(monkeypatch, "clarify")
    response = client.post(
        f"/conversations/{cid}/messages", json={"text": "审批这块再考虑一下"}
    )
    assert "暂未改变写作要求" in response.text and "修改建议" in response.text
    current = client.get(f"/conversations/{cid}").json()
    assert current["requirements"] == initial["requirements"]
    assert current["messages"][-2]["requirement"]["status"] == "clarification"


def test_failed_classification_blocks_confirmation_until_original_message_is_retried(
    client, monkeypatch
):
    cid, initial = begin(client)
    text = "审批上限改为100万元"

    async def fail(*args):
        raise TimeoutError()

    monkeypatch.setattr(workspace, "detect_intent", fail)
    failed = client.post(f"/conversations/{cid}/messages", json={"text": text})
    assert "message.failed" in failed.text
    current = client.get(f"/conversations/{cid}").json()
    assert current["requirements"] == initial["requirements"]
    assert current["pending_requirement_messages"] == [{"source_id": "message:2", "text": text}]
    blocked = confirm(client, cid, initial["directory_review"])
    assert blocked.status_code == 409 and "尚未完成意图判断" in blocked.text
    install_intent(monkeypatch, "requirements")
    retried = client.post(f"/conversations/{cid}/messages", json={"text": text})
    assert "已记录这次写作要求" in retried.text
    current = client.get(f"/conversations/{cid}").json()
    assert current["pending_requirement_messages"] == []
    assert current["requirements"]["entries"][-1]["text"] == text
    assert current["messages"][2]["requirement"]["resolved_by"] == "message:4"


def test_consultation_does_not_become_title_for_a_later_generate_command(client, monkeypatch):
    cid, initial = begin(client)
    install_intent(monkeypatch, "chat")
    client.post(
        f"/conversations/{cid}/messages", json={"text": "为什么目录里有审批管理这一章？"}
    )
    response = client.post(
        f"/conversations/{cid}/messages",
        json={"text": "根据已上传资料和我们的讨论规划目录", "action": "generate"},
    )
    assert "directory.review" in response.text
    latest = client.get(f"/conversations/{cid}").json()["directory_review"]
    assert latest["title"] == initial["directory_review"]["title"]
    assert latest["requirements"] == initial["requirements"]
