"""Exercise optional web calls through the real directory native-tool loop."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_planning_web import Provider, evidence
from test_planning_web import runtime as configured_runtime

from folio.orchestration.directory_planning import JobPlanningRuntime
from folio.research import planning
from folio.research.native_tools import native_result, tool_definitions
from folio.research.planning import (
    PlanningResearchBatch,
    PlanningResearchLimits,
    _DirectoryResearch,
    research_for_directory,
)
from folio.research.planning_web import DirectoryWebResearch
from folio.research.transport import research_messages
from folio.research.workspace import WorkspaceManager
from folio.runtime.base import RuntimeResult


def calls(*items):
    return RuntimeResult("", tool_calls=[{
        "id": f"call_{index}_{name}", "type": "function",
        "function": {"name": name, "arguments": json.dumps({"summary": "Inspect useful references", **arguments})},
    } for index, (name, arguments) in enumerate(items)])


class Runtime:
    def __init__(self, responses, *, enabled=True):
        self.config = configured_runtime().config if enabled else {}
        self.responses = iter(responses)
        self.contexts = []
        self.tools = []

    async def complete(self, task, context, schema, session_key):
        assert task == "directory_research"
        self.contexts.append(deepcopy(context))
        self.tools.append(tool_definitions(task, context, schema))
        response = next(self.responses)
        return response(context) if callable(response) else response


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "requirements.md").write_text("# Technical scope\nThe bidder must preserve audit records.\n")
    return root


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Integration tests must never contact the real search service")

    monkeypatch.setattr("folio.knowledge.tavily.TavilyKnowledge.search", forbidden)


def attach_provider(monkeypatch, provider):
    helpers = []

    def factory(runtime, output):
        helper = DirectoryWebResearch(runtime, output, provider=provider)
        helpers.append(helper)
        return helper

    monkeypatch.setattr(planning, "DirectoryWebResearch", factory)
    return helpers


def local_requirements(context):
    refs = [raw["evidence_id"] for entry in context["observations"]
            if entry["tool"] == "fs.read" and entry["arguments"].get("scope", "sources") == "sources"
            for raw in entry["result"].get("evidence", [])]
    return [{"requirement_id": "audit", "text": "Cover the required audit records", "evidence_ids": refs}]


def finish_local(context, **extra):
    return calls(("finish", {"summary": "Uploaded requirements are understood",
                              "requirements": local_requirements(context), **extra}))


async def test_parallel_local_read_and_web_search_return_matching_ids_and_separate_findings(sources, tmp_path, monkeypatch):
    provider = Provider([evidence()])
    attach_provider(monkeypatch, provider)

    def finish(context):
        messages = research_messages(context, {})
        outputs = [message for message in messages if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in outputs] == ["call_0_fs_read", "call_1_search_web"]
        assert "audit records" in outputs[0]["content"]
        assert "[W1]" in outputs[1]["content"] and "https://example.com/standard" in outputs[1]["content"]
        assert "not verified project facts" in outputs[1]["content"]
        return finish_local(context, web_findings=[{"text": "Use the public standard to organize the audit section",
                                                     "evidence_ids": ["W1"]}])

    runtime = Runtime([
        calls(("fs_read", {"file": "F1"}),
              ("search_web", {"query": "official audit guidance", "purpose": "Choose audit-section organization"})),
        finish,
    ])
    output = tmp_path / "research"
    report = await research_for_directory(runtime, sources, output, "Prepare the bid outline")
    assert report["status"] == "ready", report["validation_issues"]
    assert report["calls"] == 2 and report["tool_calls"] == 3
    assert len(provider.calls) == 1
    public = report["web_research"]
    assert public["searches"][0]["status"] == "completed"
    assert public["findings"][0]["evidence_ids"] == [evidence().evidence_id]
    assert public["evidence"][0]["metadata"]["project_fact"] is False
    assert all(item["source_id"] == "requirements.md" for item in report["evidence"])
    assert evidence().evidence_id not in report["requirements"][0]["evidence_ids"]
    saved = json.loads((output / "report.json").read_text())
    assert saved["web_research"] == public


@pytest.mark.parametrize("reference", ["W1", evidence().evidence_id])
async def test_public_reference_cannot_be_promoted_to_uploaded_requirement(sources, tmp_path, monkeypatch, reference):
    attach_provider(monkeypatch, Provider([evidence()]))

    def rejected(context):
        return calls(("finish", {"requirements": local_requirements(context) + [
            {"requirement_id": "forged-tender-term", "text": "An external page is not a tender requirement",
             "evidence_ids": [reference]},
        ]}))

    def corrected(context):
        assert context["feedback"]
        return finish_local(context)

    runtime = Runtime([
        calls(("fs_read", {"file": "F1"}), ("search_web", {"query": "public standard", "purpose": "Find context"})),
        rejected, corrected,
    ])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "Prepare the outline")
    assert report["status"] == "ready"
    assert [item["requirement_id"] for item in report["requirements"]] == ["audit"]
    assert len(runtime.contexts) == 3


async def test_disabled_configuration_omits_native_search_tool(sources, tmp_path, monkeypatch):
    provider = Provider()
    attach_provider(monkeypatch, provider)
    runtime = Runtime([calls(("fs_read", {"file": "F1"})), finish_local], enabled=False)
    report = await research_for_directory(runtime, sources, tmp_path / "research", "Prepare the outline")
    assert report["status"] == "ready"
    assert all("search_web" not in {tool["function"]["name"] for tool in tools} for tools in runtime.tools)
    assert report["web_research"]["available"] is False
    assert not provider.calls


async def test_failed_optional_search_keeps_sibling_local_read_and_allows_finish(sources, tmp_path, monkeypatch):
    provider = Provider(RuntimeError("private upstream payload and API key"))
    attach_provider(monkeypatch, provider)

    def finish(context):
        entries = context["observations"]
        local = next(item for item in entries if item["tool"] == "fs.read")
        web = next(item for item in entries if item["tool"] == "search.web")
        assert local["result"]["evidence"]
        assert web["result"]["status"] == "failed"
        assert "private upstream" not in json.dumps(web)
        return finish_local(context, unresolved=["The optional public reference could not be retrieved"])

    runtime = Runtime([
        calls(("fs_read", {"file": "F1"}), ("search_web", {"query": "public guidance", "purpose": "Check an optional reference"})),
        finish,
    ])
    report = await research_for_directory(runtime, sources, tmp_path / "research", "Prepare the outline")
    assert report["status"] == "ready"
    assert report["requirements"] and report["evidence"]
    assert report["web_research"]["searches"][0]["status"] == "failed"
    assert not report["web_research"]["findings"]


async def state_with_web(tmp_path, sources, *, text="Public reference excerpt", **limits):
    output = tmp_path / "research"
    output.mkdir()
    helper = DirectoryWebResearch(configured_runtime(), output, provider=Provider([evidence(text=text)]))
    workspace = WorkspaceManager(sources, output / "workspace")
    state = _DirectoryResearch(workspace, output, "Prepare the outline", None,
                               PlanningResearchLimits(**limits), None, web=helper)
    initial = workspace.list()
    state.register("fs.list", {}, initial)
    return state, helper, initial


async def test_unknown_and_retrieved_but_undelivered_web_references_cannot_finish(sources, tmp_path):
    state, helper, _ = await state_with_web(tmp_path, sources)
    await helper.search("standard", "Check a public requirement")
    with pytest.raises(ValueError, match="Unknown"):
        state.accept_conclusion({"web_findings": [{"text": "Invented citation", "evidence_ids": ["W999"]}]})
    with pytest.raises(ValueError, match="actually supplied"):
        state.accept_conclusion({"web_findings": [{"text": "Retrieved but unseen", "evidence_ids": ["W1"]}]})
    assert not state.conclusion.web_findings


async def test_capacity_pages_web_result_and_only_full_excerpts_count_as_delivered(sources, tmp_path):
    text = "A long public reference passage. " * 5000
    state, helper, initial = await state_with_web(tmp_path, sources, text=text, max_context_chars=40000)
    schema = PlanningResearchBatch.model_json_schema()
    context = state.context(initial, [])
    batch = PlanningResearchBatch.model_validate(native_result("directory_research", calls(
        ("search_web", {"query": "standard", "purpose": "Check public context"}),
    ), context, schema))
    await state.execute_batch(batch, "search_001", initial=initial, schema=schema)
    entry = state.observations[-1]
    assert entry["result"]["web_result_page"]
    assert not entry["result"]["complete"] and entry["result"]["next_offset"]
    assert "fs_read(scope='history'" in entry["model_text"]
    context = state.context(initial, [], preserve_history=True)
    assert state.fits_context(context)
    state.delivered(context, "model_received_first_page")
    assert not state.delivered_web_evidence
    with pytest.raises(ValueError, match="actually supplied"):
        state.accept_conclusion({"web_findings": [{"text": "The excerpt was only partially read", "evidence_ids": ["W1"]}]})
    # A continuation uses the saved search text, never another external request.
    offset = entry["result"]["next_offset"]
    number = 0
    while offset is not None:
        number += 1
        args = {"scope": "history", "request_id": "search_001", "offset": offset}
        batch = PlanningResearchBatch.model_validate(native_result(
            "directory_research", calls(("fs_read", args)), context, schema))
        request_id = f"continue_{number}"
        await state.execute_batch(batch, request_id, initial=initial, schema=schema, current_context=context)
        context = state.context(initial, [], schema=schema)
        assert state.fits_context(context)
        state.delivered(context, f"model_received_{number}")
        remaining = state.observations[-1]["result"]
        offset = remaining["next_offset"]
        if offset is not None:
            assert not state.delivered_web_evidence
    assert remaining["complete"] and remaining["text"]
    assert len(helper.report()["searches"]) == 1
    assert state.delivered_web_evidence == {helper.resolve_reference("W1").evidence_id}
    state.accept_conclusion({"web_findings": [{"text": "The complete excerpt is now available",
                                                "evidence_ids": ["W1"]}]})
    assert state.conclusion.web_findings[0].evidence_ids == [helper.resolve_reference("W1").evidence_id]


@pytest.mark.parametrize("enabled", [True, False])
def test_job_planning_adapter_forwards_verified_search_settings(enabled):
    underlying = configured_runtime() if enabled else SimpleNamespace()
    adapter = JobPlanningRuntime(SimpleNamespace(runtime=underlying))
    assert adapter.config == (underlying.config if enabled else {})
