import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from folio.api.app import create_app
from folio.api.workspace import chat_events, detect_intent
from folio.document.guidance import load_guide
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.source_documents import SourceDirectoryDrafts


def test_new_legacy_and_frozen_conversation_guides(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        guides = client.get("/writing-guides").json()["guides"]
        assert {g["id"] for g in guides} == {"urd", "srs"}
        assert all(set(g) == {"id", "name", "purpose", "version"} for g in guides)
        assert client.post("/conversations").json()["writing_guide"] is None
        assert client.post("/conversations", json={}).json()["writing_guide"] is None
        created = client.post("/conversations", json={"guide_id": "urd"}).json()
        cid, snapshot = created["id"], created["writing_guide"]
        assert snapshot["id"] == "urd"
        assert (
            client.post("/conversations", json={"guide_id": "srs"}).json()["writing_guide"]["id"]
            == "srs"
        )
        assert (
            client.post("/conversations", json={"guide_id": None}).json()["writing_guide"] is None
        )
        legacy_id = "a" * 32
        with app.state.store.connect() as conn:
            conn.execute(
                "INSERT INTO web_conversations VALUES(?,?)",
                (legacy_id, json.dumps({"messages": [], "files": [], "active_job": None})),
            )

    # Reads and selecting the same id never reload a newer configuration.
    def unexpected_load(*args):
        raise AssertionError("A saved snapshot must not be reloaded")

    monkeypatch.setattr("folio.api.workspace.load_guide", unexpected_load)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/conversations/{cid}").json()["writing_guide"] == snapshot
        response = client.put(f"/conversations/{cid}/writing-guide", json={"guide_id": "urd"})
        assert response.status_code == 200
        assert response.json() == {"writing_guide": snapshot, "selection_invalidated": False}
        legacy = client.get(f"/conversations/{legacy_id}").json()
        assert "writing_guide" not in legacy
        assert (
            client.put(
                f"/conversations/{legacy_id}/writing-guide", json={"guide_id": None}
            ).status_code
            == 200
        )


def test_guide_change_is_locked_and_accepts_only_server_ids(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations", json={"guide_id": "urd"}).json()["id"]
        path = f"/conversations/{cid}/writing-guide"
        assert client.put(path, json={"guide_id": "srs", "writing_guide": {}}).status_code == 422
        assert client.put(path, json={"guide_id": "../../outside"}).status_code == 422
        assert client.post("/conversations", json={"writing_guide": {}}).status_code == 422
        assert (
            client.put(f"/conversations/{cid}/draft", json={"writing_guide": {}}).status_code == 422
        )
        assert (
            client.post(
                f"/conversations/{cid}/messages", json={"text": "x", "writing_guide": {}}
            ).status_code
            == 422
        )
        assert (
            client.put(
                path, json={"guide_id": "srs"}, headers={"Origin": "http://foreign.test"}
            ).status_code
            == 403
        )
        with FileLock(tmp_path / f"conversation-{cid}.lock"):
            assert client.put(path, json={"guide_id": "srs"}).status_code == 409
        state = client.get(f"/conversations/{cid}").json()
        state["active_job"] = "busy-job"
        with app.state.store.connect() as conn:
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
        monkeypatch.setattr(app.state.store, "get", lambda job_id: {"status": "running"})
        assert client.put(path, json={"guide_id": "srs"}).status_code == 409
        assert client.get(f"/conversations/{cid}").json()["writing_guide"]["id"] == "urd"
        monkeypatch.setattr(app.state.store, "get", lambda job_id: {"status": "published"})
        assert client.put(path, json={"guide_id": "srs"}).status_code == 200
        assert client.get(f"/conversations/{cid}").json()["active_job"] == "busy-job"


@pytest.fixture
def guide_app(tmp_path, monkeypatch):
    contexts = []

    class Planner(DemoRuntime):
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            contexts.append((task, context))
            if task == "revise_plan":
                return RuntimeResult(
                    json.dumps(
                        {
                            "operations": [
                                {
                                    "op": "update",
                                    "node_id": context["previous_directory"][0]["node_id"],
                                    "field": "title",
                                    "value_json": json.dumps(context["revision_request"]),
                                }
                            ],
                            "summary": "调整目录标题",
                        }
                    )
                )
            return await super().complete(task, context, schema, session_key)

    async def execute(store, job_id):
        store.update(job_id, status="failed", error="test worker stopped")

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", Planner)
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )
    monkeypatch.setattr("folio.api.workspace.execute", execute)
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    return create_app(tmp_path), contexts


def generate_directory(client, inputs, cid):
    for path in (
        (Path(inputs["knowledge_path"]) / "rules.md"),
        Path(inputs["knowledge_path"]) / "rules.md",
    ):
        uploaded = client.post(
            f"/conversations/{cid}/sources",
            params={"filename": path.name},
            content=path.read_bytes(),
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["role"] == "source"
    guide = client.get(f"/conversations/{cid}").json().get("writing_guide")
    response = client.post(
        f"/conversations/{cid}/messages",
        json={"text": guide["name"] if guide else "外汇交易需求说明书", "action": "generate"},
    )
    assert response.status_code == 200
    assert "directory.review" in response.text and "job.created" not in response.text
    assert "template.selection" not in response.text and "message.failed" not in response.text
    return client.get(f"/conversations/{cid}").json()["directory_review"]


def test_guide_snapshot_flows_through_directory_revisions_and_job(guide_app, inputs, monkeypatch):
    app, contexts = guide_app
    with TestClient(app) as client:
        created = client.post("/conversations", json={"guide_id": "srs"}).json()
        cid, guide = created["id"], created["writing_guide"]
        selection = generate_directory(client, inputs, cid)
        assert selection["writing_guide"] == guide
        assert selection["directory"]["writing_guide"] == guide
        drafts = SourceDirectoryDrafts(app.state.store.root, app.state.preview_library)

        def assert_frozen(selected):
            assert selected["writing_guide"] == guide
            assert selected["directory"]["writing_guide"] == guide
            snapshot = drafts.load(selected["directory"]["revision"], cid)
            assert snapshot["writing_guide"] == guide
            assert snapshot["source_ids"] == [source["id"] for source in selected["files"]]
            assert len(list(snapshot["knowledge_path"].iterdir())) == 2

        assert_frozen(selection)

        def unexpected_load(*args):
            raise AssertionError("Directory changes must use the frozen guide")

        monkeypatch.setattr("folio.api.workspace.load_guide", unexpected_load)

        def action(name, **extra):
            selected = client.get(f"/conversations/{cid}").json()["directory_review"]
            return client.post(
                f"/conversations/{cid}/messages",
                json={
                    "text": name,
                    "action": name,
                    "selection_revision": selected["revision"],
                    **extra,
                },
            )

        assert "directory.review" in action("revise_directory", advice="交易撤销需求").text
        revised = client.get(f"/conversations/{cid}").json()["directory_review"]
        assert revised["revision"] != selection["revision"]
        assert revised["directory"]["sections"][0]["title"] == "交易撤销需求"
        assert_frozen(revised)
        assert "directory.review" in action("regenerate_directory").text
        regenerated = client.get(f"/conversations/{cid}").json()["directory_review"]
        assert regenerated["revision"] != revised["revision"]
        assert regenerated["directory"]["revision_history"] == []
        assert_frozen(regenerated)
        assert {task for task, _ in contexts} >= {"plan", "revise_plan"}
        assert all(
            "writing_guidance" in context
            for task, context in contexts
            if task in {"plan", "revise_plan"}
        )
        assert "job.created" in action("confirm_directory").text
        job_id = client.get(f"/conversations/{cid}").json()["active_job"]
        job_input = app.state.store.get(job_id)["input"]
        assert job_input["writing_guide"] == guide
        assert job_input["title"] == "软件需求规格说明书"


def test_changing_type_revokes_old_directory_confirmation(guide_app, inputs):
    app, _ = guide_app
    with TestClient(app) as client:
        cid = client.post("/conversations", json={"guide_id": "urd"}).json()["id"]
        selected = generate_directory(client, inputs, cid)
        before = client.get(f"/conversations/{cid}").json()
        same = client.put(f"/conversations/{cid}/writing-guide", json={"guide_id": "urd"})
        assert not same.json()["selection_invalidated"]
        assert client.get(f"/conversations/{cid}").json()["directory_review"] == selected
        changed = client.put(f"/conversations/{cid}/writing-guide", json={"guide_id": "srs"})
        assert changed.json()["selection_invalidated"]
        state = client.get(f"/conversations/{cid}").json()
        assert state["writing_guide"]["id"] == "srs" and "directory_review" not in state
        assert state["messages"] == before["messages"] and state["files"] == before["files"]
        stale = client.post(
            f"/conversations/{cid}/messages",
            json={
                "text": "继续",
                "action": "confirm_directory",
                "selection_revision": selected["revision"],
            },
        )
        assert stale.status_code == 409
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert (
            "directory.review"
            in client.post(
                f"/conversations/{cid}/messages",
                json={"text": "重新生成目录", "action": "generate"},
            ).text
        )
        latest = client.get(f"/conversations/{cid}").json()["directory_review"]
        assert latest["revision"] != selected["revision"]
        assert latest["writing_guide"]["id"] == "srs"
        assert latest["directory"]["writing_guide"]["id"] == "srs"


def test_directory_snapshot_mismatch_cannot_start_job(guide_app, inputs):
    app, _ = guide_app
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        selected = generate_directory(client, inputs, cid)
        path = (
            app.state.store.root
            / "directory-drafts"
            / selected["directory"]["revision"]
            / "metadata.json"
        )
        data = json.loads(path.read_text())
        data["writing_guide"] = load_guide("srs").model_dump(mode="json")
        path.write_text(json.dumps(data))
        response = client.post(
            f"/conversations/{cid}/messages",
            json={
                "text": "继续",
                "action": "confirm_directory",
                "selection_revision": selected["revision"],
            },
        )
        assert response.status_code == 409
        assert "重新生成目录" in response.json()["detail"]
        assert client.get(f"/conversations/{cid}").json()["active_job"] is None
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_chat_and_intent_do_not_repeat_frozen_guide_snapshots(monkeypatch):
    guide = load_guide("srs").model_dump(mode="json")
    workflow = {
        "writing_guide": guide,
        "directory_review": {
            "phase": "directory_review",
            "revision": "saved-version",
            "writing_guide": guide,
            "directory": {"revision": "directory-version", "writing_guide": guide},
        },
    }
    before = json.dumps(workflow, ensure_ascii=False)
    captured = {}

    class Runtime:
        model = "test-model"

        def __init__(self, config):
            pass

        async def stream(self, transcript):
            captured["chat"] = transcript[0]["content"]
            yield "completed", {"text": "你好"}

        async def aclose(self):
            pass

    class Provider:
        def __init__(self, runtime):
            pass

        async def decide(self, task, context, schema, key):
            captured["intent"] = context["workflow"]
            return RuntimeResult(json.dumps({"intent": "chat", "summary": "普通对话"}))

    monkeypatch.setattr("folio.api.workspace.LLMRuntime", Runtime)
    monkeypatch.setattr("folio.api.workspace.LLMDecisionProvider", Provider)
    messages = [{"role": "user", "content": "这是什么类型文档"}]
    assert (await detect_intent(None, messages, workflow)).intent == "chat"
    events = [event async for event in chat_events(None, messages, [], workflow)]
    assert events[-1][1]["text"] == "你好"
    assert set(captured["intent"]["writing_guide"]) == {"id", "version", "name", "purpose"}
    assert "writing_guide" not in captured["intent"]["directory_review"]
    assert "writing_guide" not in captured["intent"]["directory_review"]["directory"]
    assert captured["chat"].count('"writing_guide"') == 1
    assert '"guidance_policy"' not in captured["chat"]
    assert json.dumps(workflow, ensure_ascii=False) == before
