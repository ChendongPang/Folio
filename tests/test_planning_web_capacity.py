"""Mixed native batches and paged web delivery retain exact evidence ownership."""

import json

from test_planning_read_capacity import make_state, read_batch
from test_planning_web import Provider, evidence, runtime

from folio.research.planning import PlanningResearchBatch, PlanningResearchStep
from folio.research.planning_web import DirectoryWebResearch
from folio.research.transport import research_messages


def search_step():
    args = {"query": "public reference", "purpose": "Check the outline's public background"}
    return PlanningResearchStep(tool="search.web", arguments=args, tool_call={
        "id": "provider_web_call", "type": "function", "function": {
            "name": "search_web", "arguments": json.dumps(args | {"summary": "Check public references"}),
        },
    })


async def test_web_prefix_and_history_pages_together_establish_actual_delivery(tmp_path):
    state, initial = make_state(tmp_path, {"local.md": "Local project requirement."}, max_context_chars=30000)
    source = evidence(text="Public reference content. " * 2500)
    state.web = DirectoryWebResearch(runtime(), state.output, provider=Provider([source]))
    await state.execute_batch(PlanningResearchBatch(steps=[search_step()]), "web_batch", initial=initial)
    first = state.observations[-1]
    assert first["result"]["web_result_page"] and not first["result"]["complete"]
    state.delivered({"manifest": initial, "observations": [first]}, "saw_prefix")
    assert source.evidence_id not in state.delivered_web_evidence
    parts = [first["result"]["text"]]
    offset = first["result"]["next_offset"]
    index = 0
    while offset is not None:
        args = {"scope": "history", "request_id": "web_batch", "offset": offset, "limit": 12000}
        page = await state.dispatch("fs.read", args)
        index += 1
        state.record_tool({"request_id": f"history_{index}", "tool": "fs.read", "arguments": args}, page)
        state.delivered({"manifest": initial, "observations": [state.observations[-1]]}, f"saw_tail_{index}")
        parts.append(page["text"])
        offset = page["next_offset"]
    assert "".join(parts) == state.web_results["web_batch"]["model_text"]
    assert source.evidence_id in state.delivered_web_evidence
    assert source.evidence_id not in state.evidence
    conclusion = state.parse_conclusion({"web_findings": [{"text": "Public background affects chapter scope", "evidence_ids": ["W1"]}]})
    assert conclusion.web_findings[0].evidence_ids == [source.evidence_id]


async def test_web_timeout_does_not_replace_real_failure_with_format_keyerror(tmp_path):
    state, initial = make_state(tmp_path, {"local.md": "Local project requirement."})
    provider = Provider([evidence()])
    state.web = DirectoryWebResearch(runtime(), state.output, provider=provider)
    state.started -= state.limits.max_seconds + 1
    await state.execute_batch(PlanningResearchBatch(steps=[search_step()]), "expired_web", initial=initial)
    assert provider.calls == []
    entry = state.observations[-1]
    assert entry["tool_call"]["id"] == "provider_web_call"
    assert "error" in entry["result"]
    assert not state.delivered_web_evidence


async def test_returned_web_history_continuation_pages_to_available_capacity(tmp_path):
    state, initial = make_state(tmp_path, {"local.md": "Local project requirement."}, max_context_chars=30000)
    source = evidence(text="Public reference content. " * 2500)
    state.web = DirectoryWebResearch(runtime(), state.output, provider=Provider([source]))
    await state.execute_batch(PlanningResearchBatch(steps=[search_step()]), "web_batch", initial=initial)
    context = state.context(initial, [])
    state.delivered(context, "first_request")
    start = state.observations[-1]["result"]["next_offset"]
    args = {"scope": "history", "request_id": "web_batch", "offset": start}
    batch = PlanningResearchBatch(steps=[PlanningResearchStep(tool="fs.read", arguments=args, tool_call={
        "id": "actual_history_call", "type": "function", "function": {
            "name": "fs_read", "arguments": json.dumps(args | {"summary": "Continue public excerpts"}),
        },
    })])
    await state.execute_batch(batch, "web_history", initial=initial, current_context=context)
    next_context = state.context(initial, [])
    assert state.fits_context(next_context)
    assert state.web_results["web_batch"]["result"]["evidence"][0]["text"] == source.text
    result = state.observations[-1]["result"]
    assert result["offset"] == start and result["text"]
    assert result["next_offset"] > start
    assert not result["complete"]


async def test_mixed_batch_native_ids_and_web_project_evidence_remain_separate(tmp_path):
    state, initial = make_state(tmp_path, {"local.md": "Local project requirement."})
    source = evidence()
    state.web = DirectoryWebResearch(runtime(), state.output, provider=Provider([source]))
    local_step = read_batch({"path": "local.md"}).steps[0]
    batch = PlanningResearchBatch(steps=[search_step(), local_step])
    await state.execute_batch(batch, "mixed_batch", initial=initial)
    context = state.context(initial, [], preserve_history=True)
    messages = research_messages(context, {})
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "provider_web_call", local_step.tool_call["id"],
    ]
    state.delivered(context, "mixed_delivered")
    assert source.evidence_id in state.delivered_web_evidence
    assert source.evidence_id not in state.evidence
    assert {item["source_id"] for item in state.evidence.values()} == {"local.md"}
    state.accept_conclusion({"requirements": [{"requirement_id": "fake_local", "text": "Not an uploaded requirement", "evidence_ids": ["W1"]}]})
    assert any("without an actual reading record" in issue for issue in state.validation())
    assert state.report()["requirements"] == []
