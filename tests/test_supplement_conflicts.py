"""Conflict checks read every source before accepting model-origin supplements."""

import asyncio
import json
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

import pytest

from folio.models import JobInput
from folio.orchestration import supplement_conflicts as module
from folio.orchestration.engine import Engine as DurableEngine
from folio.runtime.base import RuntimeResult, prompt_for
from folio.runtime.errors import ModelCallFailed


def proposal(gap="g1", content="补写方案规定交易必须经过主管审批后才能完成处理。"):
    return {"gap_id": gap, "section_ids": ["s1"], "content": content, "disposition": "body"}


def material(ref="source1", text="所有交易必须经过主管审批，不得跳过审批。", kind="evidence"):
    return {"context_ref": ref, "kind": kind, "value": {"text": text, "evidence_id": "e_" + ref}}


def clear(context, constraints=()):
    return {
        "checks": [{"gap_id": p["gap_id"], "status": "compatible"} for p in context["proposals"]],
        "constraints": list(constraints),
    }


def constraint(context, quote, *, proposal_quote="", ref=None, kind="evidence"):
    return {
        "gap_ids": [context["proposals"][0]["gap_id"]],
        "source_ref": ref or context["catalog"][0]["context_ref"],
        "source_kind": kind,
        "source_quote": quote,
        "proposal_quote": proposal_quote,
        "reason": "必须遵循该原始资料中的适用规则。",
    }


class Engine:
    def __init__(self, handler, limit=30000):
        self.handler, self.limit = handler, limit
        self.requests, self.measured, self.events = [], [], []
        self.active = self.maximum = 0
        self.job_id = "test-job"
        self.store = SimpleNamespace(work_event=lambda job_id, text: self.events.append(text))

    def size(self, task, context, model):
        # Count the actual full prompt + schema, not just the source text.
        return (
            len(module.TASK_INSTRUCTIONS[task])
            + len(json.dumps(context, ensure_ascii=False))
            + len(json.dumps(model.model_json_schema(), ensure_ascii=False))
        )

    async def measure_request(self, task, context, model):
        await asyncio.sleep(0)
        self.measured.append((task, deepcopy(context)))
        return {"fits": self.size(task, context, model) <= self.limit}

    async def _call(self, task, context, model):
        assert self.size(task, context, model) <= self.limit
        self.requests.append((task, deepcopy(context)))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
            value = self.handler(task, context)
            if asyncio.iscoroutine(value):
                value = await value
            return model.model_validate(value)
        finally:
            self.active -= 1


async def check(engine, proposals, materials):
    return await module.check_supplement_conflicts(
        engine, {"section_id": "s1", "title": "约束和假设"}, proposals, materials
    )


async def test_22_sources_and_8_proposals_cover_every_pair_without_rewriting():
    proposals = [proposal(f"g{i}", "这是与原始资料不矛盾的补写规则，" * 24) for i in range(8)]
    materials = [material(f"r{i}", "必须核对适用范围，不得跳过既定审批。" * 110) for i in range(22)]
    original = deepcopy((proposals, materials))
    engine = Engine(lambda task, context: clear(context))
    result = await check(engine, proposals, materials)
    assert result.status == "checked" and result.proposals == proposals
    assert (proposals, materials) == original
    covered = {
        (p["gap_id"], m["context_ref"])
        for task, context in engine.requests
        for p in context["proposals"]
        for m in context["catalog"]
    }
    assert covered == {(p["gap_id"], m["context_ref"]) for p in proposals for m in materials}
    assert all(task == module.CHECK_TASK for task, _ in engine.requests)
    assert 1 < engine.maximum <= 4 and result.calls <= module.MAX_CALLS
    assert all("《约束和假设》" in event for event in engine.events)


async def test_compatible_constraints_preserve_trace_and_source_kind():
    source = material(text="本方案仅适用于机构客户。", kind="fact")
    engine = Engine(
        lambda task, ctx: clear(ctx, [constraint(ctx, "本方案仅适用于机构客户。", kind="fact")])
    )
    result = await check(engine, [proposal()], [source])
    assert result.status == "checked"
    assert result.constraints[0]["quote"] == "本方案仅适用于机构客户。"
    assert result.constraints[0]["source_ref"] == source["context_ref"]
    assert result.constraints[0]["source_kind"] == "fact"
    assert "source_quote" not in result.constraints[0]
    assert [task for task, _ in engine.requests] == [module.CHECK_TASK]


