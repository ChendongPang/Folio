"""An interrupted directory research run continues from its recorded model responses."""

import asyncio
import json
from copy import deepcopy

import pytest
from test_planning_web import Provider, evidence
from test_planning_web import runtime as web_runtime

from folio.research import planning
from folio.research.planning import PlanningResearchLimits, research_for_directory
from folio.research.planning_replay import request_signature
from folio.research.planning_web import DirectoryWebResearch
from folio.runtime.base import RuntimeResult


def calls(*items):
    return RuntimeResult("", usage={"input_tokens": 10, "output_tokens": 5}, tool_calls=[{
        "id": f"call_{index}_{name}", "type": "function",
        "function": {"name": name, "arguments": json.dumps({"summary": name, **arguments}, ensure_ascii=False)},
    } for index, (name, arguments) in enumerate(items)])


class Runtime:
    """Answers in order; an awaitable answer stalls a request until it times out."""

    def __init__(self, *responses, config=None):
        self.responses = list(responses)
        self.contexts = []
        self.config = config or {}

    async def complete(self, task, context, schema, session_key):
        assert task == "directory_research"
        self.contexts.append(deepcopy(context))
        response = self.responses.pop(0)
        value = response(context) if callable(response) else response
        return await value if asyncio.iscoroutine(value) else value


async def stall(context):
    await asyncio.sleep(10)


def finish(context, **extra):
    evidence_ids = [raw["evidence_id"] for entry in context["observations"] if entry["tool"] == "fs.read"
                    for raw in entry["result"].get("evidence", [])]
    return calls(("finish", {"summary": "已研究技术要求", "requirements": [
        {"requirement_id": "technical", "text": "覆盖技术要求", "evidence_ids": evidence_ids}], **extra}))


def recorded_context(folder, request_id):
    return json.loads((folder / "events" / f"{request_id}-request.json").read_text(encoding="utf-8"))["context"]


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "rules.md").write_text("# 技术要求\n必须支持撤单后释放库存。\n必须记录审批日志。\n", encoding="utf-8")
    return root


async def interrupted(sources, output):
    runtime = Runtime(calls(("fs_inspect", {"file": "F1"})), calls(("fs_read", {"file": "F1"})), stall)
    report = await research_for_directory(runtime, sources, output, "编写要求",
                                          limits=PlanningResearchLimits(call_timeout_seconds=0.05))
    assert report["termination_reason"] == "model_timeout" and report["calls"] == 3
    return report


@pytest.mark.asyncio
async def test_resume_replays_recorded_steps_and_sends_only_the_timed_out_request(sources, tmp_path):
    first = tmp_path / "first"
    original = await interrupted(sources, first)
    runtime = Runtime(finish)
    output = tmp_path / "resumed"
    messages = []
    report = await research_for_directory(runtime, sources, output, "编写要求", replay=first,
                                          on_progress=messages.append)
    assert report["status"] == "ready", report["validation_issues"]
    # The model sees exactly the request that timed out, rebuilt from replayed steps.
    assert len(runtime.contexts) == 1
    assert request_signature(runtime.contexts[0]) == request_signature(recorded_context(first, "research_003"))
    assert report["calls"] == 3 and report["tool_calls"] == 3
    assert report["resume"] == {"resumed_from": str(first), "replayed_calls": 2, "continued_at": "research_003"}
    assert [item.get("replayed", False) for item in report["usage"]] == [True, True, False]
    replayed = json.loads((output / "events/research_001-response.json").read_text(encoding="utf-8"))
    assert replayed["replayed_from"] == str(first)
    assert [item["evidence_id"] for item in report["evidence"]] == [
        item["evidence_id"] for item in original["evidence"]]
    assert any("复用已保存的 2 次模型结果" in message for message in messages)
    assert any("从第 3 次研究请求继续" in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,value", [
    ("input.json", []),
    ("report.json", []),
    ("web-research.json", []),
    ("web-research.json", {"searches": {}}),
    ("events/research_001-request.json", {"context": []}),
    ("events/research_001-response.json", []),
    ("events/research_001-response.json", {"usage": [1]}),
])
async def test_invalid_replay_records_are_rejected_before_calling_model(sources, tmp_path, filename, value):
    first = tmp_path / "first"
    await interrupted(sources, first)
    (first / filename).write_text(json.dumps(value), encoding="utf-8")
    runtime = Runtime()
    with pytest.raises(ValueError, match='research records are unavailable'):
        await research_for_directory(runtime, sources, tmp_path / "resumed", "编写要求", replay=first)
    assert not runtime.contexts


