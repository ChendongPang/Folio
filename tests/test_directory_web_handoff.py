"""Public references survive confirmation without becoming uploaded tender terms."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_directory_research_handoff import partial_research
from test_directory_research_integration import (
    planning_setup as planning_setup,
)
from test_directory_research_integration import (
    researched_directory as researched_directory,
)
from test_directory_research_integration import (
    start_writing,
    writing_task,
)

from folio.document.research_context import planning_research_context
from folio.models import Evidence, digest
from folio.orchestration.context import project
from folio.orchestration.web_research import WebQuery, WebResearchWorkflow
from folio.orchestration.work import BusinessRecord, WorkState, WorkTask
from folio.research.business_presentation import BusinessPresentation
from folio.research.context_views import context_field_text, fit_context
from folio.research.contracts import ResearchConfig
from folio.research.planning_handoff import seed_directory_research, web_research_navigation
from folio.research.workflow import ResearchTools, assess_research
from folio.research.workspace import WorkspaceManager


def web_receipt():
    text, url = "公开技术指南建议记录模型版本与评估数据版本。", "https://example.org/public-guide"
    version = digest(text)
    source = Evidence(
        evidence_id="web_" + digest(url + ":" + version), source_id=url, source_version=version,
        locator="Tavily advanced search excerpt", text=text, retrieved_at="2026-10-21T08:00:00+00:00",
        metadata={"role": "public_web", "project_fact": False, "url": url,
                  "title": "公开技术指南", "reference": "W1"},
    ).model_dump(mode="json")
    return {
        "available": True,
        "searches": [{"query": "公开模型评估技术指南", "purpose": "了解公开技术参考",
                      "status": "completed", "evidence_ids": [source["evidence_id"]]}],
        "evidence": [source], "delivered_evidence_ids": [source["evidence_id"]],
        "findings": [{"text": "可参考公开指南记录版本；这不构成本项目已批准的要求。",
                      "evidence_ids": [source["evidence_id"]]}],
    }


def attach_web_report(loaded, value):
    path = loaded["folder"] / "research" / "report.json"
    report = json.loads(path.read_text())
    report["web_research"] = value
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def test_planning_preserves_complete_public_reference_separately_from_local_requirements():
    report = partial_research()
    original = planning_research_context(report)
    report["web_research"] = web_receipt()
    before = deepcopy(report)
    context = planning_research_context(report)
    assert context["evidence"] == original["evidence"]
    assert context["requirements"] == original["requirements"]
    assert context["working_findings"] == original["working_findings"]
    assert context["web_research"] == report["web_research"]
    assert "cannot establish missing internal tender terms" in context["web_research_note"]
    source = context["web_research"]["evidence"][0]
    transported = project("plan", {"directory_research": context})
    assert transported["directory_research"]["web_research"]["evidence"][0]["retrieved_at"] == source["retrieved_at"]
    assert report == before


async def test_freeze_and_seed_reuse_public_sources_without_local_coverage_or_global_facts(researched_directory):
    root, _, _, loaded = researched_directory
    receipt = web_receipt()
    attach_web_report(loaded, receipt)
    store, job_id, engine = start_writing(root, loaded)
    frozen = json.loads((root / "jobs" / job_id / "inputs/directory-research/report.json").read_text())
    assert frozen["web_research"] == receipt
    source = receipt["evidence"][0]
    for section in loaded["plan"].sections:
        task, work = writing_task(section), WorkState(task_id=section.node_id)
        workspace = WorkspaceManager(Path(store.get(job_id)["input"]["knowledge_path"]), root / "task" / task.task_id)
        inherited = seed_directory_research(engine, task, work, workspace)
        assert source["evidence_id"] not in inherited["evidence_ids"]
        assert all(source["evidence_id"] not in item["evidence_ids"] for item in inherited["requirements"])
        assert inherited["web_research"]["findings"] == receipt["findings"]
        assert inherited["web_research"]["searches"] == receipt["searches"]
        assert inherited["web_research"]["sources"][0]["url"] == source["source_id"]
        assert "evidence" not in inherited["web_research"]  # No invented source-delivery receipt.
        assert work.record.facts == [] and work.global_facts == {} and work.web_searches == []
        saved = next(item for item in work.evidence if item.evidence_id == source["evidence_id"])
        assert saved.model_dump(mode="json") == source
        from folio.research.coverage_obligations import _verified_locations

        assert _verified_locations(workspace, [saved]) == []
        saved.text = "旧缓存错误地改变了网页摘录。"
        seed_directory_research(engine, task, work, workspace)
        saved = next(item for item in work.evidence if item.evidence_id == source["evidence_id"])
        assert saved.text == source["text"]
        tools = ResearchTools.__new__(ResearchTools)
        tools.config, tools.evidence = ResearchConfig(), {item.evidence_id: item for item in work.evidence}
        tools.presentation = BusinessPresentation(workspace, work.evidence)
        page = tools.evidence_page(source["evidence_id"], limit=600000)
        assert "Public-web reference (not a project fact)" in page["text"]
        assert source["source_id"] in page["text"] and source["retrieved_at"] in page["text"]
        assert source["text"] in page["text"] and page["evidence"][0] == source
        catalog = tools.evidence_page(query="example.org")
        assert "Public-web reference" in tools.presentation.evidence_catalog_text(catalog["items"])


@pytest.mark.parametrize("tamper", ["text", "project_fact", "finding", "undelivered", "requirement"])
async def test_frozen_public_receipts_cannot_forge_local_terms_or_provenance(researched_directory, tamper):
    root, _, _, loaded = researched_directory
    receipt = web_receipt()
    if tamper == "text":
        receipt["evidence"][0]["text"] = "本项目必须采购某指定产品。"
    elif tamper == "project_fact":
        receipt["evidence"][0]["metadata"]["project_fact"] = True
    elif tamper == "finding":
        receipt["findings"][0]["evidence_ids"] = ["invented"]
    elif tamper == "undelivered":
        receipt["delivered_evidence_ids"] = []
    report = attach_web_report(loaded, receipt)
    if tamper == "requirement":
        report["requirements"][0]["evidence_ids"] = [receipt["evidence"][0]["evidence_id"]]
        (loaded["folder"] / "research/report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        start_writing(root, loaded)


async def test_seed_revalidates_frozen_public_excerpt_before_changing_work(researched_directory):
    root, _, _, loaded = researched_directory
    attach_web_report(loaded, web_receipt())
    store, job_id, engine = start_writing(root, loaded)
    path = root / "jobs" / job_id / "inputs/directory-research/report.json"
    saved = json.loads(path.read_text())
    saved["web_research"]["evidence"][0]["text"] = "冻结记录被错误覆盖。"
    path.write_text(json.dumps(saved), encoding="utf-8")
    section = loaded["plan"].sections[0]
    task, work = writing_task(section), WorkState(task_id=section.node_id)
    workspace = WorkspaceManager(Path(store.get(job_id)["input"]["knowledge_path"]), root / "task")
    with pytest.raises(ValueError, match="identity"):
        seed_directory_research(engine, task, work, workspace)
    assert work.evidence == []


async def test_business_web_query_knows_directory_searches_without_banning_a_justified_refresh():
    prior = web_research_navigation(web_receipt())
    query = prior["searches"][0]["query"]
    events, calls, searches = [], [], []

    class Provider:
        async def search(self, value, **kwargs):
            searches.append(value)
            return []

    class Host(WebResearchWorkflow):
        job_id = "test"
        limits = SimpleNamespace(retrieval_limit=5, evidence_chars=600000)
        web_knowledge = Provider()
        store = SimpleNamespace(
            save_work_state=lambda *_: None, work_event=lambda *_: None,
            retrieval=lambda *_: None, save_retrieval=lambda *_: None,
            audit_event=lambda *args: events.append(args),
        )

        async def call(self, task, context, model):
            calls.append(context)
            return WebQuery(query=query)

    task = WorkTask(task_id="t", title="技术方案", objective="核对当前公开指南版本", section_ids=["s"],
                    questions=["指南是否更新"], completion_criteria=["保留来源"], rationale="技术参考")
    work = WorkState(task_id="t", missing=["是否已有新版指南"])
    await Host().supplement_web(task, work, questions=work.missing, previous_research=prior)
    assert calls[0]["previous_queries"] == [query]
    assert calls[0]["directory_web_research"] == prior
    assert searches == [query] and work.web_searches[0].status == "completed"
    assert events


async def test_existing_public_sources_reach_business_assessment_before_more_search(tmp_path):
    receipt = web_receipt()
    source = Evidence.model_validate(receipt["evidence"][0])
    prior = web_research_navigation(receipt)
    sources = tmp_path / "sources"
    sources.mkdir()
    workspace = WorkspaceManager(sources, tmp_path / "research")
    observed = []

    async def understand(task, context, model):
        observed.append(context)
        assert context["directory_research"]["web_research"] == prior
        assert context["evidence"][0]["text"] == source.text
        assert context["evidence"][0]["metadata"]["project_fact"] is False
        return BusinessRecord(summary="已有公开参考回答了本次公开技术问题", research_actions=[])

    engine = SimpleNamespace(call=understand, job_id="test", store=SimpleNamespace(save_work_state=lambda *_: None))
    task = SimpleNamespace(model_dump=lambda: {"task_id": "t", "title": "技术参考"})
    work = WorkState(task_id="t")
    tools = SimpleNamespace(
        evidence={source.evidence_id: source}, workspace=workspace,
        session={"missing": [], "notes": "", "step": 1},
        memory=SimpleNamespace(markdown=lambda **_: ""), presentation=SimpleNamespace(evidence_id=lambda key: key),
    )
    assignment = {"sections": [], "dependencies": [], "global_facts": {}, "writing_gaps": [],
                  "directory_research": {"web_research": prior}}
    await assess_research(engine, task, work, tools, assignment)
    assert len(observed) == 1 and tools.session["research_actions"] == [] and not work.missing


async def test_large_directory_reference_history_stays_navigable_without_disappearing():
    context = {"task": {"task_id": "t"}, "observations": [], "step": 0,
               "directory_research": {"web_research": {"findings": [{"text": "公开技术参考" * 2000}]}}}
    original = deepcopy(context)

    async def capacity(*args):
        return {"fits": len(json.dumps(args[1], ensure_ascii=False)) < 1500}

    view = await fit_context(SimpleNamespace(request_capacity=capacity), context, {})
    assert "directory_research" not in view
    assert view["context_navigation"]["directory_research"]["read"] == {
        "scope": "context", "field": "directory_research",
    }
    assert "公开技术参考" * 2000 in context_field_text(context, "directory_research")
    assert context == original
