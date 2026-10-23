import json

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from folio.api.app import create_app


def test_history_sources_and_text_draft_survive_app_restart(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        response = client.post(
            f"/conversations/{cid}/sources?filename=business.md", content="# 业务资料"
        )
        assert response.status_code == 201
        upload = response.json()
        saved_files = client.get(f"/conversations/{cid}").json()["files"]
        assert saved_files == [{key: upload[key] for key in ("id", "name", "size", "role")}]
        assert saved_files[0]["role"] == "source"
        draft = {"text": "编写交易需求"}
        assert client.put(f"/conversations/{cid}/draft", json=draft).status_code == 200
        empty_id = client.post("/conversations").json()["id"]
    with TestClient(create_app(tmp_path)) as client:
        history = client.get("/conversations").json()["conversations"]
        assert len(history) == 2
        assert next(c for c in history if c["id"] == empty_id)["title"] == "新文档"
        item = next(c for c in history if c["id"] == cid)
        assert item["title"] == draft["text"] and item["updated_at"]
        state = client.get(f"/conversations/{cid}").json()
        assert state["draft"] == draft["text"]
        assert state["files"] == saved_files
        assert "path" not in state["files"][0]
        assert state["messages"] == []
        assert "knowledge_engineering" not in state
        assert (tmp_path / "uploads" / upload["id"] / "source.md").is_file()
        assert client.put(f"/conversations/{cid}/draft", json={"text": ""}).status_code == 200
        assert client.get(f"/conversations/{cid}").json()["files"] == saved_files


@pytest.mark.parametrize(
    ("workflow_key", "preserved"), [("template_selection", False), ("directory_review", True)]
)
def test_history_pagination_and_draft_preserve_messages_and_current_directory(
    tmp_path, workflow_key, preserved
):
    app = create_app(tmp_path)
    legacy = {
        "messages": [
            {"role": "user", "content": "旧会话"},
            {"role": "assistant", "content": "已有目录等待确认", "interrupted": False},
        ],
        "files": [],
        "active_job": None,
        workflow_key: {"phase": "directory_review", "revision": "b" * 32},
    }
    with TestClient(app) as client:
        with app.state.store.connect() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO web_conversations VALUES(?,?)", (f"{i:032x}", json.dumps(legacy))
                )
        page = client.get("/conversations?limit=2").json()
        assert page["has_more"] and len(page["conversations"]) == 2
        more = client.get("/conversations?limit=2&offset=2").json()
        assert not more["has_more"] and len(more["conversations"]) == 1
        assert len({c["id"] for c in page["conversations"] + more["conversations"]}) == 3
        assert all(c["message_count"] == 2 for c in page["conversations"])
        cid = f"{0:032x}"
        loaded = client.get(f"/conversations/{cid}").json()
        assert loaded["messages"] == legacy["messages"]
        if preserved:
            assert loaded[workflow_key] == legacy[workflow_key]
        else:
            assert workflow_key not in loaded
        assert (
            client.put(f"/conversations/{cid}/draft", json={"text": "新的草稿"}).status_code == 200
        )
        restored = client.get(f"/conversations/{cid}").json()
        assert restored["messages"] == legacy["messages"]
        assert restored["draft"] == "新的草稿"
        if preserved:
            assert restored[workflow_key] == legacy[workflow_key]
        else:
            assert workflow_key not in restored
        assert client.get("/conversations").json()["conversations"][0]["id"] == cid
        assert client.get("/conversations").json()["conversations"][0]["title"] == "旧会话"
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/conversations/{cid}").json() == restored


def test_draft_respects_lock_and_attachment_validation(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        with FileLock(tmp_path / f"conversation-{cid}.lock"):
            assert (
                client.put(f"/conversations/{cid}/draft", json={"text": "overwrite"}).status_code
                == 409
            )
        assert client.get(f"/conversations/{cid}").json()["draft"] == ""
        assert (
            client.put(
                f"/conversations/{cid}/draft", json={"files": [{"id": "f" * 32, "role": "source"}]}
            ).status_code
            == 422
        )
        long_draft = "x" * 600000
        assert client.put(f"/conversations/{cid}/draft", json={"text": long_draft}).status_code == 200
        assert client.get(f"/conversations/{cid}").json()["draft"] == long_draft
        assert (
            client.put(f"/conversations/{cid}/draft", json={"text": "x" * 600001}).status_code == 422
        )
        assert client.get(f"/conversations/{cid}").json()["draft"] == long_draft
        assert (
            client.put(
                f"/conversations/{cid}/draft",
                json={"text": "x"},
                headers={"Origin": "http://foreign.test"},
            ).status_code
            == 403
        )
        assert client.put(f"/conversations/{'f' * 32}/draft", json={}).status_code == 404
