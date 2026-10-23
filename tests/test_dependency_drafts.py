"""Downstream synthesis receives saved upstream drafts, not just extracted facts."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from native_calls import native_call, research_call

from folio.confirmations import ConfirmedReferences
from folio.knowledge.local import EmptyKnowledge
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import (
    Block,
    DocumentPlan,
    Evidence,
    Extraction,
    JobInput,
    Section,
    SectionContent,
    SectionState,
    digest,
)
from folio.orchestration.context import ContextSelection, dependency_research_identity
from folio.orchestration.engine import Engine
from folio.orchestration.work import BusinessRecord, WorkPlan, WorkState, WorkTask
from folio.research.context_views import context_field_text, fit_context, read_text_page
from folio.research.contracts import ResearchBatch, ResearchConfig
from folio.research.ledger import ResearchLedger
from folio.research.session_memory import compatibility_key
from folio.research.transport import research_messages
from folio.research.workflow import ResearchTools
from folio.runtime.demo import DemoRuntime
from folio.runtime.llm import LLMRuntime


def make_task(name, section_id, dependencies=()):
    return WorkTask(task_id=name, title=name, objective="根据已完成前文总结结论",
                    section_ids=[section_id], depends_on=list(dependencies),
                    questions=["前文有什么结论？"], completion_criteria=["忠实保留前文出处"],
                    rationale="总结任务需要前文的实际成稿")


class DraftRuntime(DemoRuntime):
    def __init__(self):
        self.research_contexts = []

    async def complete(self, task, context, schema, session):
        if task == "research_step":
            self.research_contexts.append(deepcopy(context))
            return research_call("finish", missing_information=[])
        return await super().complete(task, context, schema, session)


def prepared_engine(store, inputs):
    runtime = DraftRuntime()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo", research=ResearchConfig())
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    engine.plan = DocumentPlan(sections=[
        Section(section_id=name, node_id=name, title=name, level=1,
                display_number=str(index + 1), instruction="根据已完成前文总结结论")
        for index, name in enumerate(("upstream", "unrelated", "conclusion"))
    ])
    target = make_task("t6", "conclusion", ["t2"])
    engine.work_plan = WorkPlan(tasks=[make_task("t2", "upstream"),
                                     make_task("other", "unrelated"), target])
    store.save_work_plan(engine.job_id, engine.work_plan)
    engine.references = ConfirmedReferences([], "final", [])
    engine.provider = EmptyKnowledge()
    source = Evidence(evidence_id="draft-citation", source_id="report.pdf", source_version="v1",
                      locator="page 7", text="已核实的原文出处：容量限制影响产业投资。")
    content = SectionContent(blocks=[
        Block(block_id="original-paragraph", text="开头标记" + "完整论述。" * 1400 + "末尾标记",
              origin="evidence", evidence_ids=[source.evidence_id]),
        Block(block_id="original-table", kind="table", rows=[["项目", "结论"], ["供给", "受限"]],
              origin="evidence", evidence_ids=[source.evidence_id]),
        Block(block_id="original-proposal", text="【模型建议】后续关注供应链风险。",
              origin="model_proposal"),
    ])
    state = SectionState(section_id="upstream", revision=3, content=content,
                         extraction=Extraction(summary="摘要不能代替正文", claims=[]),
                         evidence=[source], validation_completed=True)
    store.save_section(engine.job_id, state)
    store.save_section(engine.job_id, state.model_copy(deep=True, update={
        "section_id": "unrelated", "content": SectionContent(blocks=[
            Block(block_id="private", text="不属于本任务依赖的正文")
        ]), "evidence": [source.model_copy(update={"evidence_id": "unrelated-citation"})],
    }))
    store.save_work_state(engine.job_id, WorkState(task_id="t2", review_key="reviewed-v3",
                                                 review_revisions={"upstream": 3}))
    return engine, target, state


async def test_full_draft_survives_selection_without_exposing_unrelated_work(store, inputs):
    engine, task, state = prepared_engine(store, inputs)
    dependencies = engine.dependency_context(task)
    assert [d["task_id"] for d in dependencies] == ["t2"]
    section = dependencies[0]["sections"][0]
    assert section["content"] == state.content.model_dump()
    assert section["review_status"] == "reviewed"

    async def reject_everything(*args, **kwargs):
        return [ContextSelection()]

    engine.select_reads = reject_everything
    result = await engine.prepare_context("understand_work", {
        "work_task": task.model_dump(), "sections": [], "evidence": [],
        "dependencies": dependencies,
    })
    inherited = result["dependencies"][0]["sections"][0]
    assert inherited["content"] == state.content.model_dump()
    assert inherited["section_id"] == "upstream" and inherited["revision"] == 3
    assert "不属于本任务依赖" not in json.dumps(result, ensure_ascii=False)
    state.revision += 1
    engine.store.save_section(engine.job_id, state)
    assert engine.dependency_context(task)[0]["sections"][0]["review_status"] == "saved"


async def test_v2_first_research_turn_receives_complete_draft_and_original_citations(store, inputs):
    engine, task, state = prepared_engine(store, inputs)
    work = await engine.research_work(task)
    context = engine.runtime.research_contexts[0]
    assert context["dependencies"][0]["sections"][0]["content"] == state.content.model_dump()
    assert {e.evidence_id for e in work.evidence} == {"draft-citation"}
    message = research_messages(context, {}, task="research_step")[1]["content"]
    assert "开头标记" in message and "末尾标记" in message
    assert "original-table" in message and "draft-citation" in message
    assert "model_proposal" in message
    assert "不属于本任务依赖" not in message


async def test_capacity_navigation_keeps_full_draft_readable_in_context(store, inputs):
    engine, task, state = prepared_engine(store, inputs)
    context = {"task": task.model_dump(), "dependencies": engine.dependency_context(task),
               "observations": []}
    tools = object.__new__(ResearchTools)
    tools.prepare_context_sources(context)

    async def capacity(*args):
        return {"fits": len(json.dumps(args[1], ensure_ascii=False)) < 2000}

    view = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchBatch)
    assert "dependencies" not in view
    assert view["context_navigation"]["dependencies"]["read"] == {
        "scope": "context", "field": "dependencies",
    }
    original = context_field_text(tools.context_sources, "dependencies")
    chunks, offset = [], 0
    while offset is not None:
        page = read_text_page(original, offset=offset, limit=1000)
        chunks.append(page["text"])
        offset = page["next_offset"]
    assert "".join(chunks) == original
    assert state.content.blocks[0].text in original
    assert "original-table" in original and "draft-citation" in original


async def test_adding_draft_view_preserves_existing_research_identity_and_memory(store, inputs):
    engine, task, state = prepared_engine(store, inputs)
    dependencies = engine.dependency_context(task)
    legacy_dependencies = dependency_research_identity(dependencies)
    old_key = digest(json.dumps([task.model_dump(), legacy_dependencies, ""], sort_keys=True))
    work = WorkState(task_id=task.task_id, context_key=old_key, researched=True,
                     research={"quota_task_id": task.task_id, "notes": "旧笔记保留", "local_rounds": 2})
    engine.store.save_work_state(engine.job_id, work)
    restored = await engine.research_work(task)
    assert restored.context_key == old_key
    assert restored.research == work.research
    assert not engine.runtime.research_contexts

    assignment = {"task": task.model_dump(), "dependencies": dependencies}
    fields = ("task", "sections", "source_snapshot_id", "facts_snapshot_id", "dependencies", "instruction")
    legacy_assignment = assignment | {"dependencies": legacy_dependencies}
    old_memory_key = digest(json.dumps([
        {field: legacy_assignment.get(field) for field in fields}, {}
    ], sort_keys=True))
    assert compatibility_key(assignment, {}, legacy_instruction=True) == old_memory_key
    memory_key = compatibility_key(legacy_assignment, {})
    assert compatibility_key(assignment, {}) == memory_key
    # A new repair question keeps compatible source knowledge; dependency
    # content added for display likewise does not alter the formal identity.
    assert compatibility_key(assignment | {"instruction": "补查订单撤销条件"}, {}) == memory_key
    changed = deepcopy(assignment)
    changed["dependencies"][0]["sections"][0]["revision"] += 1
    assert compatibility_key(changed, {}) != memory_key


async def test_paused_legacy_research_resumes_same_round_with_notes_and_new_drafts(store, inputs):
    engine, task, state = prepared_engine(store, inputs)
    current_dependencies = engine.dependency_context
    engine.dependency_context = lambda value: dependency_research_identity(current_dependencies(value))

    class PauseAfterRead(DraftRuntime):
        async def complete(self, action, context, schema, session):
            if action == "research_step":
                if context["step"] == 0:
                    return research_call("fs.read", {"scope": "context", "field": "dependencies"},
                                         notes="已读索引，等待前文成稿。")
                raise asyncio.CancelledError()
            return await super().complete(action, context, schema, session)

    engine.runtime = PauseAfterRead()
    with pytest.raises(asyncio.CancelledError):
        await engine.research_work(task)
    ledger = ResearchLedger(store.root / "jobs" / engine.job_id / "research" / "ledger.sqlite")
    old_rounds = ledger.local_rounds(task.task_id)
    old = ledger.load_session(task.task_id, old_rounds[0]["session_id"])
    engine.dependency_context = current_dependencies
    engine.runtime = DraftRuntime()

    work = await engine.research_work(task)

    resumed = engine.runtime.research_contexts[0]
    assert resumed["step"] == old["step"] == 1
    assert resumed["notes"] == old["notes"] == "已读索引，等待前文成稿。"
    assert resumed["observations"][0]["request_id"] == old["observations"][0]["request_id"]
    assert resumed["dependencies"][0]["sections"][0]["content"] == state.content.model_dump()
    assert work.research["round_id"] == old["round_id"]
    assert len(ledger.local_rounds(task.task_id)) == len(old_rounds) == 1


async def test_actual_http_messages_deliver_body_then_old_error_then_current_version(store, inputs, tmp_path):
    engine, task, state = prepared_engine(store, inputs)
    old_call = native_call("fs_read", {
        "scope": "context", "field": "context_navigation", "summary": "旧导航查询",
    }).tool_calls[0]
    context = {
        "task": task.model_dump(), "dependencies": engine.dependency_context(task),
        "observations": [{"request_id": "old-error", "tool": "fs.read", "tool_call": old_call,
                          "model_text": "执行失败：Unknown context field"}],
    }
    settings = ModelSettingsStore(tmp_path / "mock-model")
    settings.save(ModelSettingsInput(base_url="http://model.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    runtime.capacity.ready = True
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        chunk = {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "finish-now", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"summary": "基于现有正文核对", "missing_information": []}),
            },
        }]}, "finish_reason": "tool_calls"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await runtime.complete("research_step", context, ResearchBatch.model_json_schema(), "offline")
        assert len(sent) == 1
        messages = sent[0]["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
        assert state.content.blocks[0].text in messages[1]["content"]
        assert "draft-citation" in messages[1]["content"]
        assert messages[2]["tool_calls"] == [old_call]
        assert "Unknown context field" in messages[3]["content"]
        assert messages[3]["tool_call_id"] == old_call["id"]
        assert "t2 / upstream / revision=3" in messages[4]["content"]
        assert "Complete body: 3 blocks" in messages[4]["content"]
        assert "Historical read failures remain accurate records of their time" in messages[4]["content"]

        assessment = {
            "work_task": task.model_dump(), "sections": [], "evidence": [],
            "dependencies": context["dependencies"], "writing_gaps": ["上游正文不可得"],
            "previous_record": {"open_questions": ["上游正文不可得"]},
        }
        wire, *_ = runtime.request_parts("understand_work", assessment, BusinessRecord.model_json_schema())
        assert state.content.blocks[0].text in wire[1]["content"]
        assert "Do not keep describing supplied upstream text as unavailable" in wire[0]["content"]

        hidden = context | {"dependencies": [], "context_navigation": {"dependencies": {"total_chars": 20000}}}
        no_draft_messages, *_ = runtime.request_parts("research_step", hidden, ResearchBatch.model_json_schema())
        assert no_draft_messages[-1]["role"] == "tool"
        assert not any("Dependency-body delivery status for this request" in (m.get("content") or "") for m in no_draft_messages)
    finally:
        await runtime.aclose()
