"""A completed source review must not loop to obtain private bidder facts."""

import json
from copy import deepcopy

import pytest
from native_calls import research_call
from test_research_rounds_context import request

from folio.orchestration.engine import Engine
from folio.research.ledger import ResearchLedger
from folio.runtime.base import INSTRUCTIONS, RuntimeResult
from folio.runtime.demo import DemoRuntime

# The kinds of unanswered items left by the failing tender run.
PENDING = [
    "投标人基本主体信息、报价及人员证书待提供",
    "现网技术栈、GPU配额与部署资源需要用户确认",
]


class PendingBidRuntime(DemoRuntime):
    def __init__(self):
        self.research_calls = 0
        self.assessments = []
        self.gap_contexts = []
        self.reject_research = False

    async def complete(self, task, context, schema, session):
        if task == "research_step":
            assert not self.reject_research, "a saved finish must not reopen source reading"
            self.research_calls += 1
            if self.research_calls == 1:
                return research_call("fs.read", {"path": "rules.md"})
            assert self.research_calls == 2, "user-held facts cannot be obtained by rereading"
            return research_call("finish", missing_information=PENDING)
        if task == "understand_work":
            self.assessments.append(deepcopy(context))
            return RuntimeResult(json.dumps({
                "summary": "招标要求已读完，投标人实际信息尚待提供",
                "facts": [], "open_questions": PENDING, "research_actions": [],
            }, ensure_ascii=False))
        if task == "complete_gaps":
            self.gap_contexts.append(deepcopy(context))
            return RuntimeResult(json.dumps({"proposals": [
                {"gap_id": gap["gap_id"], "section_ids": [s["section_id"] for s in context["sections"]],
                 "disposition": "record_only",
                 "content": "此项为投标人真实情况，需由用户提供；保留待确认状态和空白，不编造具体值。"}
                for gap in context["gaps"]
            ]}, ensure_ascii=False))
        return await super().complete(task, context, schema, session)


class NoPublicSearch:
    async def search(self, *args, **kwargs):
        pytest.fail("private bidder facts must not be searched on the public web")


async def test_finished_bid_research_keeps_user_gaps_without_new_reads_or_web(store, inputs):
    runtime = PendingBidRuntime()
    job_id = store.create(request(inputs))
    engine = Engine(store, runtime, web_knowledge=NoPublicSearch())
    result = await engine.run(job_id)

    assert result["status"] in {"draft", "needs_confirmation"}
    work = next(iter(store.work_states(job_id).values()))
    assert runtime.research_calls == 2 and len(runtime.assessments) == 1
    assert work.research["local_rounds"] == 1
    assert work.research["web_status"] == "not_needed" and not work.web_searches
    assert work.missing == PENDING and work.record.open_questions == PENDING
    assert work.researched and work.writable and not work.record.facts
    assert {gap["question"] for gap in runtime.gap_contexts[0]["gaps"]} == set(PENDING)
    assert work.proposals and all(item.disposition == "record_only" for item in work.proposals)
    assert "quotations" in INSTRUCTIONS["understand_work"]
    assert "Never invent" in INSTRUCTIONS["generate"]


@pytest.mark.parametrize("inherited", [False, True])
async def test_legacy_finish_is_reassessed_once_before_any_resumed_read(store, inputs, inherited):
    runtime = PendingBidRuntime()
    engine = Engine(store, runtime, web_knowledge=NoPublicSearch())
    job_id = store.create(request(inputs))
    await engine.run(job_id)
    task = engine.work_plan.tasks[0]
    work = store.work_states(job_id)[task.task_id]
    ledger = ResearchLedger(store.root / "jobs" / job_id / "research" / "ledger.sqlite")
    saved = ledger.load_session(task.task_id, work.research["round_id"])
    assert any(item["tool"] == "finish" for item in saved["observations"])
    saved.pop("assessment_version")
    saved.pop("research_actions")
    saved.update(phase="local", finished=False)
    if inherited:
        saved["inherited_from"] = "previous-compatible-research"
    ledger.save_session(task.task_id, work.research["round_id"], saved)
    work.researched = work.writable = False
    store.save_work_state(job_id, work)
    usage = ledger.usage(task.task_id)
    runtime.reject_research = True

    resumed = await engine.research_work(task)

    assert resumed.missing == PENDING and not resumed.record.facts
    assert ledger.usage(task.task_id) == usage
    updated = ledger.load_session(task.task_id, work.research["round_id"])
    assert updated["assessment_version"] == 2 and updated["finished"]
    assert updated["observations"] == saved["observations"]
    assert updated["research_memory"]["notes"] == saved["research_memory"]["notes"]
    assert len(runtime.assessments) == 2
    await engine.research_work(task)
    assert len(runtime.assessments) == 2


async def test_public_search_receives_only_explicit_public_questions(store, inputs):
    from native_calls import native_call

    from folio.models import Evidence

    public_question = "公开规范是否要求主管复核"
    queries = []

    class MixedRuntime(PendingBidRuntime):
        async def complete(self, task, context, schema, session):
            if task == "web_query":
                queries.append(deepcopy(context))
                return native_call("search_web", {"query": public_question})
            result = await super().complete(task, context, schema, session)
            if task == "understand_work":
                record = json.loads(result.content)
                if not any(item["evidence_id"] == "public-rule" for item in context["evidence"]):
                    record["research_actions"] = [{
                        "channel": "public_web", "question": public_question,
                        "action": "查看公开审批规范的复核条款",
                    }]
                return RuntimeResult(json.dumps(record, ensure_ascii=False))
            return result

    class PublicSearch:
        calls = 0

        async def search(self, *args, **kwargs):
            self.calls += 1
            return [Evidence(evidence_id="public-rule", source_id="https://example.org/rule",
                             source_version="v1", locator="section-1", text="公开复核要求",
                             metadata={"role": "public_web", "project_fact": False})]

    web = PublicSearch()
    runtime = MixedRuntime()
    job_id = store.create(request(inputs))
    await Engine(store, runtime, web_knowledge=web).run(job_id)
    work = next(iter(store.work_states(job_id).values()))

    assert web.calls == 1 and len(queries) == 1
    assert queries[0]["missing_information"] == [public_question]
    assert work.missing == PENDING and work.record.open_questions == PENDING
    assert runtime.research_calls == 2 and len(runtime.assessments) == 2


async def test_partitioned_understanding_preserves_actions_and_pending_questions():
    from types import SimpleNamespace

    from folio.orchestration.requests import RequestWorkflow
    from folio.orchestration.work import BusinessRecord

    class SmallCapacity(RequestWorkflow):
        job_id = "test"
        store = SimpleNamespace(work_event=lambda *_: None)

        async def request_capacity(self, task, context, model):
            return {"fits": len(context["evidence"]) == 1}

        async def _call(self, task, context, model):
            channel = context["evidence"][0]["channel"]
            assert context["read_batch"] is True
            return BusinessRecord(
                open_questions=PENDING,
                research_actions=[{"channel": channel, "question": "仍需核对" + channel,
                                   "action": "核对该来源的具体适用范围"}],
            )

    result = await SmallCapacity().call_bounded("understand_work", {
        "evidence": [{"channel": "local_sources"}, {"channel": "public_web"}],
    }, BusinessRecord)
    assert result.open_questions == PENDING
    assert [action.channel for action in result.research_actions] == ["local_sources", "public_web"]
