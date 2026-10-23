"""Research termination, ordered fallback, resumable history and capacity views."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from native_calls import native_call, research_call

from folio.models import Evidence, JobInput, Limits
from folio.orchestration.engine import Engine
from folio.research.context_views import context_field_text, fit_context, read_text_page
from folio.research.contracts import ResearchConfig, ResearchStep
from folio.research.ledger import ResearchLedger
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


class RoundRuntime(DemoRuntime):
    def __init__(self, events, *, keep_reading=False, web_sufficient=False):
        self.events = events
        self.keep_reading = keep_reading
        self.web_sufficient = web_sufficient

    async def complete(self, task, context, schema, session):
        if task == "research_step":
            self.events.append(("local", context["local_round"]))
            return research_call("fs.list" if self.keep_reading else "finish",
                                 missing_information=["项目审批规则缺失"])
        if task == "web_query":
            return native_call("search_web", {"query": f"公开审批规范 {len(context['previous_queries'])}"})
        if task == "understand_work":
            has_web_answer = self.web_sufficient and any(
                item["evidence_id"] == "web_rules" for item in context["evidence"])
            return RuntimeResult(json.dumps({
                "summary": "公开规范不能冒充本项目审批事实", "facts": [],
                "open_questions": [] if has_web_answer else ["项目审批规则缺失"],
                "research_actions": [] if has_web_answer else [
                    *([{"channel": "local_sources", "question": "本地审批条款尚未核清",
                        "action": "检查 rules.md 中尚未核清的主管审批条款"}]
                      if context.get("research_phase") != "web" and context.get("local_round", 0) < 3 else []),
                    {"channel": "public_web", "question": "公开审批规范如何规定复核职责",
                     "action": "查找公开审批规范中关于复核职责的原文"},
                ],
            }))
        if task == "complete_gaps":
            self.events.append(("completion", len(context["gaps"])))
        return await super().complete(task, context, schema, session)


class Web:
    def __init__(self, events, *, found=False, cancel=False):
        self.events, self.found, self.cancel = events, found, cancel

    async def search(self, *args, **kwargs):
        self.events.append(("web", 1))
        assert {value for event, value in self.events if event == "local"} == {1, 2, 3}
        if self.cancel:
            raise asyncio.CancelledError()
        return [Evidence(evidence_id="web_rules", source_id="https://example.org/rules",
                         source_version="v1", locator="excerpt", text="公开审批规范",
                         metadata={"role": "public_web", "project_fact": False})] if self.found else []


def request(inputs, **config):
    inputs["user_request"] = "# 审批规则\n说明规则。"
    return JobInput(
        **inputs, runtime="demo", research=ResearchConfig(**config), limits=Limits(max_repairs=0)
    )


@pytest.mark.parametrize("keep_reading", [False, True])
async def test_three_local_rounds_then_two_web_then_marked_completion(store, inputs, keep_reading):
    events = []
    job_id = store.create(request(inputs, max_steps_per_round=2))
    engine = Engine(store, RoundRuntime(events, keep_reading=keep_reading), web_knowledge=Web(events))
    result = await engine.run(job_id)
    assert result["status"] == "draft"
    work = next(iter(store.work_states(job_id).values()))
    local_events = [value for event, value in events if event == "local"]
    # Repeated unchanged listings converge early without skipping the three local rounds.
    assert local_events == ([1, 1, 2, 2, 3] if keep_reading else [1, 2, 3])
    assert [event for event, _ in events][-3:] == ["web", "web", "completion"]
    assert work.research["local_rounds"] == 3
    assert work.research["fallback_ready"] and work.research["web_rounds"] == 2
    assert work.proposals and engine.model_completion_allowed(work)
    assert not work.record.facts
    before = list(events)
    await engine.research_work(engine.work_plan.tasks[0], writing_gaps=["新增缺口"])
    assert [event for event in events[len(before):] if event[0] in {"local", "web"}] == []


async def test_web_stops_after_first_sufficient_result(store, inputs):
    events = []
    job_id = store.create(request(inputs))
    result = await Engine(store, RoundRuntime(events, web_sufficient=True),
                          web_knowledge=Web(events, found=True)).run(job_id)
    # Public-source applicability still receives independent downstream review.
    assert result["status"] in {"draft", "needs_confirmation"}
    assert [event for event, _ in events].count("web") == 1
    assert not any(event == "completion" for event, _ in events)


async def test_cancelled_web_round_is_not_reset_on_recovery(store, inputs):
    events = []
    job_id = store.create(request(inputs))
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, RoundRuntime(events), web_knowledge=Web(events, cancel=True)).run(job_id)
    assert [value for event, value in events if event == "local"] == [1, 2, 3]
    result = await Engine(store, RoundRuntime(events), web_knowledge=Web(events)).run(job_id)
    assert result["status"] == "draft"
    assert [value for event, value in events if event == "local"] == [1, 2, 3]
    assert [event for event, _ in events].count("web") == 2


def test_round_reservations_reuse_recovery_but_not_new_repairs(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    assert ledger.begin_round("task", "first") == ledger.begin_round("task", "first") == 1
    assert ledger.begin_round("task", "repair") == 2
    ledger.finish_round("task", 2)
    assert ledger.begin_round("task", "repair") == 3
    ledger.finish_round("task", 3)
    assert ledger.begin_round("task", "another") is None
    assert ledger.begin_round("task:new-confirmation", "new") == 1


def test_old_tool_result_is_searchable_pageable_and_task_scoped(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    result = {"text": "关键条件" + "旧资料" * 3000}
    ledger.begin("old", "owner", "round", 0, "fs.read", {"path": "rules.md"}, config=ResearchConfig())
    ledger.finish("old", result)
    for i in range(12):
        ledger.begin(str(i), "owner", "round", i + 1, "fs.list", {}, config=ResearchConfig())
        ledger.finish(str(i), {"files": []})
    assert ledger.history("owner", query="关键条件")["items"][0]["request_id"] == "old"
    chunks, offset = [], 0
    while offset is not None:
        page = ledger.history("owner", request_id="old", offset=offset, limit=2000)
        chunks.append(page["text"])
        offset = page["next_offset"]
    assert json.loads("".join(chunks)) == result
    with pytest.raises(ValueError, match="does not belong"):
        ledger.history("other", request_id="old")


async def test_oversized_facts_become_navigable_without_mutating_snapshot():
    context = {"task": {"task_id": "task", "title": "审批", "section_ids": ["s"]},
               "sections": [{"section_id": "s", "title": "审批", "instruction": "完整职责"}],
               "global_facts": {"s": [{"statement": "不得自行审批" * 5000}]},
               "observations": [], "tools": [], "step": 0}
    original = deepcopy(context)

    async def capacity(task, value, schema):
        return {"fits": len(json.dumps(value, ensure_ascii=False)) < 2500}

    view = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    assert context == original
    assert "global_facts" not in view
    assert "global_facts" in view["context_navigation"]
    full_text = context_field_text(context, "global_facts")
    assert original["global_facts"]["s"][0]["statement"] in full_text
    assert view["context_navigation"]["global_facts"]["total_chars"] == len(full_text)
    chunks, offset = [], 0
    while offset is not None:
        page = read_text_page(full_text, offset=offset, limit=1200)
        chunks.append(page["text"])
        offset = page["next_offset"]
    assert "".join(chunks) == full_text
    assert context == original


async def test_second_web_result_saved_before_assessment_is_reassessed_on_resume(store, inputs, monkeypatch):
    import folio.research.workflow as workflow

    events = []

    class SecondResultWeb(Web):
        calls = 0

        async def search(self, *args, **kwargs):
            self.calls += 1
            self.found = self.calls == 2
            return await super().search(*args, **kwargs)

    interrupted = False
    original = workflow.assess_research

    async def interrupt_before_second_web_assessment(engine, task, work, tools, assignment):
        nonlocal interrupted
        if len(work.web_searches) == 2 and any(e.evidence_id == "web_rules" for e in work.evidence) and not interrupted:
            persisted = engine.store.work_states(engine.job_id)[task.task_id]
            assert any(e.evidence_id == "web_rules" for e in persisted.evidence)
            interrupted = True
            raise asyncio.CancelledError()
        return await original(engine, task, work, tools, assignment)

    monkeypatch.setattr(workflow, "assess_research", interrupt_before_second_web_assessment)
    web = SecondResultWeb(events)
    job_id = store.create(request(inputs))
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, RoundRuntime(events, web_sufficient=True), web_knowledge=web).run(job_id)
    assert interrupted and web.calls == 2
    resumed = Engine(store, RoundRuntime(events, web_sufficient=True), web_knowledge=web)
    result = await resumed.run(job_id)
    assert result["status"] in {"draft", "needs_confirmation"}
    work = next(iter(store.work_states(job_id).values()))
    assert web.calls == 2
    assert [value for event, value in events if event == "local"] == [1, 2, 3]
    assert work.research["phase"] == "ready"
    assert not work.research["fallback_ready"]
    assert not work.missing and not work.proposals
    assert not any(event == "completion" for event, _ in events)


async def test_step_cap_assessment_can_resolve_gap_without_operational_proposals(store, inputs):
    events, assessments = [], []

    class ReadUntilCap(RoundRuntime):
        async def complete(self, task, context, schema, session):
            if task == "research_step":
                self.events.append(("local", context["local_round"]))
                return research_call("fs.read", {"path": "rules.md"},
                                     missing_information=["项目审批规则缺失"])
            if task == "understand_work":
                assessments.append(deepcopy(context))
                return RuntimeResult(json.dumps({
                    "summary": "现有资料已明确主管审批规则", "facts": [], "open_questions": [],
                }))
            return await super().complete(task, context, schema, session)

    class UnexpectedWeb:
        async def search(self, *args, **kwargs):
            pytest.fail("resolved local gap should not use web search")

    job_id = store.create(request(inputs, max_steps_per_round=1))
    result = await Engine(store, ReadUntilCap(events), web_knowledge=UnexpectedWeb()).run(job_id)
    assert result["status"] in {"draft", "needs_confirmation"}
    assert assessments and "项目审批规则缺失" in assessments[0]["writing_gaps"]
    assert all("步骤已用完" not in gap for assessment in assessments for gap in assessment["writing_gaps"])
    work = next(iter(store.work_states(job_id).values()))
    assert [value for event, value in events if event == "local"] == [1]
    assert not work.missing and not work.proposals and not work.web_searches
    assert not work.research["fallback_ready"]
    assert not any(event == "completion" for event, _ in events)
