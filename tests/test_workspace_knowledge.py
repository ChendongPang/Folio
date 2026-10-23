"""Source uploads have no knowledge-base state; explicit RAGFlow stays available."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.knowledge import KnowledgeCatalogue


def test_source_workflow_never_selects_or_contacts_external_knowledge(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Source uploads must not contact an external dataset service")

    monkeypatch.setattr("folio.api.knowledge.list_datasets", unexpected)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/conversations").json()
        cid = created["id"]
        assert client.get(f"/conversations/{cid}").json()["files"] == []
        assert "knowledge_engineering" not in created
        assert "knowledge_selection" not in created
        added = client.post(
            f"/conversations/{cid}/sources?filename=facts.md", content="# 资料\n真实内容"
        )
        assert added.status_code == 201
        assert "knowledge_engineering" not in added.json()
    with TestClient(create_app(tmp_path)) as client:
        restored = client.get(f"/conversations/{cid}").json()
        assert len(restored["files"]) == 1
        assert restored["files"][0]["id"] == added.json()["id"]
        assert "knowledge_engineering" not in restored
        assert "knowledge_selection" not in restored
        removed = client.delete(f"/conversations/{cid}/sources/{added.json()['id']}")
        assert removed.status_code == 200
        assert "knowledge_engineering" not in removed.json()
        assert client.get(f"/conversations/{cid}").json()["files"] == []


def test_legacy_dataset_state_does_not_activate_an_external_service(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        with app.state.store.connect() as conn:
            state = json.loads(
                conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0]
            )
            state["knowledge_selection"] = {
                "dataset_ids": ["legacy-private"],
                "datasets": [{"name": "Old", "id": "legacy-private"}],
            }
            state["knowledge_engineering"] = {
                "status": "not_implemented",
                "wiki": {"status": "not_implemented"},
                "rag": {"status": "not_implemented"},
            }
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
        restored = client.get(f"/conversations/{cid}").json()
        assert "knowledge_selection" not in restored
        assert "knowledge_engineering" not in restored


def test_catalogue_requires_explicit_connection_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("FOLIO_RAGFLOW_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        result = client.get("/knowledge/datasets")
        assert result.status_code == 503
        assert "FOLIO_RAGFLOW_URL" in result.text
        assert client.post("/conversations").status_code == 201


@pytest.mark.parametrize("timeout", [False, True])
def test_explicit_catalogue_service_failure_is_safe(tmp_path, monkeypatch, timeout):
    async def unavailable(*args, **kwargs):
        if timeout:
            await asyncio.sleep(1)
        raise OSError("internal backend details")

    monkeypatch.setenv("FOLIO_RAGFLOW_URL", "http://configured.test/mcp")
    monkeypatch.setattr("folio.api.knowledge.list_datasets", unavailable)
    monkeypatch.setattr("folio.api.knowledge.CATALOGUE_TIMEOUT", 0.01)
    with TestClient(create_app(tmp_path)) as client:
        result = client.get("/knowledge/datasets")
        assert result.status_code == (504 if timeout else 503)
        assert "internal backend" not in result.text


async def test_catalogue_pagination_and_cache(monkeypatch):
    calls = []

    async def pages(url, *, page):
        calls.append((url, page))
        return {
            "datasets": [
                {"id": str(i), "name": f"库{i}", "description": None}
                for i in (range(100) if page == 1 else [100])
            ],
            "total": None,
        }

    monkeypatch.setattr("folio.api.knowledge.list_datasets", pages)
    catalogue = KnowledgeCatalogue("http://configured.test/mcp/")
    first = await catalogue.page(1)
    assert first["has_more"] and first["total"] is None
    assert first["datasets"][0]["description"] == ""
    last = await catalogue.page(2)
    assert not last["has_more"] and last["page"] == 2
    assert await catalogue.page(1) == first
    assert await catalogue.page(2) == last
    first["datasets"][0]["name"] = "mutated"
    assert (await catalogue.page(1))["datasets"][0]["name"] == "库0"
    assert calls == [("http://configured.test/mcp", 1), ("http://configured.test/mcp", 2)]
