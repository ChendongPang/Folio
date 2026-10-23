"""Bound writing at 32 main calls, later stages at 16, independent Jev at 16 and other strict services at 4."""

import asyncio
import json

import pytest
from native_calls import action_call
from test_request_context import engine_for

from folio.decisions import ActionDecision, IntentDecision
from folio.decisions.selection import MaterialSelector
from folio.model_settings import probe_fingerprint
from folio.models import Review
from folio.orchestration.context import ContextSelection
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


class BlockingRuntime(DemoRuntime):
    def __init__(self, decision="enabled"):
        self.config = {
            "provider_mode": "custom",
            "decision_model": "jev",
            "decision_enabled": decision != "disabled",
            "decision_base_url": "https://judge.test/v1", "decision_api_key": "test-only",
        }
        if decision not in {"unverified", "absent"}:
            self.config["verified_capabilities"] = {
                "decision": probe_fingerprint(self.config, "decision")
            }
        if decision == "stale":
            self.config["decision_api_key"] = "changed-after-test"
        if decision != "absent":
            self.material_selector = MaterialSelector(self)
        self.entered = asyncio.Queue()
        self.main_release, self.selection_release = asyncio.Event(), asyncio.Event()
        self.active = self.main_active = self.peak = self.main_peak = 0
        self.selection_active = self.selection_peak = 0

    async def complete(self, task, context, schema, session):
        strict = task == "select_context" and getattr(
            getattr(self, "material_selector", None), "use_decision", False
        )
        self.active += 1
        self.main_active += not strict
        self.selection_active += strict
        self.peak = max(self.peak, self.active)
        self.main_peak = max(self.main_peak, self.main_active)
        self.selection_peak = max(self.selection_peak, self.selection_active)
        self.entered.put_nowait(task)
        try:
            await (self.selection_release if strict else self.main_release).wait()
            value = (
                {"selected_refs": [], "risk_refs": [], "risk_summary": ""}
                if task == "select_context"
                else {"intent": "chat", "summary": "仅讨论"}
                if task == "intent"
                else {"action": "write", "summary": "资料已齐全"}
                if task == "next_action"
                else {"findings": []}
            )
            if task == "next_action":
                return action_call(value["action"], summary=value["summary"])
            return RuntimeResult(json.dumps(value))
        finally:
            self.active -= 1
            self.main_active -= not strict
            self.selection_active -= strict

    async def started(self, count):
        return [await asyncio.wait_for(self.entered.get(), 3) for _ in range(count)]


def call(engine, task, index):
    context = {"case": index}
    if task == "select_context":
        context.update(purpose="本条资料是否相关", catalog=[])
        model = ContextSelection
    elif task == "intent":
        context["latest_user_message"] = "讨论一下"
        model = IntentDecision
    elif task == "next_action":
        context.update(available_actions=["write"], evidence=[])
        model = ActionDecision
    else:
        context["sections"] = []
        model = Review
    return asyncio.create_task(engine._call(task, context, model))


