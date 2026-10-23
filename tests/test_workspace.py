import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from folio.api.app import create_app
from folio.knowledge.local import LocalKnowledge
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime


@pytest.fixture(autouse=True)
def stub_routing(monkeypatch):
    from folio.api.workspace import detect_intent
    from folio.decisions import IntentDecision

    async def intent(*args):
        return IntentDecision(intent="chat", summary="test")

    monkeypatch.setattr("folio.api.workspace.detect_intent", intent)
    return detect_intent


def configured_app(root):
    ModelSettingsStore(root).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    return create_app(root)


def test_greeting_stream_is_real_chat_not_generation(tmp_path, monkeypatch):
    calls = []

    async def chat(config, messages, attachments, workflow=None, **kwargs):
        calls.append((messages, attachments))
        yield "message.delta", {"text": "你好"}
        yield "message.completed", {"text": "你好，很高兴与你一起整理需求。"}

    monkeypatch.setattr("folio.api.workspace.chat_events", chat)
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        conversation_id = client.post("/conversations").json()["id"]
        response = client.post(f"/conversations/{conversation_id}/messages", json={"text": "你好"})
        assert response.status_code == 200
        assert "message.delta" in response.text
        assert "你好，很高兴" in response.text
        assert "artifact.ready" not in response.text and "work.update" not in response.text
        assert "演示" not in response.text
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        saved = client.get(f"/conversations/{conversation_id}").json()
        assert len(saved["messages"]) == 2 and saved["active_job"] is None
        assert calls[0][0][0]["content"] == "你好" and calls[0][1] == []