@pytest.mark.asyncio
async def test_resumed_source_directory_keeps_revision_history(tmp_path, monkeypatch):
    from folio.previews import PreviewLibrary
    from folio.runtime.demo import DemoRuntime
    from folio.source_documents import SourceDirectoryDrafts

    requests = []
    interrupt = True

    class Planner(DemoRuntime):
        def __init__(self, config):
            self.research_calls = 0

        async def complete(self, task, context, schema, session_key):
            requests.append((task, deepcopy(context)))
            if task == "directory_research":
                self.research_calls += 1
                if interrupt and self.research_calls == 2:
                    raise TimeoutError("Interrupted model request")
            return await super().complete(task, context, schema, session_key)

    monkeypatch.setattr("folio.directory_drafts.LLMRuntime", Planner)
    monkeypatch.setattr("folio.document.word.paginate", lambda *a, **k: {"status": "not_required"})
    monkeypatch.setattr("folio.document.pagination.paginate", lambda *a, **k: {"status": "not_required"})
    source = tmp_path / "rules.md"
    source.write_text("# 订单规则\n订单付款前允许撤销。", encoding="utf-8")
    attachments = [{"id": "a" * 32, "name": source.name, "path": str(source)}]
    root = tmp_path / "state"
    drafts = SourceDirectoryDrafts(root, PreviewLibrary(root))
    first = await drafts.create_from_sources(None, "conversation", attachments, "# 订单\n说明撤销规则")
    assert first["research"]["termination_reason"] == "model_timeout"
    old = drafts.load(first["revision"], "conversation")
    history = ["把订单撤销的审批条件单独列一节"]
    metadata_path = old["folder"] / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["revision_history"] = history
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    previous = drafts.load(first["revision"], "conversation")
    interrupt = False
    requests.clear()
    resumed = await drafts.create_from_sources(
        None, "conversation", attachments, "# 订单\n说明撤销规则", resume_from=previous)
    saved = drafts.load(resumed["revision"], "conversation")
    assert saved["revision_history"] == resumed["revision_history"] == history
    assert saved["resumed_from"] == first["revision"]
    assert resumed["confirmable"]
    plan_context = next(context for task, context in requests if task == "plan")
    assert plan_context["revision_history"] == history
    assert not any(task == "revise_plan" for task, _ in requests)
    assert not any(context.get("observations") == [] for task, context in requests if task == "directory_research")


@pytest.mark.asyncio
async def test_replay_stops_at_the_first_request_that_differs_from_the_record(sources, tmp_path):
    first = tmp_path / "first"
    await interrupted(sources, first)
    path = first / "events/research_002-request.json"
    request = json.loads(path.read_text(encoding="utf-8"))
    request["context"]["notes"] = "an earlier state that differs from this run"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    runtime = Runtime(calls(("fs_read", {"file": "F1"})), finish)
    report = await research_for_directory(runtime, sources, tmp_path / "resumed", "编写要求", replay=first)
    assert report["status"] == "ready", report["validation_issues"]
    assert len(runtime.contexts) == 2
    assert report["resume"]["replayed_calls"] == 1 and report["resume"]["continued_at"] == "research_002"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["requirements", "sources"])
async def test_resume_requires_the_same_requirements_and_sources(sources, tmp_path, change):
    first = tmp_path / "first"
    await interrupted(sources, first)
    requirements = "编写要求"
    if change == "requirements":
        requirements = "编写另一份文档"
    else:
        (sources / "rules.md").write_text("# 技术要求\n内容已经更新。\n", encoding="utf-8")
    runtime = Runtime()
    with pytest.raises(ValueError, match='changed'):
        await research_for_directory(runtime, sources, tmp_path / "resumed", requirements, replay=first)
    assert not runtime.contexts


