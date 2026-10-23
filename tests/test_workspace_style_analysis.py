"""A style consultation runs real bounded extraction without creating a document."""

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from folio.api import workspace
from folio.api.app import create_app
from folio.decisions import IntentDecision
from folio.document.style_profile import style_messages
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.requirements import RequirementSnapshot
from folio.runtime.activity import ModelActivity
from folio.runtime.base import RuntimeResult
from folio.runtime.errors import ModelCallFailed

REAL_DETECT_INTENT = workspace.detect_intent


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ModelSettingsStore(tmp_path).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    calls, chats = [], []

    class StyleRuntime:
        config = {}

        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            assert task == "style_analysis"
            assert context["user_request"]
            assert context["user_request"] in style_messages(context, schema)[1]["content"]
            text = context["style_analysis_text"]
            calls.append(text)
            ids = re.findall(r"### (D\d+-S\d+) /[^\n]*\n\nType: paragraph", text)
            assert ids
            return RuntimeResult(json.dumps({
                "rules": [{"dimension": dimension, "scope": "body", "description": description,
                           "sample_ids": ids[:2], "confidence": "observed"}
                          for dimension, description in {
                              "writing": "表达客观，使用分项说明组织长句。",
                              "structure": "以多级小节组织整体说明。",
                              "organization": "每节先交代背景再解释细节。",
                              "detail": "正文使用完整句子展开论述。",
                              "headings": "标题用简短主题词命名。",
                              "expression": "同类概念保持一致的称谓。",
                          }.items()],
                "dimension_checks": [{"dimension": "figures_tables", "status": "not_present", "reason": "已读文字未出现图表说明。", "sample_ids": ids[:2]}],
                "request_sample_ids": [], "uncertainties": [],
            }, ensure_ascii=False))

        async def aclose(self):
            pass

    async def intent(config, messages, workflow):
        return IntentDecision(intent="analyze_style", summary="用户要求分析风格示例")

    async def chat(config, messages, attachments, workflow=None, **kwargs):
        chats.append({"attachments": attachments, "workflow": workflow, "messages": messages})
        assert "表达客观" in workflow["style_analysis"]
        assert "旧公司秘密金额" not in json.dumps(workflow, ensure_ascii=False)
        yield "message.completed", {"text": "这份示例表达客观，采用分项说明。可提取文字已经全部分析。"}

    monkeypatch.setattr("folio.api.style_analysis.LLMRuntime", StyleRuntime)
    monkeypatch.setattr(workspace, "detect_intent", intent)
    monkeypatch.setattr(workspace, "chat_events", chat)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        yield app, client, calls, chats


def upload(client, cid, name="示例.md", role="style_reference"):
    body = "\n\n".join(
        f"## 范文栏目{i}\n\n旧公司秘密金额是88888元。" + "本段采用完整的句子说明观察到的背景与处理方式。" * 10
        for i in range(8)
    )
    response = client.post(f"/conversations/{cid}/sources", params={"filename": name}, content=body)
    assert response.status_code == 201
    source = response.json()
    assert client.patch(f"/conversations/{cid}/sources/{source['id']}", json={"role": role}).status_code == 200
    return source


def test_style_chat_runs_without_directory_preserves_requirements_and_reuses_cache(setup, monkeypatch):
    app, client, calls, chats = setup
    cid = client.post("/conversations").json()["id"]
    upload(client, cid)
    snapshot = RequirementSnapshot.from_text("当前项目需求只覆盖审批流程。")
    review = {"phase": "directory_review", "revision": "d" * 32,
              "directory": {"revision": "e" * 32, "sections": []}, "reason": "已有目录待确认"}
    with app.state.store.connect() as conn:
        state = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
        state.update(requirements=snapshot.model_dump(mode="json"), directory_review=review)
        conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
    # Cache reuse is valid for the same request. A different request may include
    # a new output-language preference and must not blindly reuse prior prose.
    for question in ("看一下这个风格示例", "看一下这个风格示例"):
        response = client.post(f"/conversations/{cid}/messages", json={"text": question})
        assert "event: style.update" in response.text
        assert "event: style.completed" in response.text
        assert "event: message.completed" in response.text
        assert "event: directory.review" not in response.text and "event: job.created" not in response.text
        saved = client.get(f"/conversations/{cid}").json()
        assert saved["requirements"] == snapshot.model_dump(mode="json")
        assert saved["directory_review"] == review
        assert saved["messages"][-2]["requirement"]["status"] == "discussion"
        report = saved["messages"][-1]["style_analysis"]
        assert saved["messages"][-1]["activity_label"] == "风格分析过程"
        assert client.get(report["style_url"]).status_code == 200
        assert "表达客观" in client.get(report["style_url"]).text
    assert len(calls) == 1
    assert report["cache_hit"] is True
    with app.state.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert not list((app.state.store.root / "directory-drafts").glob("*/metadata.json"))

    async def ordinary_chat(*args, **kwargs):
        return IntentDecision(intent="chat", summary="继续咨询已经得到的风格说明")

    monkeypatch.setattr(workspace, "detect_intent", ordinary_chat)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "解释一下刚才的分项说明"})
    assert "event: style.update" not in response.text and len(calls) == 1
    assert "表达客观" in chats[-1]["workflow"]["style_analysis"]
    other = client.post("/conversations").json()["id"]
    assert client.get(report["style_url"].replace(cid, other)).status_code == 404
    followup = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert followup["style_context_versions"] == report["source_versions"]

    async def expired_chat(config, messages, attachments, workflow=None, **kwargs):
        assert "style_analysis" not in workflow
        assert "这份示例表达客观" not in json.dumps(messages, ensure_ascii=False)
        assert any("do not rely on that conclusion" in message["content"] for message in messages)
        yield "message.completed", {"text": "当前文件用途已经改变。"}

    monkeypatch.setattr(workspace, "chat_events", expired_chat)
    source_id = report["source_versions"][0]["id"]
    client.patch(f"/conversations/{cid}/sources/{source_id}", json={"role": "source"})
    response = client.post(f"/conversations/{cid}/messages", json={"text": "当前文件用途是什么？"})
    assert "event: message.completed" in response.text and len(calls) == 1


