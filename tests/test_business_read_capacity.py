"""Business source reads share the next request's capacity without losing history."""

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from folio.research.contracts import ResearchBatch, ResearchConfig, ResearchStep
from folio.research.ledger import ResearchLedger
from folio.research.transport import research_messages, research_prompt_chars
from folio.research.workflow import ResearchTools, _source_result_context, execute_research_batch
from folio.research.workspace import WorkspaceManager


@pytest.fixture
def capacity_tools(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.md").write_text("# Alpha\n\n" + "Alpha source text. " * 450, encoding="utf-8")
    (sources / "b.md").write_text("# Beta\n\n" + "Beta source text. " * 450, encoding="utf-8")
    for name in ("c", "d", "e"):
        (sources / f"{name}.md").write_text(f"# {name}\n\n" + f"{name} original text. " * 450, encoding="utf-8")
    config = ResearchConfig()
    engine = SimpleNamespace(
        request=SimpleNamespace(research=config), job_id="job",
        store=SimpleNamespace(work_event=lambda *args: None, audit_event=lambda *args: None),
    )
    ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    session = ledger.load_session("owner", "round") | {
        "quota_task_id": "owner", "round_id": "round", "phase": "local", "round_steps": 0,
    }
    tools = ResearchTools(
        engine, SimpleNamespace(task_id="owner", title="Research"), SimpleNamespace(evidence=[]),
        WorkspaceManager(sources, tmp_path / "task"),
        SimpleNamespace(verify_execution=lambda key: {}), ledger, session,
    )
    tools.prepare_context_sources({"user_request": "Write an English report using the selected chapters."})
    return tools


def read_batch(*arguments):
    return ResearchBatch(steps=[
        ResearchStep(tool="fs.read", arguments=value, tool_call={
            "id": f"read-{index}", "type": "function",
            "function": {"name": "fs_read", "arguments": json.dumps(value)},
        })
        for index, value in enumerate(arguments)
    ])


async def execute(tools, batch):
    tools.session.update(
        pending_batch=batch.model_dump(exclude_unset=True),
        pending_batch_start=tools.session["step"], pending_batch_cursor=0,
    )
    await execute_research_batch(tools, batch)


async def test_business_dispatch_does_not_apply_result_character_ceiling(capacity_tools):
    tools = capacity_tools
    tools.config.max_result_chars = 1000
    await execute(tools, read_batch({"path": "a.md"}, {"path": "b.md"}))
    results = [item["result"] for item in tools.session["observations"]]
    assert all(result["next_offset"] is None for result in results)
    assert all(sum(len(item["text"]) for item in result["evidence"]) > 7000 for result in results)
    assert all(max(len(item["text"]) for item in result["evidence"]) > 2000 for result in results)


@pytest.mark.parametrize("read_count", [2, 5])
async def test_parallel_reads_share_real_request_capacity_and_preserve_history(capacity_tools, read_count):
    tools = capacity_tools
    old = {
        "tool": "fs.inspect", "request_id": "old-structure",
        "arguments": {"path": "a.md"}, "result": {"text": "Previously supplied structure."},
        "model_text": "Previously supplied structure.",
    }
    tools.remember_observation(deepcopy(old))
    tools.session["notes"] = "Keep the previous analysis."
    original_history = deepcopy(tools.session["observations"])
    measured, ceiling = [], None

    def local_fits(task, context, schema, limit):
        nonlocal ceiling
        assert task == "research_step"
        assert context["provider_instruction"] == "Additional provider instructions."
        assert context["observations"][:1] == original_history
        chars = research_prompt_chars(context, schema, task=task)
        measured.append((deepcopy(context), chars))
        if ceiling is None:
            ceiling = chars - 10000
        return chars + 1000 <= ceiling  # Include the provider's output reservation.

    async def forbidden_remote(*args):
        pytest.fail("Binary searches must use the local capacity estimator, not provider tokenization.")

    tools.engine.runtime = SimpleNamespace(fits_prompt_budget=local_fits)
    tools.engine.request_context = lambda task, context: context | {
        "provider_instruction": "Additional provider instructions.",
    }
    tools.engine.request_schema = lambda task, context, model: model.model_json_schema()
    tools.engine.request_capacity = forbidden_remote
    batch = read_batch(*({"path": f"{name}.md"} for name in "abcde"[:read_count]))
    batch.steps[0].notes = "The selected chapters contain the requirements."
    await execute(tools, batch)

    observations = tools.session["observations"]
    assert observations[:1] == original_history
    assert [item["tool_call"]["id"] for item in observations[1:]] == [f"read-{i}" for i in range(read_count)]
    results = [item["result"] for item in observations[1:]]
    assert all(result["next_offset"] is not None for result in results)
    assert all(result["next_character_offset"] > 0 for result in results)
    assert all(result["pagination_reason"] == "context_capacity" for result in results)
    assert all(sum(len(item["text"]) for item in result["evidence"]) > 500 for result in results)
    assert any(chars + 1000 <= ceiling for _, chars in measured)

    # Native tool results keep the original provider IDs, including both reads.
    context = tools.engine.request_context("research_step", _source_result_context(tools, [], {}))
    assert context["notes"] == batch.steps[0].notes
    assert context["research_history"] == tools.memory.history_text(evidence_label=tools.presentation.evidence_id)
    assert context["research_notes"] == tools.memory.markdown(evidence_label=tools.presentation.evidence_id)
    assert context["read_progress"] == list(tools.session["read_progress"].values())
    assert context["step"] == read_count
    messages = research_messages(context, ResearchBatch.model_json_schema(), task="research_step")
    assert [item["tool_call_id"] for item in messages if item["role"] == "tool"] == [
        f"read-{i}" for i in range(read_count)
    ]
    assert research_prompt_chars(context, ResearchBatch.model_json_schema(), task="research_step") + 1000 <= ceiling

    # The exact unread suffix can be recovered, including within one long paragraph.
    for observation, result in zip(observations[1:], results, strict=True):
        path = observation["arguments"]["path"]
        suffix = tools.workspace.read(
            path, offset=result["next_offset"], character_offset=result["next_character_offset"],
        )
        full = tools.workspace.read(path)
        assert "".join(item["text"] for item in result["evidence"] + suffix["evidence"]) == "".join(
            item["text"] for item in full["evidence"]
        )
        progress = next(item for item in tools.session["read_progress"].values()
                        if item["arguments"]["path"] == path)
        assert progress["next_character_offset"] == result["next_character_offset"]
        assert not progress["pagination_complete"]


async def test_requested_chapter_over_one_hundred_blocks_is_not_paginated(capacity_tools):
    tools = capacity_tools
    path = tools.workspace.source_root / "large.md"
    path.write_text("# Large chapter\n\n" + "\n\n".join(f"Paragraph {i}." for i in range(140)), encoding="utf-8")
    # A new upload creates a new immutable source snapshot.
    tools.workspace = WorkspaceManager(tools.workspace.source_root, tools.workspace.task_root / "new-snapshot")
    from folio.research.business_presentation import BusinessPresentation

    tools.presentation = BusinessPresentation(tools.workspace)
    await execute(tools, read_batch({"path": "large.md"}))
    result = tools.session["observations"][0]["result"]
    assert len(result["evidence"]) == 141
    assert result["next_offset"] is None


async def test_capacity_uses_the_actual_model_window_after_history_archival(capacity_tools, monkeypatch):
    tools = capacity_tools
    archived = {
        "tool": "fs.inspect", "request_id": "archived",
        "arguments": {"path": "a.md"}, "result": {"text": "old " * 30000},
        "model_text": "old " * 30000,
    }
    visible = {
        "tool": "fs.inspect", "request_id": "visible",
        "arguments": {"path": "b.md"}, "result": {"text": "Latest visible structure."},
        "model_text": "Latest visible structure.",
    }
    tools.remember_observation(archived)
    tools.remember_observation(visible)
    full_context = {
        "user_request": "Write the report.",
        "observations": list(tools.session["observations"]),
        "research_history": "Full history remains available in the archived context.",
    }
    window = {
        "user_request": "Write the report.", "observations": [visible],
        "context_navigation": {"research_history": {"read": {"scope": "context", "field": "research_history"}}},
    }
    batch = read_batch({"path": "a.md"}, {"path": "b.md"})

    async def fitted(engine, context, schema):
        assert context == full_context
        return deepcopy(window)

    async def model_call(task, context, schema):
        assert context == window
        return batch

    monkeypatch.setattr("folio.research.context_views.fit_context", fitted)
    monkeypatch.setattr(tools, "model_budget", nullcontext)
    tools.engine.call = model_call
    await tools.model("research_step", full_context, ResearchBatch)
    assert tools.session["pending_model_context"] == window
    assert tools.context_sources["observations"] == [archived, visible]

    async def capacity(task, context, schema):
        assert "research_history" not in context
        assert context["context_navigation"] == window["context_navigation"]
        assert context["observations"][0] == visible
        assert all(item["request_id"] != "archived" for item in context["observations"])
        return {"fits": True}

    tools.engine.request_capacity = capacity
    await execute(tools, batch)
    assert tools.session["observations"][:2] == [archived, visible]
    assert all(item["result"]["next_offset"] is None for item in tools.session["observations"][2:])
    assert "pending_model_context" not in tools.session


async def test_new_notes_may_overflow_existing_window_before_any_new_source_text(capacity_tools):
    tools = capacity_tools
    await execute(tools, read_batch({"path": "a.md"}))
    read = tools.session["observations"][0]
    evidence_ids = list(tools.evidence)
    tools.memory.record_delivery(read["request_id"], evidence_ids, complete=True)
    tools.remember_observation({
        "tool": "fs.inspect", "request_id": "latest-navigation",
        "arguments": {"path": "b.md"}, "result": {"text": "The next chapter is Beta."},
        "model_text": "The next chapter is Beta.",
    })
    existing = _source_result_context(tools, [], {})
    tools.session["pending_model_context"] = deepcopy(existing)
    tools.session["pending_context_observation_count"] = len(tools.session["observations"])
    schema = ResearchBatch.model_json_schema()
    ceiling = research_prompt_chars(existing, schema, task="research_step") + 300
    seen = []

    async def capacity(task, context, model):
        fits = research_prompt_chars(context, model.model_json_schema(), task=task) <= ceiling
        seen.append((deepcopy(context), fits))
        return {"fits": fits}

    tools.engine.request_capacity = capacity
    batch = read_batch({"path": "b.md"})
    batch.steps[0].tool_call["function"]["arguments"] = json.dumps({
        "path": "b.md",
        "research_notes": [{
            "topic": "Alpha requirements", "text": "A verified requirement. " * 75,
            "kind": "source_statement", "evidence_ids": evidence_ids,
        }],
    })
    await execute(tools, batch)
    assert len(tools.session["observations"]) == 3
    assert tools.session["observations"][0] == read
    result = tools.session["observations"][-1]["result"]
    assert result.get("status") not in {"failed", "interrupted"}
    assert result["evidence"]
    # Archival was justified by the new notes before adding any new read result.
    assert any(not fits and len(context["observations"]) == 2
               and "A verified requirement." in context["research_notes"]
               for context, fits in seen)
    assert any(fits and all(item["request_id"] != read["request_id"] for item in context["observations"])
               and "A verified requirement." in context["research_notes"]
               for context, fits in seen)
