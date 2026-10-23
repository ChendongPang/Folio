"""Provider continuation fields survive tools and restarts without becoming prose."""

import json

import pytest
from native_calls import native_call, research_call
from test_business_research_batch import batch_for, save_pending
from test_business_research_batch import batch_tools as _batch_tools
from test_planning_research import ReadPolicy, Runtime
from test_planning_research import sources as _sources
from test_request_context import engine_for

from folio.research.contracts import ResearchBatch
from folio.research.coverage_obligations import CoverageObligations
from folio.research.native_tools import native_result, tool_definitions
from folio.research.planning import PlanningResearchBatch, research_for_directory
from folio.research.planning_replay import DirectoryResearchReplay
from folio.research.transport import research_messages, research_text
from folio.research.workflow import execute_research_batch
from folio.runtime.base import RuntimeResult
from folio.runtime.image_inputs import text_characters
from folio.runtime.tool_calls import native_retry_context

CONTINUATION = "Provider continuation state, not source evidence."
batch_tools = _batch_tools
sources = _sources


@pytest.mark.parametrize("task,model", [
    ("research_step", ResearchBatch), ("directory_research", PlanningResearchBatch),
])
def test_native_batch_cache_preserves_optional_state_without_exposing_argument(task, model):
    result = research_call("fs.read", {"file": "F1"})
    result.reasoning_content = CONTINUATION
    batch = model.model_validate(native_result(task, result, {}, {}))
    restored = model.model_validate_json(batch.model_dump_json())
    assert restored.reasoning_content == CONTINUATION
    assert restored.steps[0].tool_call == result.tool_calls[0]
    assert "reasoning_content" not in model.model_json_schema()["properties"]
    assert CONTINUATION not in restored.assistant_content
    assert "reasoning_content" not in restored.steps[0].arguments


@pytest.mark.parametrize("reasoning", [None, "", CONTINUATION])
def test_failed_parallel_calls_replay_one_assistant_turn_and_keep_field_out_of_text(reasoning):
    result = RuntimeResult("Read the sources.", tool_calls=[
        native_call("fs_read", {"file": "F1"}, call_id="one").tool_calls[0],
        native_call("fs_read", {"file": "F2"}, call_id="two").tool_calls[0],
    ], reasoning_content=reasoning)
    context = native_retry_context("research_step", {}, result=result, feedback="Invalid arguments")
    messages = research_messages(context, {}, task="research_step")
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "tool", "tool", "user",
    ]
    assert messages[2]["tool_calls"] == result.tool_calls
    assert [message["tool_call_id"] for message in messages[3:5]] == ["one", "two"]
    if reasoning is None:
        assert "reasoning_content" not in messages[2]
    else:
        assert messages[2]["reasoning_content"] == reasoning
    assert CONTINUATION not in research_text(context)
    assert all(CONTINUATION not in (message.get("content") or "") for message in messages)
    stripped = [{key: value for key, value in message.items() if key != "reasoning_content"}
                for message in messages]
    assert text_characters(messages) - text_characters(stripped) == len(reasoning or "")


def test_prose_retry_continuation_is_not_inserted_in_user_context():
    context = native_retry_context("research_step", {}, result=RuntimeResult(
        "Please provide additional details.", reasoning_content=CONTINUATION,
    ))
    messages = research_messages(context, {}, task="research_step")
    assert messages[-2]["reasoning_content"] == CONTINUATION
    assert CONTINUATION not in messages[1]["content"]
    assert CONTINUATION not in research_text(context)


async def test_business_pending_batch_restart_preserves_assistant_state(batch_tools):
    batch = batch_for("a.md", "b.md")
    batch.reasoning_content = CONTINUATION
    save_pending(batch_tools, batch)
    batch_tools.session.update(batch_tools.ledger.load_session("owner", "round"))
    restored = ResearchBatch.model_validate(batch_tools.session["pending_batch"])
    await execute_research_batch(batch_tools, restored)
    observations = batch_tools.ledger.load_session("owner", "round")["observations"]
    messages = research_messages({"observations": observations}, {}, task="research_step")
    assert messages[2]["reasoning_content"] == CONTINUATION
    assert [message["tool_call_id"] for message in messages[3:]] == ["call-0", "call-1"]
    assert all(CONTINUATION not in item["model_text"] for item in observations)


async def test_engine_validated_cache_retains_continuation(store, inputs):
    class ThinkingRuntime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            result = research_call("fs.read", {"file": "F1"})
            result.reasoning_content = CONTINUATION
            return result

    runtime = ThinkingRuntime()
    engine = engine_for(store, inputs, runtime)
    first = await engine._call("research_step", {}, ResearchBatch)
    restored = await engine._call("research_step", {}, ResearchBatch)
    assert runtime.calls == 1
    assert restored == first
    assert restored.reasoning_content == CONTINUATION


async def test_directory_history_and_recorded_response_replay_retain_continuation(sources, tmp_path):
    class ThinkingRuntime(Runtime):
        async def complete(self, *args):
            result = await super().complete(*args)
            result.reasoning_content = CONTINUATION
            return result

    runtime = ThinkingRuntime(ReadPolicy())
    output = tmp_path / "research"
    await research_for_directory(runtime, sources, output, "Draft an outline")
    assert len(runtime.calls) > 1
    for context in runtime.calls[1:]:
        assistants = [message for message in research_messages(context, {})
                      if message["role"] == "assistant"]
        assert assistants
        assert all(message["reasoning_content"] == CONTINUATION for message in assistants)
    replay = DirectoryResearchReplay(output)
    result = replay.take("research_001", runtime.calls[0])
    assert result.reasoning_content == CONTINUATION
    response = json.loads((output / "events/research_001-response.json").read_text())
    assert response["reasoning_content"] == CONTINUATION
    assert CONTINUATION not in (output / "research.md").read_text()


def test_coverage_navigation_retains_state_as_metadata_not_tool_arguments():
    schema = CoverageObligations.model_json_schema()
    result = native_call("inspect_coverage_sources", {
        "catalog_requests": [{"offset": 1}], "reason": "Locate remaining files",
    })
    result.reasoning_content = CONTINUATION
    decision = CoverageObligations.model_validate(native_result(
        "coverage_obligations", result, {}, schema,
    ))
    assert decision.reasoning_content == CONTINUATION
    assert "reasoning_content" not in json.dumps(tool_definitions("coverage_obligations", {}, schema))
