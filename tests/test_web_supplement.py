import asyncio
import json

import httpx
import pytest
from native_calls import action_call, native_call

from folio.knowledge.tavily import TavilyKnowledge, WebSearchUnavailable
from folio.models import Evidence, JobInput
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


class ResearchRuntime(DemoRuntime):
    def __init__(self):
        self.completed_gaps = 0

    async def complete(self, task, context, schema, session_key):
        if task == "query":
            return native_call("search_sources", {"queries": [f"知识库补查{len(context['previous_queries'])}"]})
        if task == "web_query":
            return native_call("search_web", {"query": f"公开交易标准{len(context['previous_queries'])}"})
        if task == "complete_gaps":
            self.completed_gaps += 1
        return await super().complete(task, context, schema, session_key)


class EmptyKnowledge:
    def __init__(self):
        self.calls = 0

    async def search(self, *args, **kwargs):
        self.calls += 1
        return []


def one_node(inputs):
    inputs["user_request"] = "# 公开交易标准\n说明该标准。"


async def test_web_is_only_after_three_internal_searches_and_stops_when_sufficient(store, inputs):
    one_node(inputs)
    kb, runtime = EmptyKnowledge(), ResearchRuntime()

    class Web:
        calls = 0

        async def search(self, query, **kwargs):
            assert kb.calls == 3
            self.calls += 1
            return [
                Evidence(
                    evidence_id="web_a",
                    source_id="https://standard.example/a",
                    source_version="v1",
                    locator="excerpt",
                    text="公开交易标准定义了数据交换字段。",
                    metadata={"role": "public_web"},
                )
            ]

    web = Web()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, runtime, kb, web_knowledge=web).run(job_id)
    assert result["status"] == "draft" and web.calls == 1 and runtime.completed_gaps == 0
    work = next(iter(store.work_states(job_id).values()))
    assert len(work.searches) == 3 and work.web_searches[0].status == "completed"
    assert work.web_searches[0].evidence_ids == ["web_a"]


async def test_two_unsuccessful_web_calls_then_model_completion(store, inputs):
    one_node(inputs)
    kb, web, runtime = EmptyKnowledge(), EmptyKnowledge(), ResearchRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, runtime, kb, web_knowledge=web).run(job_id)
    assert result["status"] == "draft" and kb.calls == 3 and web.calls == 2
    assert runtime.completed_gaps
    work = next(iter(store.work_states(job_id).values()))
    assert len(work.web_searches) == 2 and work.proposals


async def test_web_outage_is_nonblocking_and_record_has_no_exception_secret(store, inputs):
    one_node(inputs)

    class Broken(EmptyKnowledge):
        async def search(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("secret-server-response")

    web = Broken()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, ResearchRuntime(), EmptyKnowledge(), web_knowledge=web).run(job_id)
    assert result["status"] == "draft" and web.calls == 2
    for work in store.work_states(job_id).values():
        assert "secret-server-response" not in work.model_dump_json()
        assert all(s.status == "failed" for s in work.web_searches)


async def test_cancelled_web_attempt_is_reserved_and_not_replayed(store, inputs):
    one_node(inputs)

    class Interrupted(EmptyKnowledge):
        async def search(self, *args, **kwargs):
            self.calls += 1
            raise asyncio.CancelledError()

    web = Interrupted()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, ResearchRuntime(), EmptyKnowledge(), web_knowledge=web).run(job_id)
    work = next(iter(store.work_states(job_id).values()))
    assert len(work.web_searches) == 1 and work.web_searches[0].status == "started"
    remaining = EmptyKnowledge()
    result = await Engine(store, ResearchRuntime(), EmptyKnowledge(), web_knowledge=remaining).run(
        job_id
    )
    assert result["status"] == "draft" and remaining.calls == 1
    assert len(next(iter(store.work_states(job_id).values())).web_searches) == 2


async def test_disabled_web_preserves_previous_three_round_workflow(store, inputs):
    one_node(inputs)
    kb = EmptyKnowledge()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, ResearchRuntime(), kb).run(job_id)
    assert result["status"] == "draft" and kb.calls == 3
    work = next(iter(store.work_states(job_id).values()))
    assert work.web_searches == [] and work.proposals