def test_missing_style_and_invalid_target_do_not_analyze_or_change_file_roles(setup, monkeypatch):
    _, client, calls, chats = setup
    cid = client.post("/conversations").json()["id"]
    source = upload(client, cid, role="source")
    response = client.post(f"/conversations/{cid}/messages", json={"text": "分析风格"})
    assert "用途设为“风格示例”" in response.text
    assert not calls and not chats
    assert client.get(f"/conversations/{cid}").json()["files"][0]["role"] == "source"
    upload(client, cid, name="另一份.md")

    async def bad_target(*args, **kwargs):
        return IntentDecision(intent="analyze_style", summary="错误选择", style_source_ids=[source["id"]])

    monkeypatch.setattr(workspace, "detect_intent", bad_target)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "分析第一份"})
    assert "event: style.completed" not in response.text
    assert not calls and not chats


def test_saved_style_survives_later_chat_failure(setup, monkeypatch):
    _, client, calls, _ = setup
    cid = client.post("/conversations").json()["id"]
    upload(client, cid)

    async def failed_chat(*args, **kwargs):
        raise ModelCallFailed("private transport body", "timeout")
        yield  # Retain the async iterator interface.

    monkeypatch.setattr(workspace, "chat_events", failed_chat)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "分析风格示例"})
    assert "event: style.completed" in response.text and "event: message.failed" in response.text
    assert "private transport body" not in response.text
    saved = client.get(f"/conversations/{cid}").json()
    assert len(saved["style_analyses"]) == 1 and len(calls) == 1
    assert saved["messages"][-1]["error_diagnostics"]["stage"] == "style_analysis"
    assert client.get(saved["messages"][-1]["style_analysis"]["style_url"]).status_code == 200


def test_progress_is_emitted_before_intent_finishes_and_keeps_each_stage(setup, monkeypatch):
    _, client, _, _ = setup
    cid = client.post("/conversations").json()["id"]
    upload(client, cid)
    events = []
    original_sse = workspace.sse

    def capture(kind, data):
        events.append((kind, data))
        return original_sse(kind, data)

    async def classify(*args, **kwargs):
        assert any(kind == "message.update" and "理解你的请求" in data["text"] for kind, data in events)
        activity = ModelActivity()
        activity.emit("waiting")
        await asyncio.sleep(0)
        activity.emit("connected")
        await asyncio.sleep(0)
        activity.emit("thinking")
        return IntentDecision(intent="analyze_style", summary="分析范文风格")

    monkeypatch.setattr(workspace, "sse", capture)
    monkeypatch.setattr(workspace, "detect_intent", classify)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "看一下这个示例文件的风格"})
    assert "event: message.completed" in response.text
    intent = [data for kind, data in events if kind == "message.update"]
    assert {item["model_status"] for item in intent} >= {"waiting", "connected", "thinking"}
    assert len({item["activity"]["id"] for item in intent}) == 1
    saved = client.get(f"/conversations/{cid}").json()["messages"][-1]
    activities = saved["activities"]
    assert "理解你的请求" in activities[0]["text"]
    assert "《示例.md》" in activities[1]["text"]
    assert "整理对你问题的回答" in activities[-1]["text"]
    assert len({item["id"] for item in activities}) == len(activities)


def test_intent_failure_preserves_generic_progress_label(setup, monkeypatch):
    _, client, _, _ = setup
    cid = client.post("/conversations").json()["id"]

    async def failed_intent(*args, **kwargs):
        raise ModelCallFailed("private failure", "timeout")

    monkeypatch.setattr(workspace, "detect_intent", failed_intent)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "你好"})
    assert "event: message.update" in response.text and "event: message.failed" in response.text
    saved = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert saved["activity_label"] == "处理过程"
    assert saved["activities"][-1]["status"] == "failed"