def test_missing_config_or_materials_do_not_fake_success(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        cid = client.post("/conversations").json()["id"]
        assert (
            client.post(f"/conversations/{cid}/messages", json={"text": "你好"}).status_code == 409
        )
        ModelSettingsStore(tmp_path).save(
            ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
        )
        response = client.post(
            f"/conversations/{cid}/messages", json={"text": "生成文档", "action": "generate"}
        )
        assert response.status_code == 422
        assert client.get(f"/conversations/{cid}").json()["messages"] == []


def test_upload_generate_download_and_resume(tmp_path, inputs, monkeypatch):
    captured = []

    class PlanningRuntime(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "plan":
                result.content = json.dumps(json.loads(result.content) | {
                    "document_title": "交易审批流程说明书",
                }, ensure_ascii=False)
            return result

    async def execute(store, job_id):
        captured.append(store.get(job_id)["input"])
        return await Engine(
            store, DemoRuntime(), LocalKnowledge(Path(store.get(job_id)["input"]["knowledge_path"]))
        ).run(job_id)

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", lambda _: PlanningRuntime())
    app = configured_app(tmp_path / "web")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        files = []
        for role, path in [("source", Path(inputs["knowledge_path"]) / "rules.md")]:
            response = client.post(
                "/uploads", params={"filename": path.name}, content=path.read_bytes()
            )
            assert response.status_code == 201
            assert "path" not in response.json()
            files.append({"id": response.json()["id"], "role": role})
        response = client.post(
            f"/conversations/{cid}/messages",
            json={
                "text": "生成文档，重点写清审批流程。",
                "action": "generate",
                "files": files,
            },
        )
        assert "directory.review" in response.text and "job.created" not in response.text
        assert (
            client.get(f"/conversations/{cid}").json()["directory_review"]["phase"]
            == "directory_review"
        )
        response = confirm_selection(client, cid)
        assert "job.created" in response.text and "work.update" in response.text
        assert "artifact.ready" in response.text
        artifact = next(
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and '"download_url"' in line
        )
        assert client.get(artifact["download_url"]).content.startswith(b"PK")
        assert "重点写清审批流程" in captured[0]["user_request"]
        assert captured[0]["sample_paths"] == [] and captured[0]["reference_paths"] == []
        assert captured[0]["runtime"] == "llm"
        assert (
            captured[0]["writing_guide"] is None
            and captured[0]["title"] == "交易审批流程说明书"
        )
        assert artifact["filename"] == "交易审批流程说明书.docx"
        state = client.get(f"/conversations/{cid}").json()
        assert state["active_job"] == artifact["job_id"]
        assert "path" not in json.dumps(state["files"])
        assert "artifact.ready" in client.get(f"/jobs/{artifact['job_id']}/stream").text
        assert client.get("/jobs/missing/stream").status_code == 404


def test_upload_rejects_bad_files(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/uploads?filename=x.exe", content=b"x").status_code == 422
        assert client.post("/uploads?filename=x.docx", content=b"not a zip").status_code == 422
        assert client.post("/uploads?filename=x.md", content=b"").status_code == 422
        assert client.post("/uploads?filename=x.md", content=b"\xff").status_code == 422


def test_upload_accepts_large_source_and_prepares_structure(tmp_path):
    source = tmp_path / "large.png"
    Image.new("RGB", (8, 8), "white").save(source)
    size = 20 * 1024 * 1024 + 1
    with source.open("r+b") as stream:
        stream.truncate(size)
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        cid = client.post("/conversations").json()["id"]
        with source.open("rb") as stream:
            response = client.post(
                f"/conversations/{cid}/sources?filename=large.png",
                content=iter(lambda: stream.read(1024 * 1024), b""),
            )
        assert response.status_code == 201, response.text
        uploaded = response.json()
        assert uploaded["size"] == size
        assert client.get(f"/conversations/{cid}").json()["files"][0]["id"] == uploaded["id"]
        folder = root / "uploads" / uploaded["id"]
        assert (folder / "source.png").stat().st_size == size
        assert json.loads((folder / "structure.json").read_text())["format"] == "png"
        assert (folder / "structure.md").is_file()


def test_classified_model_failure_is_safe_and_persisted(tmp_path, monkeypatch):
    async def failing_chat(*args, **kwargs):
        raise RuntimeError("401 authentication failed: secret-upstream-body")
        yield  # async generator contract

    monkeypatch.setattr("folio.api.workspace.chat_events", failing_chat)
    with TestClient(configured_app(tmp_path)) as client:
        cid = client.post("/conversations").json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"text": "你好"})
        assert '"code": "authentication"' in response.text
        assert "secret-upstream-body" not in response.text
        state = client.get(f"/conversations/{cid}").json()
        assert state["messages"][-1]["error_code"] == "authentication"
        assert "secret-upstream-body" not in json.dumps(state)


async def test_chat_uses_runtime_model_and_persists_transcript(monkeypatch, tmp_path):
    from folio.api.workspace import chat_events

    observed = []

    class Runtime:
        model = "configured-model-identity"

        def __init__(self, *args):
            pass

        async def stream(self, messages, **kwargs):
            observed.append(messages)
            yield "text.delta", {"text": self.model}
            yield "completed", {"text": self.model}

        async def aclose(self):
            observed.append("closed")

    monkeypatch.setattr("folio.api.workspace.LLMRuntime", Runtime)
    events = [
        event
        async for event in chat_events(
            tmp_path / "config", [{"role": "user", "content": "你用的什么大模型"}], []
        )
    ]
    assert '"runtime_model": "configured-model-identity"' in observed[0][0]["content"]
    assert observed[0][1] == {"role": "user", "content": "你用的什么大模型"}
    assert observed[-1] == "closed"
    assert [kind for kind, _ in events] == ["message.delta", "message.completed"]


def test_natural_language_generation_and_file_reuse(tmp_path, inputs, monkeypatch):
    from folio.decisions import IntentDecision

    async def intent(*args):
        return IntentDecision(intent="generate", summary="用户明确要求开始")

    async def execute(store, job_id):
        return await Engine(
            store, DemoRuntime(), LocalKnowledge(Path(store.get(job_id)["input"]["knowledge_path"]))
        ).run(job_id)

    monkeypatch.setattr("folio.api.workspace.detect_intent", intent)
    monkeypatch.setattr("folio.api.workspace.execute", execute)
    with TestClient(configured_app(tmp_path / "ui")) as client:
        cid = client.post("/conversations").json()["id"]
        files = []
        for role in ("source",):
            path = Path(inputs["knowledge_path"]) / "rules.md"
            file = client.post(
                "/uploads", params={"filename": path.name}, content=path.read_bytes()
            ).json()
            files.append({"id": file["id"], "role": role})
        response = client.post(
            f"/conversations/{cid}/messages", json={"text": "开始生成", "files": files}
        )
        assert response.status_code == 200 and "directory.review" in response.text
        assert "job.created" not in response.text
        response = confirm_selection(client, cid)
        assert response.status_code == 200 and "artifact.ready" in response.text
        assert "job.created" in response.text


def test_chat_reads_material_structure_without_sample_facts(inputs):
    from folio.api.workspace import material_context

    parsed = material_context(
        [
            {
                "id": "a",
                "name": "rules.md",
                "role": "source",
                "path": str(Path(inputs["knowledge_path"]) / "rules.md"),
            },
        ]
    )
    assert "交易撤销" in json.dumps(parsed, ensure_ascii=False)
    assert "path" not in parsed[0] and "parsed" in parsed[0]


def test_routing_failure_never_starts_job_or_falls_back_silently(tmp_path, monkeypatch):
    async def broken_intent(*args):
        raise TimeoutError()

    monkeypatch.setattr("folio.api.workspace.detect_intent", broken_intent)
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"text": "开始生成"})
        assert '"code": "timeout"' in response.text
        assert "job.created" not in response.text
        assert client.get(f"/conversations/{cid}").json()["messages"][-1]["error_code"] == "timeout"
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def confirm_selection(client, cid, **extra):
    selection = client.get(f"/conversations/{cid}").json()["directory_review"]
    assert selection["phase"] == "directory_review"
    return client.post(
        f"/conversations/{cid}/messages",
        json={
            "text": "目录没问题，继续生成",
            "action": "confirm_directory",
            "selection_revision": selection["revision"],
            **extra,
        },
    )