@pytest.mark.parametrize("first_source", ["internal", "web"])
@pytest.mark.parametrize("writer_output", ["pending", "proposal"])
async def test_late_writer_gaps_use_remaining_web_budget_before_model_completion(
    store, inputs, first_source, writer_output
):
    one_node(inputs)

    def source(eid, role="knowledge"):
        return Evidence(
            evidence_id=eid,
            source_id="https://standard.example/" + eid,
            source_version="v1",
            locator="excerpt",
            text="公开标准定义了字段类型和长度。",
            metadata={"role": role},
        )

    class Internal(EmptyKnowledge):
        async def search(self, *args, **kwargs):
            self.calls += 1
            return (
                [source("internal_rule")] if first_source == "internal" and self.calls == 3 else []
            )

    class Web(EmptyKnowledge):
        async def search(self, *args, **kwargs):
            self.calls += 1
            initial = first_source == "web" and self.calls == 1
            return [source("web_initial" if initial else "web_writer_gap", "public_web")]

    class LateGapRuntime(ResearchRuntime):
        generation_completion_allowed = None

        async def complete(self, task, context, schema, session_key):
            if task == "next_action" and context.get("writing_gaps"):
                resolved = any(e["evidence_id"] == "web_writer_gap" for e in context["evidence"])
                return action_call("write" if resolved else "pending",
                                   summary="核对写作新发现的标准缺口",
                                   missing_information=[] if resolved else context["writing_gaps"])
            result = await super().complete(task, context, schema, session_key)
            if task == "generate":
                self.generation_completion_allowed = context["model_completion_allowed"]
                content = json.loads(result.content)
                if writer_output == "pending":
                    content["pending_items"] = ["需核对公开标准中的字段长度规则"]
                else:
                    content["blocks"].append(
                        {
                            "block_id": "new_proposal",
                            "text": "系统应校验字段的长度范围。",
                            "origin": "model_proposal",
                            "evidence_ids": [],
                        }
                    )
                return RuntimeResult(json.dumps(content, ensure_ascii=False))
            return result

    internal, web, runtime = Internal(), Web(), LateGapRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, runtime, internal, web_knowledge=web).run(job_id)
    assert result["status"] == "draft"
    assert internal.calls == 3 and web.calls == (1 if first_source == "internal" else 2)
    assert runtime.generation_completion_allowed is False
    assert runtime.completed_gaps == 0
    work = next(iter(store.work_states(job_id).values()))
    assert not work.missing and not work.proposals
    assert any(e.evidence_id == "web_writer_gap" for e in work.evidence)


async def test_tavily_request_and_provenance_are_bounded(monkeypatch):
    async def post(self, url, **kwargs):
        assert url == "https://api.tavily.com/search"
        assert kwargs["json"]["search_depth"] == "advanced"
        assert kwargs["json"]["include_answer"] is False
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.org/a", "title": "Standard", "content": "a" * 8000},
                    {"url": "https://example.org/a", "content": "duplicate"},
                    {"url": "javascript:alert(1)", "content": "bad"},
                    {"url": "https://example.org/b", "content": "b" * 8000},
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    results = await TavilyKnowledge("not-a-real-key").search("标准", limit=8, max_chars=9000)
    assert len(results) == 2 and sum(len(e.text) for e in results) == 9000
    assert results[0].text == "a" * 8000  # No hidden 6K cap on one source.
    assert results[1].text == "b" * 1000  # The explicit total budget still applies.
    assert all(e.retrieved_at and e.metadata["role"] == "public_web" for e in results)
    assert all(not e.metadata["project_fact"] for e in results)


async def test_tavily_error_hides_upstream_echo(monkeypatch):
    async def post(self, url, **kwargs):
        return httpx.Response(401, text="secret-input-echo")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(WebSearchUnavailable, match="http_401") as exc:
        await TavilyKnowledge("not-a-real-key").search("standard", limit=8, max_chars=9000)
    assert "secret-input-echo" not in str(exc.value)
