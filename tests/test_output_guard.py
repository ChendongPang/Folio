import json
from copy import deepcopy

import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import JobInput, SectionContent
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed, ModelOutputRepetition
from folio.runtime.llm import LLMRuntime
from folio.runtime.output_guard import RepeatedParagraphGuard, validate_paragraph_cycles

TEXT = "这是一段完整业务规则，明确输入、处理过程、输出以及异常处理。" * 5


def alternating_blocks(cycles=3):
    # Distilled run-02 shape: the two paragraphs had 32 and 44 characters,
    # repeated 48 times with fresh IDs. Neither reaches the old 80-char guard.
    texts = ["甲" * 32, "乙" * 44]
    return [
        {
            "block_id": f"b{i}",
            "kind": "paragraph",
            "text": texts[i % 2],
            "origin": "model_proposal",
            "evidence_ids": [],
            "heading_level": 4,
        }
        for i in range(cycles * 2)
    ]


@pytest.mark.parametrize("chunk_size", [1, 17, 10000])
def test_repeated_paragraphs_with_different_ids_are_detected_across_stream_chunks(chunk_size):
    value = json.dumps(
        {"blocks": [{"block_id": f"b{i}", "text": TEXT} for i in range(4)]}, ensure_ascii=False
    )
    guard = RepeatedParagraphGuard()
    with pytest.raises(ModelOutputRepetition):
        for start in range(0, len(value), chunk_size):
            guard.feed(value[start : start + chunk_size])
    assert guard.consecutive == 3


@pytest.mark.parametrize(
    "blocks",
    [
        [{"block_id": f"b{i}", "text": TEXT + str(i)} for i in range(3)],
        [{"block_id": f"b{i}", "kind": "table", "rows": [[TEXT]]} for i in range(3)],
        [{"block_id": f"b{i}", "kind": "heading", "text": TEXT} for i in range(3)],
        [{"block_id": f"b{i}", "text": "简短提示"} for i in range(3)],
        [
            {"block_id": "a", "text": TEXT},
            {"block_id": "b", "text": TEXT},
            {"block_id": "c", "text": "其他业务规则"},
        ],
    ],
)
def test_guard_does_not_mistake_tables_headings_or_distinct_content_for_writer_loop(blocks):
    guard = RepeatedParagraphGuard()
    guard.feed(
        json.dumps({"pending_items": ['字面量 "blocks":[]'], "blocks": blocks}, ensure_ascii=False)
    )
    assert guard.finished


@pytest.mark.parametrize("chunk_size", [1, 17, 4096])
def test_short_ab_cycle_from_100_block_result_is_rejected_without_editing(chunk_size):
    value = {
        "blocks": [
            {"block_id": "table", "kind": "table", "rows": [["项目", "状态"]]},
            *[{"block_id": f"intro{i}", "text": str(i) * 39} for i in range(3)],
            *alternating_blocks(48),
        ]
    }
    original = deepcopy(value)
    guard = RepeatedParagraphGuard()
    encoded = json.dumps(value, ensure_ascii=False)
    with pytest.raises(ModelOutputRepetition):
        for start in range(0, len(encoded), chunk_size):
            guard.feed(encoded[start : start + chunk_size])
    with pytest.raises(ModelOutputRepetition):
        validate_paragraph_cycles(value)
    assert value == original


@pytest.mark.parametrize("period", range(2, 9))
def test_three_exact_cycles_detected_for_each_supported_period(period):
    blocks = [
        {"block_id": str(i), "text": f"业务条件{i % period}：" + "条件内容" * 6}
        for i in range(period * 3)
    ]
    validate_paragraph_cycles({"blocks": blocks[:-1]})
    with pytest.raises(ModelOutputRepetition):
        validate_paragraph_cycles({"blocks": blocks})