async def test_intent_receives_latest_request_and_real_attachments(
    tmp_path, monkeypatch, stub_routing
):
    from folio.runtime.base import RuntimeResult

    captured = []

    class Runtime:
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            captured.append(context)
            return RuntimeResult(json.dumps({"intent": "generate", "summary": "开始推荐模板"}))

        async def aclose(self):
            pass

    monkeypatch.setattr("folio.api.workspace.LLMRuntime", Runtime)
    workflow = {"files": [{"id": "a", "name": "大纲.docx", "role": "outline"}], "selection": None}
    result = await stub_routing(
        tmp_path / "config",
        [
            {"role": "assistant", "content": "请上传大纲。"},
            {"role": "user", "content": "基于这个大纲来写需求说明书"},
        ],
        workflow,
    )
    assert result.intent == "generate"
    assert captured[0]["latest_user_message"] == "基于这个大纲来写需求说明书"
    assert captured[0]["workflow"] == workflow


def test_chat_send_plans_directory_directly_from_saved_sources(tmp_path, inputs, monkeypatch):
    from folio.decisions import IntentDecision

    received = []

    async def intent(config, history, workflow):
        received.append(workflow)
        assert workflow["files"][0]["role"] == "source"
        assert history[-1]["content"] == "编写交易需求说明书"
        return IntentDecision(intent="generate", summary="用户要求生成文档")

    async def chat(*args):
        pytest.fail("Generation request must not become a chat reply")
        yield

    monkeypatch.setattr("folio.api.workspace.detect_intent", intent)
    monkeypatch.setattr("folio.api.workspace.chat_events", chat)
    app = configured_app(tmp_path / "ui")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        client.post(
            f"/conversations/{cid}/sources?filename=资料.md",
            content=(Path(inputs["knowledge_path"]) / "rules.md").read_bytes(),
        )
        response = client.post(
            f"/conversations/{cid}/messages", json={"text": "编写交易需求说明书", "action": "chat"}
        )
        assert "directory.review" in response.text and "job.created" not in response.text
        state = client.get(f"/conversations/{cid}").json()
        assert state["directory_review"]["phase"] == "directory_review"
        assert state["last_intent"]["intent"] == "generate"
        assert received[0]["generation_requires_confirmation"] is True
        assert "knowledge_engineering" not in received[0]
        with app.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_workspace_serves_built_assets_and_disables_html_cache(tmp_path):
    import re

    import folio.api.app as module

    web = module.WEB_DIST
    assert (web / "index.html").is_file(), module.UI_BUILD_INSTRUCTIONS
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assets = re.findall(r'(?:src|href)="(/ui/assets/[^\"]+\.(?:js|css))"', response.text)
        assert any(url.endswith(".js") for url in assets)
        assert any(url.endswith(".css") for url in assets)
        for url in assets:
            assert (web / url.removeprefix("/ui/")).is_file()
            asset = client.get(url)
            assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"


def test_missing_ui_build_gives_instructions_without_disabling_api(tmp_path, monkeypatch):
    import folio.api.app as module

    monkeypatch.setattr(module, "WEB_DIST", tmp_path / "missing-dist")
    with TestClient(create_app(tmp_path / "data")) as client:
        response = client.get("/")
        assert response.status_code == 503
        assert "npm --prefix frontend ci" in response.text
        assert "npm --prefix frontend run build" in response.text
        assert client.get("/ui/assets/missing.js").status_code == 503
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/conversations").status_code == 201


@pytest.fixture(autouse=True)
def directory_runtime(monkeypatch):
    from folio.runtime.demo import DemoRuntime

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", lambda config: DemoRuntime())
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )
