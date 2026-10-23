import json

import httpx
import pytest

from folio.document.planner import planning_context
from folio.document.planning_transport import planning_payload, planning_text
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import PlanProposal
from folio.runtime.errors import ModelCallFailed, public_model_error
from folio.runtime.llm import LLMRuntime


def context(tmp_path):
    return planning_context(
        "交易处理：不增加原范围之外的要求。",
        [
            {
                "source_id": "source-1",
                "name": "业务资料.md",
                "format": "markdown",
                "headings": ["业务章节"],
                "excerpt": "必须完整保留这段说明。" * 80,
            }
        ],
    )


def test_wire_preserves_request_and_all_source_metadata_without_synthetic_refs(tmp_path):
    original = context(tmp_path)
    before = json.dumps(original)
    wire = planning_payload(original)
    assert wire["requirements"] == original["requirements"]
    assert wire["source_catalog"] == original["source_catalog"]
    assert json.dumps(original) == before
    visible = planning_text(wire)
    assert "必须完整保留这段说明。" * 80 in visible
    assert "outline_refs" not in visible and "template_slot_id" not in visible


def test_rejected_proposal_has_array_coordinates_without_mutating_saved_directory(tmp_path):
    original = context(tmp_path)
    proposal = {
        "sections": [
            {
                "node_id": "n47",
                "title": "交易处理",
                "level": 1,
                "instruction": "说明处理",
                "content_mode": "generate",
            }
        ]
    }
    original.update(previous_proposal=proposal, rejected_proposal=proposal)
    before = json.dumps(original)
    wire = planning_payload(original)
    assert json.dumps(original) == before
    assert wire["rejected_proposal"]["sections"][0]["edit_path"] == "/sections/0"
    assert "edit_path" not in wire["previous_proposal"]["sections"][0]


def test_schema_requests_only_directory_fields(tmp_path):
    from folio.document.planning_transport import planning_schema

    schema = planning_schema(PlanProposal.model_json_schema(), context(tmp_path))
    assert set(schema["$defs"]["PlanItem"]["properties"]) == {
        "node_id",
        "title",
        "level",
        "instruction",
        "content_mode",
    }
    assert set(schema["properties"]) == {"document_title", "sections", "notes"}
    assert "document_title" in schema["required"]


@pytest.mark.parametrize("structured", ["json_schema", "prompt"])
async def test_request_measurement_and_schema_fallback(tmp_path, structured):
    original = context(tmp_path)
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            structured_output=structured,
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    schema = PlanProposal.model_json_schema()
    from folio.runtime.base import prompt_chars

    measured_messages = []
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test", "max_model_len": 922000}]})
        payload = json.loads(request.content)
        if request.url.path == "/tokenize":
            measured_messages.extend(payload["messages"])
            return httpx.Response(200, json={"count": 500})
        assert payload["messages"] == measured_messages
        assert "不增加原范围之外的要求" in payload["messages"][1]["content"]
        assert prompt_chars(runtime, "plan", original, schema) == sum(
            len(m["content"]) for m in payload["messages"]
        )
        system = payload["messages"][0]["content"]
        assert "work_task/business_record" not in system
        visible_schema = json.JSONDecoder().raw_decode(system.split("JSON Schema:\n", 1)[1])[0]
        if structured == "json_schema":
            assert visible_schema == payload["response_format"]["json_schema"]["schema"]
            assert payload["response_format"]["json_schema"]["strict"] is True
        else:
            assert "response_format" not in payload
        assert visible_schema["$defs"]["PlanItem"]
        event = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        measured = await runtime.measure("plan", original, schema, 192000)
        assert measured["measurement"] == "server_tokenizer"
        assert measured["characters"] == sum(len(m["content"]) for m in measured_messages)
        await runtime.complete("plan", original, schema, "test")
        assert requests == ["/v1/models", "/tokenize", "/v1/chat/completions"]
    finally:
        await runtime.aclose()


async def test_provider_context_error_is_specific_and_does_not_leak_response(tmp_path):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={
                    "error": {
                        "message": "This model's maximum context length is 32768 tokens. private detail"
                    }
                },
            )
        )
    )
    try:
        with pytest.raises(ModelCallFailed) as error:
            await runtime.complete("plan", {}, PlanProposal.model_json_schema(), "test")
        public = public_model_error(error.value)
        assert public["code"] == "context_limit" and "上下文上限" in public["text"]
        assert "private detail" not in public["text"]
    finally:
        await runtime.aclose()


async def test_planning_output_budget_does_not_change_other_calls(tmp_path):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            max_tokens=8192,
            planning_max_tokens=16384,
        )
    )
    assert settings.public()["planning_max_tokens"] == 16384
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    budgets = []

    def handler(request):
        budgets.append(json.loads(request.content)["max_tokens"])
        event = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        for task in ["plan", "repair_plan", "generate"]:
            await runtime.complete(task, {}, {}, "test")
        assert budgets == [16384, 16384, 8192]
    finally:
        await runtime.aclose()


def test_retry_bounds_explanations_without_limiting_directory_or_input(tmp_path):
    from folio.document.planning_transport import planning_schema

    original = context(tmp_path)
    wire = planning_payload(original)
    schema = PlanProposal.model_json_schema()
    normal = planning_schema(schema, wire)
    retry = planning_schema(schema, wire | {"concise_plan_retry": True})
    assert (
        retry["$defs"]["PlanItem"]["properties"]["instruction"]["maxLength"]
        < normal["$defs"]["PlanItem"]["properties"]["instruction"]["maxLength"]
    )
    assert "maxItems" not in retry["properties"]["sections"]
    assert schema["$defs"]["PlanItem"]["properties"]["instruction"]["maxLength"] == 4000


@pytest.mark.parametrize(
    "tokens,window,expected",
    [(5000, 32768, 24576), (15000, 32768, 17256), (5000, 20000, 16384), (None, None, 16384)],
)
async def test_retry_budget_is_measured_local_and_bounded(tmp_path, tokens, window, expected):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            max_tokens=8192,
            planning_max_tokens=16384,
        )
    )
    runtime = LLMRuntime(settings.path)

    async def measure(*args):
        return {"input_tokens": tokens, "context_window": window, "margin_tokens": 512}

    runtime.measure = measure
    try:
        result = await runtime.planning_retry_budget("plan", {}, {}, 192000)
        assert result["retry_output_tokens"] == expected
        assert runtime.output_tokens("plan") == expected
        assert runtime.output_tokens("generate") == 8192
        assert settings.public()["planning_max_tokens"] == 16384
    finally:
        await runtime.aclose()
