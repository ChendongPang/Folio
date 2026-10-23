import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.directory_drafts import DirectoryDrafts
from folio.knowledge.local import LocalKnowledge
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


@pytest.fixture
def setup(tmp_path, monkeypatch):
    calls = []

    class Planner(DemoRuntime):
        def __init__(self, config):
            pass

        async def complete(self, task, context, schema, session_key):
            # These assertions count directory proposals/edits; actual research
            # and coverage review still execute through DemoRuntime below.
            if task in {"plan", "revise_plan", "repair_plan"}:
                calls.append(json.loads(json.dumps(context)))
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
                            "summary": "修改标题",
                        }
                    )
                )
            return await super().complete(task, context, schema, session_key)

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", Planner)
    monkeypatch.setattr(
        "folio.document.word.paginate", lambda *a, **k: {"status": "requires_office"}
    )
    root = tmp_path / "web"
    ModelSettingsStore(root).save(
        ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True)
    )
    app = create_app(root)
    return app, calls


def state(client, cid):
    return client.get(f"/conversations/{cid}").json()["directory_review"]


def act(client, cid, action, **extra):
    return client.post(
        f"/conversations/{cid}/messages",
        json={
            "text": action,
            "action": action,
            "selection_revision": state(client, cid)["revision"],
            **extra,
        },
    )


def begin(client, inputs, *, guide_id=None):
    cid = client.post("/conversations", json={"guide_id": guide_id}).json()["id"]
    source = Path(inputs["knowledge_path"]) / "rules.md"
    uploaded = client.post(
        f"/conversations/{cid}/sources",
        params={"filename": source.name},
        content=source.read_bytes(),
    )
    assert uploaded.status_code == 201
    response = client.post(
        f"/conversations/{cid}/messages", json={"text": "外汇交易需求说明书", "action": "generate"}
    )
    assert "directory.review" in response.text and "job.created" not in response.text
    assert "资料清单和结构索引" in response.text
    assert "正在等待模型返回完整目录" in response.text
    assert "正在排版 Word 预览" in response.text
    assert '"elapsed_seconds":' in response.text
    assert state(client, cid)["phase"] == "directory_review"
    return cid


def assert_no_jobs(app):
    with app.state.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_apply_then_multiple_revisions_require_fresh_confirmation(setup, inputs):
    app, calls = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        first = state(client, cid)
        assert first["directory"]["sections"] and calls[0]["requirements"] == "外汇交易需求说明书"
        assert_no_jobs(app)
        assert act(client, cid, "revise_directory", advice="新的项目概述").status_code == 200
        second = state(client, cid)
        assert second["directory"]["sections"][0]["title"] == "新的项目概述"
        assert second["revision"] != first["revision"]
        assert "previous_proposal" in calls[-1] and calls[-1]["revision_request"] == "新的项目概述"
        assert calls[-1]["previous_directory"][0]["number"] == "1"
        assert "确认" in second["reason"]
        assert_no_jobs(app)
        stale = act(client, cid, "confirm_directory", selection_revision=first["revision"])
        assert stale.status_code == 409
        act(client, cid, "revise_directory", advice="再调整后的概述")
        third = state(client, cid)
        assert third["directory"]["sections"][0]["title"] == "再调整后的概述"
        assert calls[-1]["revision_history"] == ["新的项目概述", "再调整后的概述"]
        assert calls[-1]["previous_proposal"]["sections"][0]["title"] == "新的项目概述"
        assert_no_jobs(app)
    with TestClient(create_app(app.state.store.root)) as client:
        assert state(client, cid) == third


def test_revision_failure_keeps_last_reviewable_directory(setup, inputs, monkeypatch):
    app, calls = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        previous = state(client, cid)

        async def fail(*args, **kwargs):
            raise TimeoutError()

        original = DirectoryDrafts.create
        monkeypatch.setattr(DirectoryDrafts, "create", fail)
        response = act(client, cid, "revise_directory", advice="修改概述")
        assert "message.failed" in response.text and "directory.review" in response.text
        failed = state(client, cid)
        assert failed["directory"] == previous["directory"]
        assert failed["revision"] == previous["revision"]
        assert "已有目录仍保留" in failed["operation_error"]["text"]
        assert failed["failed_advice"] == "修改概述"
        assert_no_jobs(app)
        monkeypatch.setattr(DirectoryDrafts, "create", original)
        act(client, cid, "revise_directory", advice="修改概述")
        succeeded = state(client, cid)
        assert "operation_error" not in succeeded and "failed_advice" not in succeeded
        assert succeeded["directory"]["sections"][0]["title"] == "修改概述"


