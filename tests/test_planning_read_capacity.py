"""Directory reads use one real next-request allowance and resumable source ranges."""

import json

import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.research.memory import ResearchNoteUpdate
from folio.research.planning import (
    PlanningResearchBatch,
    PlanningResearchLimits,
    PlanningResearchStep,
    _DirectoryResearch,
    research_for_directory,
)
from folio.research.transport import research_prompt_chars
from folio.research.workspace import WorkspaceManager
from folio.runtime.base import RuntimeResult
from folio.runtime.llm import LLMRuntime


def make_state(tmp_path, texts, **limits):
    sources, output = tmp_path / "sources", tmp_path / "research"
    sources.mkdir()
    output.mkdir()
    for name, text in texts.items():
        (sources / name).write_text(text, encoding="utf-8")
    workspace = WorkspaceManager(sources, output / "workspace")
    state = _DirectoryResearch(
        workspace, output, "核实全部选定章节", None, PlanningResearchLimits(**limits), None)
    initial = workspace.list()
    state.register("fs.list", {}, initial)
    return state, initial


def read_batch(*arguments):
    return PlanningResearchBatch(steps=[
        PlanningResearchStep(tool="fs.read", arguments=args, tool_call={
            "id": f"read_{index}_{args['path']}", "type": "function", "function": {
                "name": "fs_read", "arguments": json.dumps({"summary": "核实资料", **args}, ensure_ascii=False),
            },
        }) for index, args in enumerate(arguments)
    ])


@pytest.mark.asyncio
async def test_full_chapters_ignore_result_cap_and_keep_more_than_100_blocks(tmp_path):
    text = "\n\n".join(f"条款{index}：" + "验收记录必须留存。" * 8 for index in range(140))
    state, initial = make_state(tmp_path, {"requirements.md": text}, max_result_chars=2000)
    await state.execute_batch(read_batch({"path": "requirements.md"}), "research_001", initial=initial)
    result = state.observations[0]["result"]
    assert result["complete"] and result["next_offset"] is None
    assert len(result["evidence"]) > 100
    assert result["characters"] > 2000
    assert "条款139" in state.observations[0]["model_text"]


@pytest.mark.asyncio
async def test_batch_pages_long_blocks_with_shared_context_and_preserves_history(tmp_path):
    state, initial = make_state(tmp_path, {
        "prior.md": "已有资料原文必须保留。",
        "a.md": "甲" * 40000,
        "b.md": "乙" * 40000,
    }, max_context_chars=50000)
    await state.execute_batch(read_batch({"path": "prior.md"}), "research_001", initial=initial)
    prior = state.observations[0]["model_text"]
    await state.execute_batch(
        read_batch({"path": "a.md"}, {"path": "b.md"}), "research_002", initial=initial)
    context = state.context(initial, [], preserve_history=True)
    assert state.fits_context(context)
    assert len(context["observations"]) == 3
    assert context["observations"][0]["model_text"] == prior
    for entry in context["observations"][1:]:
        result = entry["result"]
        assert len(result["evidence"]) == result["total"] == 1
        assert not result["complete"] and result["next_offset"] == 0
        assert 0 < result["next_character_offset"] < 40000
        scope = state.ranges[result["range_id"]]
        assert not scope["complete"] and not scope["pages"]
        assert scope["next_offset"] == 0
        assert scope["next_character_offset"] == result["next_character_offset"]
        for raw in result["evidence"]:
            assert state.workspace.resolve_evidence(raw).text == raw["text"]
    assert set(state.presentation.evidence) == set(state.evidence)


@pytest.mark.asyncio
async def test_partial_block_continuations_accumulate_one_range_without_skipping_tail(tmp_path):
    text = "审批与日志。" * 1000
    state, initial = make_state(tmp_path, {"a.md": text})
    first = await state.dispatch("fs.read", {"path": "a.md", "max_chars": 1301})
    assert not first["range_complete"]
    second = await state.dispatch("fs.read", {
        "path": "a.md", "offset": first["next_offset"],
        "character_offset": first["next_character_offset"],
    })
    assert first["range_id"] == second["range_id"]
    assert second["range_complete"]
    assert len(state.ranges) == 1
    scope = next(iter(state.ranges.values()))
    assert scope["complete"] and scope["next_offset"] is None
    assert "".join(raw["text"] for result in (first, second) for raw in result["evidence"]) == text