async def cancel(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_writing_admits_32_main_calls_and_16_independent_jev_calls(store, inputs):
    runtime = BlockingRuntime()
    engine = engine_for(store, inputs, runtime)
    engine._writing_active = True
    tasks = [call(engine, "next_action", i) for i in range(36)]
    try:
        assert await runtime.started(32) == ["next_action"] * 32
        tasks.extend(call(engine, "select_context", i) for i in range(20))
        assert await runtime.started(16) == ["select_context"] * 16
        assert runtime.main_active == 32 and runtime.selection_active == 16
        assert runtime.entered.empty()
        await cancel(tasks)
        assert runtime.active == 0
        assert engine._generation_model_slots._value == 32
        assert engine._provider_model_slots._value == 32
        assert engine._decision_model_slots._value == 16
    finally:
        await cancel(tasks)


async def test_stage_transition_does_not_exceed_shared_main_provider_limit(store, inputs):
    runtime = BlockingRuntime()
    engine = engine_for(store, inputs, runtime)
    engine._writing_active = True
    tasks = [call(engine, "next_action", i) for i in range(32)]
    try:
        await runtime.started(32)
        engine._writing_active = False
        tasks.extend(call(engine, "review", i) for i in range(20))
        await asyncio.sleep(0.02)
        assert runtime.entered.empty() and runtime.main_active == 32
        runtime.main_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert runtime.main_peak <= 32
        assert engine._model_slots._value == 16
        assert engine._provider_model_slots._value == 32
    finally:
        await cancel(tasks)


async def test_strict_selection_starts_while_sixteen_main_calls_are_blocked(store, inputs):
    runtime = BlockingRuntime()
    engine = engine_for(store, inputs, runtime)
    tasks = [call(engine, "review", i) for i in range(20)]
    try:
        assert await runtime.started(16) == ["review"] * 16
        tasks.extend(call(engine, "select_context", i) for i in range(20))
        assert await runtime.started(16) == ["select_context"] * 16
        assert runtime.active == 32 and runtime.main_active == 16
        assert runtime.selection_active == 16
        assert runtime.entered.empty()
        runtime.selection_release.set()
        runtime.main_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert runtime.peak == 32 and runtime.main_peak == 16 and runtime.selection_peak == 16
        assert runtime.active == runtime.main_active == runtime.selection_active == 0
    finally:
        await cancel(tasks)


@pytest.mark.parametrize("decision", ["absent", "disabled", "unverified", "stale"])
async def test_without_verified_strict_selector_all_sixteen_slots_remain_available(
    store, inputs, decision
):
    runtime = BlockingRuntime(decision)
    engine = engine_for(store, inputs, runtime)
    tasks = [call(engine, "review", i) for i in range(10)]
    tasks.extend(call(engine, "select_context", i) for i in range(10))
    try:
        assert await runtime.started(16) == ["review"] * 10 + ["select_context"] * 6
        assert runtime.active == runtime.main_active == 16
        runtime.main_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert runtime.peak == runtime.main_peak == 16
    finally:
        await cancel(tasks)


async def test_fallback_intent_and_native_actions_wait_for_main_capacity(store, inputs):
    runtime = BlockingRuntime()
    engine = engine_for(store, inputs, runtime)
    tasks = [call(engine, "review", i) for i in range(16)]
    try:
        await runtime.started(16)
        tasks.extend(call(engine, task, 99) for task in ("intent", "next_action"))
        tasks.append(call(engine, "select_context", 99))
        assert await runtime.started(1) == ["select_context"]
        assert runtime.active == 17 and runtime.main_active == 16
        runtime.selection_release.set()
        runtime.main_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert runtime.main_peak <= 16
    finally:
        await cancel(tasks)


@pytest.mark.parametrize("model", ["jev", "luna-decisions"])
async def test_all_models_use_same_decision_slots(store, inputs, model):
    runtime = BlockingRuntime()
    runtime.config.update(
        provider_mode="custom",
        decision_model=model,
        base_url="https://example.invalid/v1",
        api_key="test-only",
    )
    runtime.config["verified_capabilities"]["decision"] = probe_fingerprint(
        runtime.config, "decision"
    )
    engine = engine_for(store, inputs, runtime)
    tasks = [call(engine, "select_context", i) for i in range(8)]
    try:
        assert await runtime.started(8) == ["select_context"] * 8
        assert runtime.selection_active == 8 and runtime.entered.empty()
        await cancel(tasks)
        assert engine._decision_model_slots._value == 16
    finally:
        await cancel(tasks)


@pytest.mark.parametrize("first", ["review", "select_context"])
async def test_cancellation_releases_running_and_queued_calls_on_both_services(
    store, inputs, first
):
    runtime = BlockingRuntime()
    engine = engine_for(store, inputs, runtime)
    tasks = [call(engine, first, i) for i in range(20)]
    try:
        await runtime.started(16)
        other = "select_context" if first == "review" else "review"
        tasks.extend(call(engine, other, i) for i in range(20))
        await runtime.started(16)
        assert runtime.active == 32
        assert engine._model_slots.locked() and engine._decision_model_slots.locked()
        await cancel(tasks)
        assert runtime.active == runtime.main_active == runtime.selection_active == 0
        assert engine._model_slots._value == 16
        assert engine._decision_model_slots._value == 16
        runtime.selection_release.set()
        runtime.main_release.set()
        await asyncio.wait_for(
            asyncio.gather(*(call(engine, "review", 100 + i) for i in range(16))), 3
        )
    finally:
        await cancel(tasks)
