import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator

from folio.document.planner import planning_context
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import PlanItem, PlanProposal
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime
from folio.runtime.schema_compat import strict_output_schema

STRICT_CONFIG = {
    "provider_mode": "airouter",
    "model": "gpt-sol",
    "structured_output": "json_schema",
}


def test_strict_schema_requires_nested_fields_without_changing_nullable_constraints_or_data():
    literal = {"properties": {"untouched": {}}, "default": "literal value"}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "records": {"type": "array", "items": {"$ref": "#/$defs/Record"}, "default": []},
            "choice": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": {
                                "anyOf": [{"type": "string", "enum": ["s1"]}, {"type": "null"}],
                                "default": None,
                            },
                        },
                    },
                    {"type": "null"},
                ]
            },
            "literal": {"const": literal},
        },
        "required": ["records"],
        "$defs": {
            "Record": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string", "maxLength": 12, "default": ""}},
            }
        },
    }
    original = deepcopy(schema)

    wire = strict_output_schema(STRICT_CONFIG, schema)

    assert schema == original
    assert strict_output_schema(STRICT_CONFIG, wire) == wire
    assert wire["required"] == ["records", "choice", "literal"]
    assert wire["$defs"]["Record"]["required"] == ["text"]
    assert wire["$defs"]["Record"]["properties"]["text"] == {
        "type": "string",
        "maxLength": 12,
    }
    choice = wire["properties"]["choice"]["anyOf"][0]
    assert choice["required"] == ["source"]
    assert choice["properties"]["source"]["anyOf"] == [
        {"type": "string", "enum": ["s1"]},
        {"type": "null"},
    ]
    assert wire["properties"]["literal"]["const"] == literal
    valid = {"records": [{"text": "业务正文"}], "choice": {"source": None}, "literal": literal}
    validator = Draft202012Validator(wire)
    validator.validate(valid)
    assert not validator.is_valid(valid | {"choice": {"source": "invented"}})
    assert not validator.is_valid(valid | {"records": [{"text": None}]})
    assert not validator.is_valid(valid | {"choice": {}})


@pytest.mark.parametrize(
    "config",
    [
        STRICT_CONFIG | {"structured_output": "prompt"},
    ],
)
def test_other_provider_schemas_are_unchanged(config):
    schema = PlanProposal.model_json_schema()
    original = deepcopy(schema)
    assert strict_output_schema(config, schema) is schema
    assert schema == original


def runtime_for(tmp_path, **overrides):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://unused.test/v1",
            no_auth=True,
            **(STRICT_CONFIG | overrides),
        )
    )
    return LLMRuntime(settings.path)


@pytest.mark.parametrize("task", ["plan"])
async def test_request_prompt_and_direct_stream_use_identical_strict_schema(tmp_path, inputs, task):
    runtime = runtime_for(tmp_path)
    context = planning_context(inputs["user_request"], [])
    schema = (
        PlanProposal.model_json_schema()
        if task == "plan"
        else {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}, "default": []}
            },
        }
    )
    original = deepcopy(schema)
    captured = []

    async def stream_once(payload, headers):
        captured.append(payload)
        yield "completed", {"text": "{}", "usage": {}}

    runtime._stream_once = stream_once
    try:
        messages, wire, *_ = runtime.request_parts(task, context, schema)
        visible = json.JSONDecoder().raw_decode(
            messages[0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        assert visible == wire
        assert runtime.prompt_chars(task, context, schema) == sum(
            len(m["content"]) for m in messages
        )
        assert "Explicitly return every required field in every object" in messages[0]["content"]
        assert [event async for event in runtime.stream(messages, schema=wire)]
        assert captured[0]["response_format"]["json_schema"]["schema"] == wire
        assert captured[0]["response_format"]["json_schema"]["strict"] is True
        if task == "plan":
            fields = wire["$defs"]["PlanItem"]
            assert set(fields["required"]) == set(fields["properties"])
            assert "notes" in wire["required"]
            assert set(fields["properties"]) == {
                "node_id",
                "title",
                "level",
                "instruction",
                "content_mode",
            }
        # Direct structured-result stream consumers also receive
        # adaptation without needing to go through request_parts.
        assert [event async for event in runtime.stream(messages, schema=schema)]
        assert captured[1]["response_format"]["json_schema"]["schema"] == strict_output_schema(
            STRICT_CONFIG, schema
        )
        assert schema == original
        assert PlanItem(node_id="n1", title="章节", level=1).content_mode == "generate"
    finally:
        await runtime.aclose()


async def test_query_uses_native_parameters_without_response_schema(tmp_path):
    from folio.models import Query
    from folio.runtime.image_inputs import text_characters
    from folio.runtime.tool_calls import tool_characters

    runtime = runtime_for(tmp_path)
    context = {"work_task": {"title": "撤销审批"}, "previous_queries": []}
    schema = Query.model_json_schema()
    original = deepcopy(schema)
    captured = []

    async def stream_once(payload, headers):
        captured.append(payload)
        yield (
            "completed",
            {
                "text": "",
                "usage": {},
                "tool_calls": [
                    {
                        "id": "query_1",
                        "type": "function",
                        "function": {
                            "name": "search_sources",
                            "arguments": '{"queries":["撤销审批"]}',
                        },
                    }
                ],
            },
        )

    runtime._stream_once = stream_once
    try:
        messages, wire, *_ = runtime.request_parts("query", context, schema)
        tools = runtime.request_tools("query", context, wire)
        assert "JSON Schema:" not in messages[0]["content"]
        assert "每个对象的 required 字段均须显式返回" not in messages[0]["content"]
        assert runtime.prompt_chars("query", context, schema) == text_characters(
            messages
        ) + tool_characters(tools)
        function = tools[0]["function"]
        assert function["name"] == "search_sources"
        assert function["strict"] is False
        Draft202012Validator(function["parameters"]).validate({"queries": ["撤销审批"]})
        assert [event async for event in runtime.stream(messages, schema=wire, tools=tools)]
        assert captured[0]["tools"] == tools
        assert captured[0]["tool_choice"] == "required"
        assert captured[0]["parallel_tool_calls"] is False
        assert "response_format" not in captured[0]
        assert schema == original
    finally:
        await runtime.aclose()


async def test_nested_provider_schema_rejection_is_safe_and_not_retried(tmp_path):
    runtime = runtime_for(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Upstream rejected request secret-key",
                    "metadata": {
                        "raw": json.dumps(
                            {
                                "error": {
                                    "code": "invalid_json_schema",
                                    "message": "Invalid schema for response_format: required including every key in properties. "
                                    "Missing template_slot_id; secret-key",
                                },
                            }
                        )
                    },
                }
            },
        )

    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelCallFailed) as error:
            async for _ in runtime.stream(
                [{"role": "user", "content": "test"}], schema={"type": "object"}
            ):
                pass
        assert error.value.code == "schema_unsupported"
        assert error.value.diagnostics["backend_error"] == "invalid_json_schema"
        assert error.value.diagnostics["schema_constraint"] == "required_all_properties"
        assert "secret-key" not in str(error.value) + str(error.value.diagnostics)
        assert "template_slot_id" not in str(error.value) + str(error.value.diagnostics)
        assert len(calls) == 1
    finally:
        await runtime.aclose()