@pytest.mark.parametrize(
    "change", ["text", "origin", "evidence_ids", "heading_level", "scope", "section_id"]
)
def test_signature_preserves_every_business_field_except_block_id(change):
    blocks = alternating_blocks()
    if change == "text":
        blocks[2]["text"] += " "  # Even whitespace is not silently normalized.
    elif change == "origin":
        blocks[2]["origin"] = "evidence"
    elif change == "evidence_ids":
        blocks[2]["evidence_ids"] = ["another-version"]
    elif change == "heading_level":
        blocks[2]["heading_level"] = 5
    else:
        blocks[2][change] = "another-scope"
    value = {"blocks": blocks}
    before = deepcopy(value)
    validate_paragraph_cycles(value)
    guard = RepeatedParagraphGuard()
    guard.feed(json.dumps(value))
    assert guard.finished and value == before


@pytest.mark.parametrize("separator", ["heading", "table", "empty"])
def test_cycle_detection_does_not_cross_nonparagraph_boundaries(separator):
    middle = (
        {"block_id": "scope", "kind": separator, "text": "另一种业务情况"}
        if separator != "empty"
        else {"block_id": "blank", "text": ""}
    )
    value = {"blocks": alternating_blocks(2) + [middle] + alternating_blocks(2)}
    validate_paragraph_cycles(value)
    guard = RepeatedParagraphGuard()
    guard.feed(json.dumps(value))
    assert guard.finished


def test_scope_boundaries_reset_history_even_if_two_scopes_repeat_alternately():
    value = {"blocks": alternating_blocks(12)}
    for i, block in enumerate(value["blocks"]):
        block["scope"] = f"scope{i % 2}"
    validate_paragraph_cycles(value)


@pytest.mark.parametrize("texts", [["甲标签", "乙标签"], ["字" * 30], ["甲" * 15, "乙" * 15]])
def test_short_labels_cannot_reach_threshold_by_multiplying_the_primitive_period(texts):
    value = {
        "blocks": [
            {"block_id": str(i), "text": texts[i % len(texts)]} for i in range(len(texts) * 24)
        ]
    }
    validate_paragraph_cycles(value)
    guard = RepeatedParagraphGuard()
    guard.feed(json.dumps(value))
    assert guard.finished


def test_long_single_paragraph_repetition_keeps_changed_citations():
    value = {
        "blocks": [
            {"block_id": str(i), "text": TEXT, "evidence_ids": [f"version{i}"]} for i in range(9)
        ]
    }
    validate_paragraph_cycles(value)
    guard = RepeatedParagraphGuard()
    guard.feed(json.dumps(value))
    assert guard.finished