def test_explicit_regeneration_plans_afresh_and_preserves_sources_and_guide(setup, inputs):
    app, calls = setup
    with TestClient(app) as client:
        cid = begin(client, inputs, guide_id="srs")
        original = state(client, cid)
        act(client, cid, "revise_directory", advice="不应锚定的旧版标题")
        before = state(client, cid)
        response = act(client, cid, "regenerate_directory")
        assert response.status_code == 200 and "directory.review" in response.text
        assert "message.failed" not in response.text
        after = state(client, cid)
        assert (
            after["directory"]["sections"][0]["title"]
            == original["directory"]["sections"][0]["title"]
        )
        # Replanning discards the old proposal, but retains the user's changes.
        assert "不应锚定的旧版标题" in calls[-1]["requirements"]
        assert after["revision"] != before["revision"]
        assert after["directory"]["revision_history"] == []
        assert "previous_proposal" not in calls[-1]
        assert "revision_request" not in calls[-1]
        assert after["files"] == before["files"]
        assert after["writing_guide"] == before["writing_guide"]
        assert after["directory"]["writing_guide"]["id"] == "srs"
        stale = act(client, cid, "confirm_directory", selection_revision=before["revision"])
        assert stale.status_code == 409
        call_count = len(calls)
        stale = act(client, cid, "regenerate_directory", selection_revision=before["revision"])
        assert stale.status_code == 409 and len(calls) == call_count
        assert_no_jobs(app)


def test_reapply_failure_keeps_current_directory_and_confirmation(setup, inputs, monkeypatch):
    app, calls = setup
    with TestClient(app) as client:
        cid = begin(client, inputs, guide_id="urd")
        before = state(client, cid)

        async def fail(*args, **kwargs):
            raise TimeoutError()

        original = DirectoryDrafts.create
        monkeypatch.setattr(DirectoryDrafts, "create", fail)
        # Preserve failed local advice first, then clear it only after fresh success.
        act(client, cid, "revise_directory", advice="修改概述")
        assert state(client, cid)["failed_advice"] == "修改概述"
        response = act(client, cid, "regenerate_directory")
        assert "message.failed" in response.text and "directory.review" in response.text
        after = state(client, cid)
        for field in (
            "directory",
            "revision",
            "phase",
            "files",
            "writing_guide",
        ):
            assert after[field] == before[field]
        assert "已有目录仍保留" in after["operation_error"]["text"]
        assert_no_jobs(app)
        monkeypatch.setattr(DirectoryDrafts, "create", original)
        response = act(client, cid, "regenerate_directory")
        assert "directory.review" in response.text and "message.failed" not in response.text
        after = state(client, cid)
        assert "operation_error" not in after and "failed_advice" not in after
        assert after["revision"] != before["revision"]
        assert "previous_proposal" not in calls[-1]
        assert after["files"] == before["files"]
        assert after["writing_guide"] == before["writing_guide"]
        assert_no_jobs(app)


def test_restart_words_inside_local_advice_do_not_trigger_reapply(setup, inputs):
    app, calls = setup
    advice = "不要重新套用模板，只将概述改名为项目背景"
    with TestClient(app) as client:
        cid = begin(client, inputs)
        response = act(client, cid, "revise_directory", advice=advice)
        assert response.status_code == 200 and "message.failed" not in response.text
        assert calls[-1]["revision_request"] == advice
        assert "previous_proposal" in calls[-1]
        assert state(client, cid)["directory"]["sections"][0]["title"] == advice