def test_real_intent_entry_routes_only_the_selected_style_file(setup, monkeypatch):
    _, client, calls, chats = setup
    cid = client.post("/conversations").json()["id"]
    upload(client, cid, name="业务资料.md", role="source")
    upload(client, cid, name="参考A.md")
    target = upload(client, cid, name="参考B.md")
    decisions = []

    class IntentRuntime:
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            assert task == "intent"
            decisions.append(context)
            assert len(context["workflow"]["files"]) == 3
            return RuntimeResult(json.dumps({
                "intent": "analyze_style", "summary": "分析指定范文的表达特点",
                "style_source_ids": [target["id"]],
            }))

        async def aclose(self):
            pass

    monkeypatch.setattr(workspace, "detect_intent", REAL_DETECT_INTENT)
    monkeypatch.setattr(workspace, "LLMRuntime", IntentRuntime)
    response = client.post(f"/conversations/{cid}/messages", json={"text": "分析参考B.md的表达特点"})
    assert "event: style.completed" in response.text
    saved = client.get(f"/conversations/{cid}").json()
    assert saved["style_analyses"][-1]["document_names"] == ["参考B.md"]
    assert [item["id"] for item in chats[-1]["attachments"]] == [target["id"]]
    assert len(calls) == len(decisions) == 1
    response = client.post(f"/conversations/{cid}/messages", json={"text": "分析参考B.md的表达特点"})
    assert "event: style.completed" in response.text and len(calls) == 1
    assert all("style_analysis" not in message and "activities" not in message for message in decisions[-1]["messages"])


def test_chat_receives_owned_directory_style_without_rereading_example(tmp_path):
    from folio.api.style_analysis import ConversationStyles
    from folio.previews import PreviewLibrary
    from folio.source_documents import SourceDirectoryDrafts

    class Directory:
        def load(self, revision, conversation_id):
            assert revision == "r" and conversation_id == "c"
            return self.saved

    from hashlib import sha256

    path = tmp_path / "sample.txt"
    path.write_text("旧项目范文", encoding="utf-8")
    version = sha256(path.read_bytes()).hexdigest()
    files = [{"id": "a" * 32, "name": path.name, "role": "style_reference", "path": str(path)}]
    directory = Directory()
    directory.saved = {"source_roles": [{"id": files[0]["id"], "role": "style_reference"}], "writing_profile": {
        "role": "style_only", "source_hashes": [version], "documents": [{"source_hash": version, "status": "analyzed", "rules": [
            {"dimension": "writing", "description": "使用客观陈述。", "confidence": "observed"},
        ]}],
    }}
    state = {"directory_review": {"directory": {"revision": "r"}}}
    styles = ConversationStyles(tmp_path)
    assert "客观陈述" in workspace.conversation_style_context(styles, directory, "c", state, files)
    path.write_text("已改变原件", encoding="utf-8")
    assert workspace.conversation_style_context(styles, directory, "c", state, files) == ""
    assert workspace.conversation_style_context(styles, SourceDirectoryDrafts(tmp_path, PreviewLibrary(tmp_path)), "c", state, files) == ""


def test_current_directory_fills_the_other_file_missing_from_standalone_analysis(tmp_path):
    from hashlib import sha256

    files, documents = [], []
    for letter, font_size in (("a", 12), ("b", 14)):
        path = tmp_path / (letter + ".md")
        path.write_text(letter, encoding="utf-8")
        files.append({"id": letter * 32, "name": path.name, "path": str(path), "role": "style_reference"})
        documents.append({
            "source_hash": sha256(path.read_bytes()).hexdigest(), "status": "analyzed",
            "layout": {"body": {"font_size_pt": font_size}}, "rules": [],
        })

    class Styles:
        def documents(self, *args, **kwargs):
            return [documents[0] | {"reference_name": "a.md", "reference_id": "a" * 32}]

    class Directory:
        def load(self, *args):
            return {
                "source_roles": [{"id": item["id"], "role": item["role"]} for item in files],
                "writing_profile": {"role": "style_only", "source_hashes": [doc["source_hash"] for doc in documents], "documents": documents},
            }

    state = {"directory_review": {"directory": {"revision": "r"}}}
    selected = workspace.conversation_style_documents(Styles(), Directory(), "c", state, files)
    assert [doc["reference_name"] for doc in selected] == ["a.md", "b.md"]
    assert [doc["layout"]["body"]["font_size_pt"] for doc in selected] == [12, 14]


def test_analyzing_another_file_or_changed_hash_excludes_old_style_prose(tmp_path):
    from hashlib import sha256

    path = tmp_path / "a.md"
    path.write_text("原始示例", encoding="utf-8")
    reference = {"id": "a" * 32, "sha256": sha256(path.read_bytes()).hexdigest()}
    message = {"role": "assistant", "content": "历史特定风格结论", "style_context_versions": [reference]}
    current = {"id": "a" * 32, "role": "style_reference", "path": str(path)}
    assert workspace.scoped_chat_history([message], [current])[0]["content"] == message["content"]
    assert "历史特定风格结论" not in str(workspace.scoped_chat_history([message], [current | {"id": "b" * 32}]))
    path.write_text("新文件内容", encoding="utf-8")
    assert "历史特定风格结论" not in str(workspace.scoped_chat_history([message], [current]))
