import json

import httpx
import pytest

from folio.decisions import IntentDecision
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.base import RuntimeResult
from folio.runtime.llm import LLMRuntime


def settings_for(tmp_path, *, mode="json_schema", max_tokens=8192):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://model.test/v1",
            model="glm",
            no_auth=True,
            max_tokens=max_tokens,
            planning_max_tokens=16384,
            structured_output=mode,
        )
    )
    return settings


def completion(value):
    chunk = {"choices": [{"delta": {"content": json.dumps(value)}, "finish_reason": "stop"}]}
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize("mode", ["prompt", "json_schema"])
@pytest.mark.parametrize("configured_tokens,expected_tokens", [(8192, 8192), (512, 512)])
async def test_intent_has_visible_contract_and_matches_measured_request(
    tmp_path, mode, configured_tokens, expected_tokens
):
    runtime = LLMRuntime(settings_for(tmp_path, mode=mode, max_tokens=configured_tokens).path)
    await runtime.client.aclose()
    counted, sent = [], []
    schema = IntentDecision.model_json_schema()
    context = {
        "latest_user_message": "按大纲写操作手册，先套模板，不生成正文。",
        "messages": [],
        "workflow": {"files": [{"role": "outline"}]},
    }

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "glm", "max_model_len": 16384}]})
        payload = json.loads(request.content)
        if request.url.path == "/tokenize":
            counted.append(payload["messages"])
            return httpx.Response(200, json={"count": 900})
        sent.append(payload)
        system = payload["messages"][0]["content"]
        visible_schema = json.JSONDecoder().raw_decode(system.split("JSON Schema:\n", 1)[1])[0]
        assert visible_schema == schema
        assert schema["required"] == ["intent", "summary", "style_source_ids"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]["intent"]["enum"]) == {
            "generate",
            "resume_generation",
            "requirements",
            "analyze",
            "analyze_style",
            "clarify",
            "chat",
        }
        assert '{"intent":"generate","summary":' in system
        for irrelevant in ("writing_profile", "proposal_ref", "evidence_ids", "section.level"):
            assert irrelevant not in system
        assert json.loads(payload["messages"][1]["content"]) == context
        assert payload["max_tokens"] == expected_tokens
        if mode == "json_schema":
            assert payload["response_format"]["json_schema"]["schema"] == visible_schema
            assert payload["response_format"]["json_schema"]["strict"] is True
        else:
            assert "response_format" not in payload
        return completion({"intent": "generate", "summary": "用户请求开始文档准备。"})

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        measured = await runtime.measure("intent", context, schema, 48000)
        result = await runtime.complete("intent", context, schema, "intent-test")
        assert IntentDecision.model_validate_json(result.content).intent == "generate"
        assert counted == [sent[0]["messages"]]
        assert measured["characters"] == runtime.prompt_chars("intent", context, schema)
        assert measured["output_tokens"] == sent[0]["max_tokens"]
        assert runtime.config["max_tokens"] == configured_tokens
        assert runtime.output_tokens("plan") == 16384
    finally:
        await runtime.aclose()


async def test_intent_format_retry_bypasses_decision_and_keeps_original_request(tmp_path):
    runtime = LLMRuntime(settings_for(tmp_path).path)
    await runtime.client.aclose()
    decisions, sent = [], []
    original = {"latest_user_message": "请检查大纲中的矛盾，暂不编制文档。", "workflow": {}}
    feedback = original | {
        "intent_format_retry": True,
        "format_feedback": "缺少 intent、summary；action 不属于允许字段。",
        "previous_invalid_intent": {"action": "generate"},
    }

    async def decision(task, context, schema):
        decisions.append((task, context))
        return RuntimeResult('{"action":"generate"}'), None

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert json.loads(payload["messages"][1]["content"]) == feedback
        system = payload["messages"][0]["content"]
        assert "Continue classifying the original latest_user_message" in system
        assert "do not treat format feedback as new user intent" in system
        return completion({"intent": "analyze", "summary": "用户仅要求检查材料。"})

    runtime.decision_router.try_complete = decision
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        schema = IntentDecision.model_json_schema()
        first = await runtime.complete("intent", original, schema, "first")
        assert json.loads(first.content) == {"action": "generate"}
        result = await runtime.complete("intent", feedback, schema, "retry")
        assert IntentDecision.model_validate_json(result.content).intent == "analyze"
        assert result.usage["decision_fallback"] == "intent_format_retry"
        assert decisions == [("intent", original)]
        assert len(sent) == 1
        # A per-request retry must not disable optional routing on later intents.
        await runtime.complete("intent", original, schema, "later")
        assert decisions == [("intent", original), ("intent", original)]
        assert len(sent) == 1
        assert "intent_format_retry" not in original
    finally:
        await runtime.aclose()


async def test_capacity_reselection_without_decision_uses_bounded_llm(tmp_path):
    from folio.orchestration.context import ContextSelection

    runtime = LLMRuntime(settings_for(tmp_path).path)
    await runtime.client.aclose()
    sent = []

    async def unexpected_decision(*_):
        raise AssertionError("A disabled decision service must not receive material requests")

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert "selection_capacity_retry" in payload["messages"][1]["content"]
        return completion({"selected_refs": ["e1"]})

    runtime.decision_router.try_complete = unexpected_decision
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete(
            "select_context",
            {
                "selection_capacity_retry": True,
                "purpose": {"section": {"title": "业务背景"}},
                "catalog": [{"context_ref": "e1", "kind": "evidence", "value": "完整原始规则"}],
            },
            ContextSelection.model_json_schema(),
            "capacity-refinement",
        )
        assert json.loads(result.content)["selected_refs"] == ["e1"]
        assert result.usage["selection_provider"] == "llm"
        assert result.usage["selection_batches"] == 1
        assert "decision_fallback" not in result.usage
        assert len(sent) == 1
    finally:
        await runtime.aclose()
