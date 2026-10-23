import json

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from folio.api.app import create_app


def test_custom_title_survives_draft_updates_and_restart(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        title = "文" * 200
        changed = client.patch(f"/conversations/{cid}", json={"title": f"  {title}  "})
        assert changed.status_code == 200
        assert changed.json()["title"] == title
        assert client.put(f"/conversations/{cid}/draft", json={"text": "不同的自动标题"}).status_code == 200
        assert client.get("/conversations").json()["conversations"][0]["title"] == title
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/conversations/{cid}").json()["title"] == title


def test_pinning_orders_all_pages_without_changing_activity_time(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        ids = [client.post("/conversations").json()["id"] for _ in range(3)]
        with app.state.store.connect() as conn:
            for index, cid in enumerate(ids):
                conn.execute("UPDATE web_conversations SET data=json_set(data,'$.updated_at',?) WHERE id=?", (f"2026-10-{10 + index}T00:00:00Z", cid))
        before = client.get(f"/conversations/{ids[0]}").json()["updated_at"]
        pinned = client.patch(f"/conversations/{ids[0]}", json={"pinned": True}).json()
        assert pinned["pinned"] and pinned["updated_at"] == before
        first = client.get("/conversations?limit=1").json()
        assert first["has_more"] and first["conversations"][0]["id"] == ids[0]
        rest = client.get("/conversations?offset=1").json()["conversations"]
        assert [row["id"] for row in rest] == ids[:0:-1]
        assert client.get(f"/conversations/{ids[0]}").json()["pinned"] is True
        client.patch(f"/conversations/{ids[0]}", json={"pinned": False})
        assert [row["id"] for row in client.get("/conversations").json()["conversations"]] == ids[::-1]


@pytest.mark.parametrize("body", [{}, {"title": "  "}, {"title": "x" * 201}, {"title": None}, {"pinned": None}, {"files": []}])
def test_invalid_metadata_does_not_change_session(tmp_path, body):
    with TestClient(create_app(tmp_path)) as client:
        cid = client.post("/conversations").json()["id"]
        before = client.get(f"/conversations/{cid}").json()
        assert client.patch(f"/conversations/{cid}", json=body).status_code == 422
        assert client.get(f"/conversations/{cid}").json() == before


def test_metadata_requires_origin_and_session_lock(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        assert client.patch(f"/conversations/{cid}", json={"title": "改名"}, headers={"Origin": "https://foreign.test"}).status_code == 403
        with FileLock(tmp_path / f"conversation-{cid}.lock"):
            assert client.patch(f"/conversations/{cid}", json={"pinned": True}).status_code == 409
        assert client.patch(f"/conversations/{'f' * 32}", json={"title": "不存在"}).status_code == 404
        with app.state.store.connect() as conn:
            state = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
        assert "title" not in state and "pinned" not in state