async def test_tail_negation_is_checked_then_corrected_and_all_pages_rechecked():
    bad = "补写方案认为所有交易可跳过审批，直接交割且不再进行审批核验。"
    good = "补写方案遵循主管审批要求，全部交易通过主管审批后方可交割。"
    tail = "所有交易不得跳过主管审批。"
    source = material(text="适用业务背景与操作说明。" * 800 + tail)

    def handler(task, context):
        if task == module.REPAIR_TASK:
            assert context["constraints"][0]["source_quote"] == tail
            return {"changes": [{"gap_id": "g1", "content": good}], "unresolved": False}
        item = context["catalog"][0]
        text = item["value"] if isinstance(item["value"], str) else item["value"]["text"]
        if tail in text and context["proposals"][0]["content"] == bad:
            return {
                "checks": [{"gap_id": "g1", "status": "conflict"}],
                "constraints": [constraint(context, tail, proposal_quote="可跳过审批")],
            }
        return clear(context, [constraint(context, tail)] if tail in text else [])

    engine = Engine(handler, limit=5400)
    result = await check(engine, [proposal(content=bad)], [source])
    assert result.status == "checked" and result.proposals == [proposal(content=good)]
    checked = [ctx for task, ctx in engine.requests if task == module.CHECK_TASK]
    before = [ctx["catalog"][0] for ctx in checked if ctx["proposals"][0]["content"] == bad]
    after = [ctx["catalog"][0] for ctx in checked if ctx["proposals"][0]["content"] == good]
    assert len(before) > 1 and len(before) == len(after)
    assert before == after
    assert "".join(
        item["value"] for item in sorted(before, key=lambda item: item["fragment"]["start"])
    ) == module._encoded(source["value"])
    assert result.constraints[0]["quote"] == tail
    assert "source_span" in result.constraints[0]


async def test_opposing_pages_cannot_last_win_and_pending_keeps_original():
    initial = "本补写规定审批只能由甲岗位执行，其他岗位不得进行该审批。"
    alternate = "本补写规定审批只能由乙岗位执行，其他岗位不得进行该审批。"
    a, b = "审批只能由甲岗位执行。", "审批只能由乙岗位执行。"
    sources = [material("a", a + "背景" * 1500), material("b", b + "背景" * 1500)]
    revisions = []

    def handler(task, context):
        current = context["proposals"][0]["content"]
        if task == module.REPAIR_TASK:
            revisions.append(current)
            return {
                "changes": [
                    {"gap_id": "g1", "content": alternate if current == initial else initial}
                ],
                "unresolved": False,
            }
        ref = context["catalog"][0]["context_ref"]
        quote = b if ref == "b" else a
        contradicted = (ref == "b" and current == initial) or (ref == "a" and current == alternate)
        notes = [
            constraint(context, quote, proposal_quote="甲岗位" if current == initial else "乙岗位")
        ]
        return {
            "checks": [{"gap_id": "g1", "status": "conflict" if contradicted else "compatible"}],
            "constraints": notes,
        }

    engine = Engine(handler, limit=6600)
    original = [proposal(content=initial)]
    result = await check(engine, original, sources)
    assert result.status == "pending" and result.proposals == original
    assert len(revisions) == 2
    assert "two correction rounds" in result.reason
    checks = [ctx for task, ctx in engine.requests if task == module.CHECK_TASK]
    assert len(checks) == 6  # both original sources after each correction


@pytest.mark.parametrize(
    "mutation", ["reference", "kind", "source_quote", "proposal_quote", "missing_verdict"]
)
async def test_invalid_grounding_cannot_be_accepted(mutation):
    def handler(task, context):
        note = constraint(context, "不得跳过审批", proposal_quote="主管审批")
        checks = [{"gap_id": "g1", "status": "conflict"}]
        if mutation == "reference":
            note["source_ref"] = "another-page-source"
        elif mutation == "kind":
            note["source_kind"] = "fact"
        elif mutation == "source_quote":
            note["source_quote"] = "不存在的否定规则"
        elif mutation == "proposal_quote":
            note["proposal_quote"] = "方案没有这句话"
        else:
            checks[0]["gap_id"] = "unknown-gap"
        return {"checks": checks, "constraints": [note]}

    original = [proposal()]
    engine = Engine(handler)
    result = await check(engine, original, [material()])
    assert result.status == "pending" and result.proposals == original
    assert len(engine.requests) == 1