def test_confirmed_plan_is_atomically_saved_and_engine_does_not_replan(setup, inputs, monkeypatch):
    app, calls = setup
    executed = []

    class Writer(DemoRuntime):
        async def complete(self, task, *args):
            assert task != "plan", "Confirmed directory must not be regenerated"
            return await super().complete(task, *args)

    async def execute(store, job_id):
        executed.append(store.get(job_id)["plan"])
        await Engine(
            store, Writer(), LocalKnowledge(Path(store.get(job_id)["input"]["knowledge_path"]))
        ).run(job_id)

    monkeypatch.setattr("folio.api.workspace.execute", execute)
    with TestClient(app) as client:
        cid = begin(client, inputs)
        act(client, cid, "revise_directory", advice="交易撤销概述")
        review = state(client, cid)
        response = act(client, cid, "confirm_directory")
        assert "artifact.ready" in response.text and "job.created" in response.text
        assert len(calls) == 2 and len(executed) == 1
        assert [s["title"] for s in executed[0]["sections"]] == [
            s["title"] for s in review["directory"]["sections"]
        ]
        job_id = client.get(f"/conversations/{cid}").json()["active_job"]
        job_input = app.state.store.get(job_id)["input"]
        assert job_input["requirements"] == review["requirements"]
        assert job_input["user_request"] == review["request"]
        assert "交易撤销概述" in job_input["user_request"]
        assert "confirm_directory" not in job_input["user_request"]
        frozen = app.state.store.root / "jobs" / job_id / "inputs" / "requirements.json"
        assert json.loads(frozen.read_text()) == review["requirements"]
        assert act(client, cid, "confirm_directory").status_code == 409
        assert state(client, cid)["phase"] == "generating"


def test_natural_chat_cannot_bypass_directory_gate(setup, inputs, monkeypatch):
    from folio.decisions import IntentDecision

    app, _ = setup

    async def intent(*args):
        return IntentDecision(intent="generate", summary="用户要继续生成")

    monkeypatch.setattr("folio.api.workspace.detect_intent", intent)
    with TestClient(app) as client:
        cid = begin(client, inputs)
        previous = state(client, cid)
        response = client.post(f"/conversations/{cid}/messages", json={"text": "继续生成"})
        assert "directory.review" in response.text and "job.created" not in response.text
        assert state(client, cid) == previous
        assert_no_jobs(app)


def test_planning_reads_all_user_constraints_and_refuses_capacity_without_truncating(
    setup,
    inputs,
):
    app, calls = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        saved = client.get(f"/conversations/{cid}").json()
        saved["messages"].extend(
            {"role": "user", "content": f"补充业务约束 {index}，必须保留原始限定。"}
            for index in range(12)
        )
        with app.state.store.connect() as conn:
            conn.execute(
                "UPDATE web_conversations SET data=? WHERE id=?",
                (json.dumps(saved), cid),
            )
        assert "message.failed" not in act(client, cid, "regenerate_directory").text
        assert "外汇交易需求说明书" in calls[-1]["requirements"]
        assert "补充业务约束 0" in calls[-1]["requirements"]
        assert "补充业务约束 11" in calls[-1]["requirements"]
        saved = client.get(f"/conversations/{cid}").json()
        oversized_constraint = "完整用户限制" * 100_001
        assert len(oversized_constraint) > 600000
        saved["messages"].append({"role": "user", "content": oversized_constraint})
        with app.state.store.connect() as conn:
            conn.execute(
                "UPDATE web_conversations SET data=? WHERE id=?",
                (json.dumps(saved), cid),
            )
        count = len(calls)
        response = act(client, cid, "regenerate_directory")
        assert "directory_context_limit" in response.text and "未截断" in response.text
        assert len(calls) == count
        saved = client.get(f"/conversations/{cid}").json()
        assert saved["requirements"]["entries"][-1]["text"] == oversized_constraint


def test_new_requirements_cannot_be_confirmed_with_old_directory(setup, inputs):
    app, _ = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        saved = client.get(f"/conversations/{cid}").json()
        saved["messages"].append({"role": "user", "content": "新的限制：只写一期。"})
        with app.state.store.connect() as conn:
            conn.execute(
                "UPDATE web_conversations SET data=? WHERE id=?",
                (json.dumps(saved), cid),
            )
        response = act(client, cid, "confirm_directory")
        assert response.status_code == 409 and "写作要求已更新" in response.text
        assert_no_jobs(app)


def test_legacy_directory_requires_lossless_replanning_before_confirmation(setup, inputs):
    app, _ = setup
    with TestClient(app) as client:
        cid = begin(client, inputs)
        selected = state(client, cid)
        metadata_path = (
            app.state.store.root
            / "directory-drafts"
            / selected["directory"]["revision"]
            / "metadata.json"
        )
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("requirements")
        metadata_path.write_text(json.dumps(metadata))
        rejected = act(client, cid, "confirm_directory")
        assert rejected.status_code == 409 and "需求快照" in rejected.text
        assert_no_jobs(app)
        regenerated = act(client, cid, "regenerate_directory")
        assert "directory.review" in regenerated.text and "message.failed" not in regenerated.text
