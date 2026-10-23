"""Selection overlaps; all edits still consume the newest document in item order."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from test_supplement_integration import (
    IntegrationRuntime,
    engine_for,
    proposal,
    source_context,
)

from folio.orchestration.supplement_conflicts import SupplementCheckResult
from folio.orchestration.supplement_integration import integrate_supplements
from folio.runtime.base import RuntimeResult
from folio.runtime.business_transport import business_payload
from folio.runtime.errors import ModelCallBudgetExceeded


def source_with(*names):
    source = source_context()
    source["model_proposals"] = [proposal(name) for name in names]
    return source


async def test_three_selections_start_before_first_edit_and_later_edits_stay_ordered(store, inputs):
    source = source_with("alpha", "beta", "gamma", "delta")
    source["model_proposals"].insert(1, proposal("metadata", disposition="record_only"))
    ready, editing, release_edit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    gates = {name: asyncio.Event() for name in ("alpha", "beta", "gamma", "delta")}
    started = []

    class WindowRuntime(IntegrationRuntime):
        async def complete(self, task, context, schema, session):
            if task == "select_context":
                name = context["purpose"]["supplement"]["gap_id"]
                started.append(name)
                if len(started) == 3:
                    ready.set()
                await gates[name].wait()
            elif context["integration_item"]["gap_id"] == "alpha":
                editing.set()
                await release_edit.wait()
            return await super().complete(task, context, schema, session)

    runtime = WindowRuntime()
    engine = engine_for(store, inputs, runtime)
    run = asyncio.create_task(integrate_supplements(engine, source, source["model_proposals"]))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert set(started) == {"alpha", "beta", "gamma"} and not runtime.integrations
        gates["alpha"].set()
        await asyncio.wait_for(editing.wait(), 1)
        assert "delta" not in started
        gates["beta"].set()
        gates["gamma"].set()
        gates["delta"].set()
        release_edit.set()
        result = await asyncio.wait_for(run, 1)
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    assert started == ["alpha", "beta", "gamma", "delta"]
    assert [c["integration_item"]["gap_id"] for c in runtime.integrations] == started
    assert [b.block_id for b in result.blocks[-4:]] == ["added_" + name for name in started]
    for index, context in enumerate(runtime.integrations):
        assert {"added_" + name for name in started[:index]} <= set(context["protected_block_ids"])


async def test_prefetch_cannot_replace_live_body_protection_sources_pending_or_schema(
    store, inputs
):
    source = source_with("alpha", "beta")
    source["evidence"].append({"evidence_id": "extra", "source_id": "original", "text": "alpha额外原文"})
    original = deepcopy(source)

    class LatestRuntime(IntegrationRuntime):
        async def complete(self, task, context, schema, session):
            output = await super().complete(task, context, schema, session)
            if task == "integrate_supplement":
                if context["integration_item"]["gap_id"] == "alpha":
                    patch = json.loads(output.content)
                    patch["updates"][0]["block_id"] = "si1"
                    patch["updates"].append(
                        context["current"]["blocks"][0]
                        | {"evidence_ids": ["source_alpha", "extra"]}
                    )
                    patch["resolved_pending_items"] = ["待补充适用范围"]
                    return RuntimeResult(json.dumps(patch))
                assert "si1" in context["protected_block_ids"]
                assert "extra" in context["existing_evidence_ids"]
                assert context["current"]["pending_items"] == ["审核日期待确认"]
                _, wire_schema, _ = business_payload(context, schema)
                assert "si1" not in {
                    bid
                    for branch in wire_schema["$defs"]["Block"]["anyOf"]
                    for bid in branch["properties"]["block_id"]["enum"]
                }
                assert wire_schema["properties"]["resolved_pending_items"]["items"]["enum"] == [
                    "审核日期待确认"
                ]
            return output

    runtime = LatestRuntime()
    engine = engine_for(store, inputs, runtime)
    prepare = engine.prepare_context

    async def selection_only(task, context):
        assert not {"current", "protected_block_ids", "existing_evidence_ids"} & context.keys()
        selected = await prepare(task, context)
        return selected | {
            "current": {"stale": True},
            "protected_block_ids": [],
            "existing_evidence_ids": [],
            "integration_item": proposal("wrong"),
        }

    engine.prepare_context = selection_only
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert result.blocks[-2].block_id == "si1" and result.blocks[-1].block_id == "added_beta"
    assert source == original
    directory = engine.store.root / "jobs" / engine.job_id / "contexts/supplement_sources"
    archives = [json.loads(p.read_text()) for p in directory.glob("*.json")]
    beta = next(c for c in archives if c["integration_item"]["gap_id"] == "beta")
    assert beta["current"]["blocks"][-1]["block_id"] == "si1"
    assert "extra" in beta["existing_evidence_ids"]


async def test_later_selection_failure_is_consumed_after_prior_edit_and_does_not_cancel_next(
    store, inputs
):
    source = source_with("alpha", "beta", "gamma")
    engine = engine_for(store, inputs, IntegrationRuntime())
    prepare = engine.prepare_context
    failed = asyncio.Event()

    async def prepare_one(task, context):
        name = context["integration_item"]["gap_id"]
        if name == "beta":
            failed.set()
            raise ValueError("this item cannot select materials")
        if name == "alpha":
            await failed.wait()
        return await prepare(task, context)

    engine.prepare_context = prepare_one
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert [c["integration_item"]["gap_id"] for c in engine.runtime.integrations] == [
        "alpha",
        "gamma",
    ]
    assert [b.block_id for b in result.blocks[-2:]] == ["added_alpha", "added_gamma"]
    assert any("beta" in item for item in result.pending_items)
    assert "added_alpha" in engine.runtime.integrations[-1]["protected_block_ids"]


async def test_capacity_is_measured_after_prior_edit_even_when_selection_already_finished(
    store, inputs, monkeypatch
):
    source = source_with("alpha", "beta")

    class GrowingRuntime(IntegrationRuntime):
        async def measure(self, task, context, schema, char_limit):
            result = await super().measure(task, context, schema, char_limit)
            if task == "integrate_supplement" and any(
                b["block_id"] == "added_alpha" for b in context["current"]["blocks"]
            ):
                result["fits"] = False
            return result

    engine = engine_for(store, inputs, GrowingRuntime())
    check = AsyncMock(return_value=SupplementCheckResult("pending", [], reason="资料核对未完成"))
    monkeypatch.setattr(
        "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
    )
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert len(engine.runtime.integrations) == 1
    assert result.blocks[-1].block_id == "added_alpha"
    assert any("beta" in item for item in result.pending_items)
    check.assert_awaited_once()


async def test_prefetch_does_not_exceed_or_hide_global_call_budget(store, inputs):
    source = source_with("alpha", "beta", "gamma")
    engine = engine_for(store, inputs, IntegrationRuntime())
    engine.limits.max_calls = 1
    with pytest.raises(ModelCallBudgetExceeded):
        await integrate_supplements(engine, source, source["model_proposals"])
    assert engine.store.get(engine.job_id)["calls"] == 1
    assert not engine.runtime.integrations


async def test_cancel_drains_real_selection_workers_async_cleanup(store, inputs):
    source = source_with("alpha", "beta", "gamma", "delta")
    started, closing, closed = [], [], []
    ready = asyncio.Event()

    class SlowSelection(IntegrationRuntime):
        async def complete(self, task, context, schema, session):
            assert task == "select_context"
            name = context["purpose"]["supplement"]["gap_id"]
            started.append(name)
            if len(started) == 3:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.append(name)
                await asyncio.sleep(0.01)
                closed.append(name)

    engine = engine_for(store, inputs, SlowSelection())
    run = asyncio.create_task(integrate_supplements(engine, source, source["model_proposals"]))
    await asyncio.wait_for(ready.wait(), 1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 1)
    assert sorted(closed) == sorted(closing) == sorted(started)
    assert "delta" not in started
    assert not [
        task for task in asyncio.all_tasks() if task.get_name().startswith("supplement-selection-")
    ]


async def test_completed_prefetch_is_durable_across_cancel_and_resume(store, inputs):
    source = source_with("alpha", "beta", "gamma")
    editing = asyncio.Event()

    class PauseEditor(IntegrationRuntime):
        pause = True

        async def complete(self, task, context, schema, session):
            if task == "integrate_supplement" and self.pause:
                while len(self.selections) != 3:
                    await asyncio.sleep(0)
                editing.set()
                await asyncio.Event().wait()
            return await super().complete(task, context, schema, session)

    runtime = PauseEditor()
    engine = engine_for(store, inputs, runtime)
    run = asyncio.create_task(integrate_supplements(engine, source, source["model_proposals"]))
    await asyncio.wait_for(editing.wait(), 1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 1)
    assert len(runtime.selections) == 3
    runtime.pause = False
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert len(runtime.selections) == 3
    assert [b.block_id for b in result.blocks[-3:]] == ["added_alpha", "added_beta", "added_gamma"]
