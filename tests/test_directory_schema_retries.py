"""Malformed directory output never becomes a sparse-edit baseline."""

import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from folio.document.prepare import prepare_with_model
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Limits
from folio.runtime.base import RuntimeResult, planning_messages
from folio.runtime.llm import LLMRuntime

VALID = {
    "sections": [
        {"node_id": "orders", "title": "订单", "level": 1,
         "instruction": "说明订单规则", "content_mode": "generate"}
    ]
}
INVALID_STRUCTURE = deepcopy(VALID)
INVALID_STRUCTURE["sections"][0]["content_mode"] = "container"
REPAIR = {"edits": [{"op": "replace", "path": "/sections/0/content_mode",
                     "value_json": '"generate"'}]}


class SequenceRuntime:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def complete(self, task, context, schema, session_key):
        # The transport must also serialize invalid previous output safely.
        planning_messages(task, context, schema)
        self.calls.append((task, deepcopy(context)))
        response = self.responses[len(self.calls) - 1]
        return RuntimeResult(response if isinstance(response, str) else json.dumps(response))


@pytest.fixture
def preview_stub(monkeypatch):
    rendered = []

    def preview(output, *, plan, **kwargs):
        rendered.append(plan)
        return {"status": "layout_preview"}

    monkeypatch.setattr("folio.document.prepare.prepare", preview)
    return rendered


@pytest.mark.parametrize("invalid", [
    '{"sections":',
    {"unexpected": True},
    {"sections": None},
    {"sections": ["not a node"]},
    {"sections": [VALID["sections"][0] | {"outline_refs": ["o1"]}]},
    VALID | {"unused_template_slots": []},
])
async def test_invalid_schema_gets_full_correction_before_structural_repairs(
    tmp_path, preview_stub, invalid
):
    runtime = SequenceRuntime([invalid, VALID])
    await prepare_with_model("说明订单业务", [], tmp_path, runtime=runtime)
    assert [task for task, _ in runtime.calls] == ["plan", "plan"]
    assert "rejected_proposal" in runtime.calls[1][1]
    assert "repair_paths" not in runtime.calls[1][1]
    assert len(preview_stub) == 1
    assert preview_stub[0].sections[0].instruction == "说明订单规则"
    assert "outline_refs" not in (tmp_path / "planning-proposal.json").read_text()


@pytest.mark.parametrize("invalid_repair", [
    '{"edits":',
    {"edits": [{"op": "replace", "path": "/sections/0/content_mode", "value_json": '"invalid"'}]},
    {"edits": [{"op": "replace", "path": "/sections/0/title", "value_json": '"改错节点"'}]},
])
async def test_bad_repair_preserves_schema_valid_baseline(tmp_path, preview_stub, invalid_repair):
    runtime = SequenceRuntime([INVALID_STRUCTURE, invalid_repair, REPAIR])
    await prepare_with_model(
        "说明订单业务", [], tmp_path, runtime=runtime, limits=Limits(call_attempts=3)
    )
    assert [task for task, _ in runtime.calls] == ["plan", "repair_plan", "repair_plan"]
    first, retry = runtime.calls[1][1], runtime.calls[2][1]
    assert retry["rejected_proposal"] == first["rejected_proposal"]
    assert retry["repair_paths"] == first["repair_paths"]
    assert retry["rejected_proposal_schema_valid"] is True
    assert "rejected_repair" in retry
    assert preview_stub[0].sections[0].title == "订单"
    assert preview_stub[0].sections[0].content_mode == "generate"


async def test_repeated_invalid_schema_exhausts_budget_without_rendering(tmp_path, preview_stub):
    runtime = SequenceRuntime([{"sections": None}, {"sections": None}])
    with pytest.raises(ValidationError):
        await prepare_with_model("说明订单业务", [], tmp_path, runtime=runtime)
    assert len(runtime.calls) == 2
    assert preview_stub == []
    assert not (tmp_path / "planning-proposal.json").exists()


async def test_typed_transport_failure_keeps_response_and_valid_repair_baseline(tmp_path, preview_stub):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(ModelSettingsInput(
        base_url="http://example.test/v1", model="test", no_auth=True, structured_output="prompt"
    ))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    calls = []
    responses = [INVALID_STRUCTURE, {"replacements": {"/sections/0/content_mode": "invalid"},
                                    "remove_indices": [], "append_sections": []},
                 {"replacements": {"/sections/0/content_mode": "generate"},
                  "remove_indices": [], "append_sections": []}]

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        calls.append(json.loads(payload["messages"][1]["content"]))
        event = {"choices": [{"delta": {"content": json.dumps(responses[len(calls) - 1])},
                               "finish_reason": "stop"}], "usage": {"total_tokens": 10}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        output = tmp_path / "preview"
        await prepare_with_model("说明订单业务", [], output, runtime=runtime,
                                 limits=Limits(call_attempts=3))
    finally:
        await runtime.aclose()
    assert len(calls) == 3
    assert calls[2]["rejected_proposal"] == calls[1]["rejected_proposal"]
    assert calls[2]["repair_paths"] == calls[1]["repair_paths"]
    attempts = json.loads((output / "planning-attempts.json").read_text())
    assert "Invalid typed directory repair response" in attempts[1]["error"]
    assert json.loads(attempts[1]["response"]) == responses[1]
    assert attempts[1]["usage"]["total_tokens"] == 10
    assert attempts[2]["validated"] is True
    assert len(preview_stub) == 1