async def test_fact_or_proposal_conflict_does_not_become_evidence():
    def handler(task, context):
        return {
            "checks": [{"gap_id": "g1", "status": "conflict"}],
            "constraints": [
                constraint(context, "所有交易不得审批", proposal_quote="主管审批", kind="proposal")
            ],
        }

    result = await check(
        Engine(handler), [proposal()], [material(text="所有交易不得审批", kind="proposal")]
    )
    assert result.status == "pending" and "Index entries or model suggestions cannot" in result.reason


async def test_service_error_and_uncertainty_preserve_originals():
    original = [proposal()]

    def unavailable(*_):
        raise ModelCallFailed("secret-upstream-body", "timeout")

    engine = Engine(unavailable)
    result = await check(engine, original, [material()])
    assert result.status == "pending" and result.proposals == original
    assert "secret" not in result.reason and "secret" not in str(engine.events)
    uncertain = Engine(lambda task, ctx: {"checks": [{"gap_id": "g1", "status": "uncertain"}]})
    assert (await check(uncertain, original, [material()])).status == "pending"


async def test_external_cancellation_drains_all_workers():
    started = asyncio.Event()

    async def waiting(*_):
        started.set()
        await asyncio.Event().wait()

    engine = Engine(waiting)
    task = asyncio.create_task(check(engine, [proposal()], [material(str(i)) for i in range(20)]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.active == 0


async def test_budget_rejection_does_not_silently_skip_sources(monkeypatch):
    monkeypatch.setattr(module, "MAX_CALLS", 2)
    engine = Engine(lambda task, ctx: clear(ctx))
    original = [proposal()]
    result = await check(engine, original, [material(str(i)) for i in range(22)])
    assert result.status == "pending" and result.proposals == original
    assert len(engine.requests) <= 2


async def test_common_context_too_large_and_timeout_are_pending(monkeypatch):
    engine = Engine(lambda task, ctx: clear(ctx), limit=5)
    result = await check(engine, [proposal()], [material()])
    assert result.status == "pending" and not engine.requests
    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.001)

    async def slow(*_):
        await asyncio.sleep(30)

    timed = Engine(slow)
    result = await check(timed, [proposal()], [material()])
    assert result.status == "pending" and "time limit" in result.reason and timed.active == 0


async def test_runtime_output_limit_splits_both_axes_instead_of_repeating():
    failed = []

    def handler(task, context):
        if len(context["proposals"]) > 1 or len(context["catalog"]) > 1:
            identity = json.dumps(context, sort_keys=True)
            assert identity not in failed
            failed.append(identity)
            raise ModelCallFailed("output incomplete", "length_limit")
        return clear(context)

    engine = Engine(handler)
    proposals = [proposal("g1"), proposal("g2")]
    sources = [material("r1"), material("r2")]
    result = await check(engine, proposals, sources)
    assert result.status == "checked" and failed
    accepted = [
        ctx for _, ctx in engine.requests if len(ctx["proposals"]) == len(ctx["catalog"]) == 1
    ]
    assert {
        (ctx["proposals"][0]["gap_id"], ctx["catalog"][0]["context_ref"]) for ctx in accepted
    } == {("g1", "r1"), ("g1", "r2"), ("g2", "r1"), ("g2", "r2")}


async def test_correction_cannot_change_unrelated_gap_or_ownership():
    def handler(task, context):
        if task == module.REPAIR_TASK:
            return {
                "changes": [{"gap_id": "unknown", "content": proposal()["content"]}],
                "unresolved": False,
            }
        return {
            "checks": [{"gap_id": "g1", "status": "conflict"}],
            "constraints": [constraint(context, "不得跳过审批", proposal_quote="主管审批")],
        }

    original = [proposal()]
    result = await check(Engine(handler), original, [material()])
    assert result.status == "pending" and result.proposals == original


async def test_call_reservations_do_not_race_during_token_measurement(monkeypatch):
    monkeypatch.setattr(module, "MAX_CALLS", 1)
    engine = Engine(lambda task, ctx: clear(ctx))
    checker = module._Checker(engine, {"title": "并发检查"}, [proposal()], [material()])
    context = checker.context([proposal()], [material()])
    results = await asyncio.gather(
        *(checker.invoke(module.CHECK_TASK, context, module.SupplementPageCheck) for _ in range(4)),
        return_exceptions=True,
    )
    assert len(engine.requests) == checker.calls == 1
    assert sum(isinstance(result, module._Pending) for result in results) == 3


async def test_real_engine_durable_page_results_survive_restart(store, inputs):
    class Runtime:
        identity = {"test": "durable-supplement"}
        config = {}

        def __init__(self):
            self.calls = 0

        async def measure(self, task, context, schema, limit):
            size = len(prompt_for(task, context, schema))
            return {"fits": size <= limit, "characters": size}

        async def complete(self, task, context, schema, session):
            self.calls += 1
            return RuntimeResult(json.dumps(clear(context)), usage={"total_tokens": 20})

    request = JobInput(**inputs)
    job_id = store.create(request)
    runtime = Runtime()

    def engine_for():
        engine = DurableEngine(store, runtime)
        engine.job_id, engine.request, engine.limits = job_id, request, request.limits
        return engine

    proposals, sources = [proposal()], [material(str(i)) for i in range(10)]
    first = await check(engine_for(), proposals, sources)
    assert first.status == "checked" and runtime.calls > 1
    calls = runtime.calls
    second = await check(engine_for(), proposals, sources)
    assert second.status == "checked" and second.proposals == first.proposals
    assert runtime.calls == calls
    assert second.calls == 0  # Cached pages consume no model-attempt allowance.
    with store.connect() as conn:
        rows = conn.execute("SELECT result,usage FROM tasks WHERE job_id=?", (job_id,)).fetchall()
    assert len(rows) == calls and all(
        row["result"] and json.loads(row["usage"])["total_tokens"] == 20 for row in rows
    )


async def test_multiple_supplements_share_one_call_budget():
    engine = Engine(lambda task, ctx: clear(ctx))
    budget = module.SupplementCheckBudget(max_calls=1)
    section = {"section_id": "s1", "title": "约束"}
    first = await module.check_supplement_conflicts(
        engine, section, [proposal("g1")], [material()], budget=budget
    )
    second = await module.check_supplement_conflicts(
        engine, section, [proposal("g2")], [material()], budget=budget
    )
    assert first.status == "checked" and first.calls == 1
    assert second.status == "pending" and second.calls == 0
    assert budget.calls == len(engine.requests) == 1 and budget._reserved == 0


async def test_shared_deadline_is_not_reset_between_items():
    async def handler(task, context):
        await asyncio.sleep(0.03)
        return clear(context)

    engine = Engine(handler)
    budget = module.SupplementCheckBudget(timeout_seconds=0.05)
    deadline = budget.deadline
    section = {"title": "约束"}
    first = await module.check_supplement_conflicts(
        engine, section, [proposal("g1")], [material()], budget=budget
    )
    assert first.status == "checked"
    second = await module.check_supplement_conflicts(
        engine, section, [proposal("g2")], [material()], budget=budget
    )
    assert second.status == "pending" and "time limit" in second.reason
    assert budget.deadline == deadline
    assert monotonic() - deadline < 0.2 and engine.active == budget._reserved == 0


async def test_shared_budget_counts_actual_attempts_and_refunds_cached_reads():
    from folio.orchestration.review_trace import record_cache_hit, record_model_call

    class Instrumented(Engine):
        async def _call(self, task, context, model):
            if context["proposals"][0]["gap_id"] == "cached":
                record_cache_hit(task)
            else:
                record_model_call(task)
            return model.model_validate(clear(context))

    engine = Instrumented(None)
    budget = module.SupplementCheckBudget(max_calls=1)
    section = {"title": "约束"}
    cached = await module.check_supplement_conflicts(
        engine, section, [proposal("cached")], [material()], budget=budget
    )
    assert cached.status == "checked" and cached.calls == budget.calls == 0
    fresh = await module.check_supplement_conflicts(
        engine, section, [proposal("fresh")], [material()], budget=budget
    )
    assert fresh.status == "checked" and fresh.calls == budget.calls == 1
    assert budget._reserved == 0