@pytest.mark.asyncio
async def test_real_local_capacity_includes_tools_history_and_output_reserve(tmp_path):
    state, initial = make_state(tmp_path, {"a.md": "状态审计必须留痕。" * 4000},
                                max_context_chars=4000)
    store = ModelSettingsStore(tmp_path / "model")
    store.save(ModelSettingsInput(
        base_url="http://unused.invalid/v1", model="test", no_auth=True, max_tokens=1024,
        max_input_tokens=600000, tokenizer_encoding="o200k_base"))
    runtime = LLMRuntime(store.path)
    schema = PlanningResearchBatch.model_json_schema()
    try:
        base = state.context(initial, [], runtime, schema, preserve_history=True)
        measured = runtime.capacity.local_measure("directory_research", base, schema, 4000)
        runtime.capacity.window = measured["estimated_input_tokens"] + 1024 + 512 + 1800
        await state.execute_batch(read_batch({"path": "a.md"}), "research_001",
                                  initial=initial, runtime=runtime, schema=schema)
        context = state.context(initial, [], runtime, schema, preserve_history=True)
        measured = runtime.capacity.local_measure("directory_research", context, schema, 4000)
        assert measured["fits"]
        assert measured["tool_characters"] > 0
        assert measured["characters"] > state.limits.max_context_chars
        assert measured["estimated_input_tokens"] + measured["output_tokens"] + 512 <= runtime.capacity.window
        assert state.observations[0]["result"]["next_character_offset"] > 0
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_production_next_context_fits_with_parallel_results_and_error_feedback(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name, text in {"prior.md": "已确认原文。",
                       "a.md": "甲" * 40000, "b.md": "乙" * 40000}.items():
        (sources / name).write_text(text, encoding="utf-8")

    class Runtime:
        calls = 0

        async def complete(self, task, context, schema, session_key):
            self.calls += 1
            if self.calls == 1:
                calls = read_batch({"path": "prior.md"})
            elif self.calls == 2:
                calls = read_batch({"path": "a.md"}, {"path": "b.md"}, {"path": "missing.md"})
            else:
                assert research_prompt_chars(context, schema) <= 50000
                assert len(context["observations"]) == 4
                assert context["feedback"]
                assert "已确认原文" in context["observations"][0]["model_text"]
                for entry in context["observations"][1:3]:
                    assert entry["result"]["pagination_reason"] == "context_capacity"
                    assert entry["result"]["next_character_offset"] > 0
                return RuntimeResult("", tool_calls=[{
                    "id": "finish", "type": "function", "function": {
                        "name": "finish", "arguments": json.dumps({
                            "summary": "已读部分原文，保留未读范围。",
                            "blocking_gaps": ["资料未读取完整"],
                        }, ensure_ascii=False),
                    },
                }])
            return RuntimeResult("", tool_calls=[step.tool_call for step in calls.steps])

    runtime = Runtime()
    report = await research_for_directory(
        runtime, sources, tmp_path / "output", "整理目录",
        limits=PlanningResearchLimits(max_calls=3, max_context_chars=50000))
    assert runtime.calls == 3
    assert report["status"] == "partial"


@pytest.mark.asyncio
async def test_checkpoint_long_block_slice_can_be_cited_without_delivering_its_tail(tmp_path):
    state, initial = make_state(tmp_path, {"a.md": "状态审计必须留痕。" * 1000})
    await state.execute_batch(read_batch({"path": "a.md"}), "research_001")
    original = state.observations[0]
    original_id = original["result"]["evidence"][0]["evidence_id"]
    base = state.context(initial, [], preserve_history=True) | {"observations": []}
    view = state.fit_latest(original, base, lambda context:
                            len(context["observations"][0]["model_text"]) <= 2500)
    sliced_id = view["result"]["evidence"][0]["evidence_id"]
    assert sliced_id != original_id
    assert view["result"]["next_offset"] == 0 and view["result"]["next_character_offset"] > 0
    state.delivered(base | {"observations": [view]}, "research_002")
    assert sliced_id in state.delivered_evidence
    assert original_id not in state.delivered_evidence
    assert state.memory.apply([ResearchNoteUpdate(
        topic="审计", text="审计必须留痕。", evidence_ids=[sliced_id],
        kind="source_statement",
    )], evidence=state.evidence, call_id="save") > 0
    assert not state.memory.dump()["batches"]["research_001"]["delivered"]
    assert not state.ranges[view["result"]["range_id"]]["delivered_complete"]
    await state.execute_batch(read_batch({
        "path": "a.md", "offset": view["result"]["next_offset"],
        "character_offset": view["result"]["next_character_offset"],
    }), "research_003")
    state.delivered(base | {"observations": [state.observations[-1]]}, "research_004")
    assert original_id not in state.delivered_evidence
    assert state.ranges[view["result"]["range_id"]]["delivered_complete"]