@pytest.mark.asyncio
async def test_completed_research_is_not_resumable(sources, tmp_path):
    first = tmp_path / "first"
    done = await research_for_directory(Runtime(calls(("fs_read", {"file": "F1"})), finish),
                                        sources, first, "编写要求")
    assert done["status"] == "ready", done["validation_issues"]
    with pytest.raises(ValueError, match='not interrupted by a timeout'):
        await research_for_directory(Runtime(), sources, tmp_path / "resumed", "编写要求", replay=first)


@pytest.mark.asyncio
async def test_replay_reuses_public_search_receipts_without_searching_again(sources, tmp_path, monkeypatch):
    def attach(provider):
        monkeypatch.setattr(planning, "DirectoryWebResearch",
                            lambda runtime, output: DirectoryWebResearch(runtime, output, provider=provider))

    config = web_runtime().config
    searched = Provider([evidence()])
    attach(searched)
    first = tmp_path / "first"
    query = {"query": "official audit guidance", "purpose": "Choose audit-section organization"}
    report = await research_for_directory(
        Runtime(calls(("fs_read", {"file": "F1"}), ("search_web", query)), stall, config=config),
        sources, first, "编写要求", limits=PlanningResearchLimits(call_timeout_seconds=0.05))
    assert report["termination_reason"] == "model_timeout" and len(searched.calls) == 1
    unused = Provider()
    attach(unused)
    runtime = Runtime(lambda context: finish(context, web_findings=[
        {"text": "参考公开标准组织审计章节", "evidence_ids": ["W1"]}]), config=config)
    resumed = await research_for_directory(runtime, sources, tmp_path / "resumed", "编写要求", replay=first)
    assert resumed["status"] == "ready", resumed["validation_issues"]
    assert unused.calls == [] and len(runtime.contexts) == 1
    assert request_signature(runtime.contexts[0]) == request_signature(recorded_context(first, "research_002"))
    assert resumed["web_research"]["searches"] == report["web_research"]["searches"]
    assert resumed["web_research"]["findings"][0]["evidence_ids"] == [evidence().evidence_id]


@pytest.mark.asyncio
async def test_replay_preserves_references_when_parallel_searches_finish_out_of_order(sources, tmp_path, monkeypatch):
    second_started = asyncio.Event()

    class OutOfOrderProvider(Provider):
        async def search(self, query, **kwargs):
            self.calls.append(query)
            if query == "first query":
                await second_started.wait()
            else:
                second_started.set()
            return [evidence(f"https://example.com/{query.split()[0]}", query)]

    provider = OutOfOrderProvider()
    monkeypatch.setattr(planning, "DirectoryWebResearch",
                        lambda runtime, output: DirectoryWebResearch(runtime, output, provider=provider))
    config = web_runtime().config
    first = tmp_path / "first"
    initial = Runtime(
        calls(("fs_read", {"file": "F1"}),
              ("search_web", {"query": "first query", "purpose": "First purpose"}),
              ("search_web", {"query": "second query", "purpose": "Second purpose"})),
        calls(("fs_inspect", {"file": "F1"})), stall, config=config,
    )
    original = await research_for_directory(
        initial, sources, first, "编写要求", limits=PlanningResearchLimits(call_timeout_seconds=0.05))
    assert original["termination_reason"] == "model_timeout"
    assert [item["metadata"]["reference"] for item in original["web_research"]["evidence"]] == ["W1", "W2"]
    assert original["web_research"]["evidence"][0]["text"] == "second query"
    runtime = Runtime(finish, config=config)
    resumed = await research_for_directory(runtime, sources, tmp_path / "resumed", "编写要求", replay=first)
    assert resumed["status"] == "ready", resumed["validation_issues"]
    assert resumed["resume"]["replayed_calls"] == 2
    assert len(runtime.contexts) == 1 and len(provider.calls) == 2
    assert request_signature(runtime.contexts[0]) == request_signature(recorded_context(first, "research_003"))