async def test_repetition_closes_stream_early_and_never_adopts_partial_json(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # The synthetic stream does not provide tokenizer endpoints.
    seen, closed = [], []

    async def stream(*args, **kwargs):
        try:
            yield "text.delta", {"text": '{"blocks":['}
            for i in range(20):
                seen.append(i)
                yield (
                    "text.delta",
                    {
                        "text": ("," if i else "")
                        + json.dumps({"block_id": "b" + str(i), "text": TEXT})
                    },
                )
            pytest.fail("must interrupt before spending the remaining output budget")
        finally:
            closed.append(True)

    runtime.stream = stream
    try:
        with pytest.raises(ModelOutputRepetition):
            await runtime.complete("generate", {}, SectionContent.model_json_schema(), "test")
        assert seen == [0, 1, 2] and closed == [True]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("completed_only", [False, True])
async def test_runtime_rejects_short_cycles_from_stream_or_complete_result(
    tmp_path, completed_only
):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Retain local capacity checks without network probes.
    seen, closed = [], []
    value = {"blocks": alternating_blocks(48)}
    before = deepcopy(value)

    async def stream(*args, **kwargs):
        try:
            if completed_only:
                yield "completed", {"text": json.dumps(value), "usage": {}}
            else:
                yield "text.delta", {"text": '{"blocks":['}
                for i, block in enumerate(value["blocks"]):
                    seen.append(i)
                    encoded = ("," if i else "") + json.dumps(block)
                    for start in range(0, len(encoded), 11):
                        yield "text.delta", {"text": encoded[start : start + 11]}
                pytest.fail("the stream must stop after three complete AB cycles")
        finally:
            closed.append(True)

    runtime.stream = stream
    try:
        with pytest.raises(ModelOutputRepetition):
            await runtime.complete("generate", {}, SectionContent.model_json_schema(), "test")
        assert seen == ([] if completed_only else list(range(6)))
        assert closed == [True]
        assert value == before
    finally:
        await runtime.aclose()


def test_engine_complete_result_validation_rejects_cycle_without_rewriting():
    engine = object.__new__(Engine)
    value = SectionContent.model_validate({"blocks": alternating_blocks()})
    before = value.model_dump()
    for task in ("generate", "repair"):
        with pytest.raises(ModelOutputRepetition):
            engine.check_result(value, {"evidence": [], "current": before}, task=task)
        assert value.model_dump() == before


async def test_cached_cycle_is_not_accepted_or_rewritten_or_counted_as_a_new_call(store, inputs):
    class Runtime(DemoRuntime):
        calls = 0

        async def complete(self, task, context, schema, session):
            self.calls += 1
            return RuntimeResult(json.dumps({"blocks": alternating_blocks(1)}))

    runtime = Runtime()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    await engine._call("generate", {"evidence": []}, SectionContent)
    with store.connect() as conn:
        key = conn.execute(
            "SELECT task_key FROM tasks WHERE job_id=?", (engine.job_id,)
        ).fetchone()[0]
    bad_cache = json.dumps({"blocks": alternating_blocks(48)})
    store.finish_task(engine.job_id, key, result=bad_cache)
    with pytest.raises(ModelOutputRepetition):
        await engine._call("generate", {"evidence": []}, SectionContent)
    assert runtime.calls == 1
    assert store.task(engine.job_id, key)["result"] == bad_cache
    assert store.task(engine.job_id, key)["attempts"] == 1


async def test_complete_cycle_rejection_retries_whole_result_with_existing_budget(store, inputs):
    class Runtime(DemoRuntime):
        calls = 0
        feedback = []

        async def complete(self, task, context, schema, session):
            self.calls += 1
            self.feedback.append(context.get("format_feedback", ""))
            return RuntimeResult(
                json.dumps({"blocks": alternating_blocks(48 if self.calls == 1 else 1)})
            )

    runtime = Runtime()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    result = await engine._call("generate", {"evidence": []}, SectionContent)
    assert runtime.calls == 2
    assert "cycled through the same paragraph group three times" in runtime.feedback[1]
    assert [block.text for block in result.blocks] == ["甲" * 32, "乙" * 44]
    with store.connect() as conn:
        row = conn.execute(
            "SELECT attempts,result FROM tasks WHERE job_id=?", (engine.job_id,)
        ).fetchone()
    assert row[0] == 2 and len(json.loads(row[1])["blocks"]) == 2


@pytest.mark.parametrize(
    "code, message", [("repetition", "same long paragraph three times"), ("length_limit", "exhausted its output allowance")]
)
async def test_writer_retry_receives_specific_feedback_and_preserves_bounded_attempts(
    store, inputs, code, message
):
    class Corrected(DemoRuntime):
        seen = set()
        feedback = []

        async def complete(self, task, context, schema, session):
            if task == "generate":
                sid = context["section"]["section_id"]
                if sid not in self.seen:
                    self.seen.add(sid)
                    raise ModelCallFailed("safe test diagnostic", code)
                self.feedback.append(context.get("format_feedback", ""))
            return await super().complete(task, context, schema, session)

    runtime = Corrected()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, runtime).run(job_id)
    assert result["status"] == "draft"
    assert runtime.feedback and all(message in f for f in runtime.feedback)
    with store.connect() as c:
        attempts = [
            r[0]
            for r in c.execute(
                "SELECT attempts FROM tasks WHERE job_id=? AND task_key LIKE 'generate:%'",
                (job_id,),
            )
        ]
    assert attempts and all(n == 2 for n in attempts)
