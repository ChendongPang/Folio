"""Input policy counts the complete request independently of character limits."""

import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.research.planning import PlanningResearchStep
from folio.runtime.llm import LLMRuntime


async def make_runtime(tmp_path, handler, **policy):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(
        base_url="http://route.test/v1", model="route-alias", no_auth=True,
        max_input_tokens=600000, context_window=922000, tokenizer_encoding="o200k_base",
        **policy,
    ))
    runtime = LLMRuntime(store.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return runtime


@pytest.mark.parametrize("legacy_nulls", [False, True])
async def test_default_budget_keeps_large_chinese_sources_without_model_capacity_settings(tmp_path, legacy_nulls):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(base_url="http://route.test/v1", model="kimi-k3", no_auth=True))
    if legacy_nulls:
        saved = json.loads(store.path.read_text())
        saved.update(dict.fromkeys(("max_input_tokens", "context_window", "tokenizer_encoding")))
        store.path.write_text(json.dumps(saved))
    runtime = LLMRuntime(store.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    context = {"requirements": "按照资料说明，存储带宽与网络互连决定集群的实际效率。\n" * 10000}
    schema = PlanningResearchStep.model_json_schema()
    try:
        result = await runtime.measure("directory_research", context, schema, 48000)
        assert result["characters"] > 192000
        assert result["fits"] and result["max_input_tokens"] == 600000
        assert result["measurement"] == "default_tokenizer_estimate"
        assert result["tokenizer_encoding"] == "o200k_base"
        assert result["input_tokens"] is None
        assert 0 < result["estimated_input_tokens"] < 600000
        assert runtime.fits_prompt_budget("directory_research", context, schema, 48000)
    finally:
        await runtime.aclose()


async def test_default_budget_respects_small_window_when_serving_tokenizer_is_missing(tmp_path):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(base_url="http://route.test/v1", model="small-model", no_auth=True,
                                  max_tokens=8192))

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "small-model", "max_model_len": 32768}]})
        return httpx.Response(404)

    runtime = LLMRuntime(store.path)
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        schema = PlanningResearchStep.model_json_schema()
        small = await runtime.measure("directory_research", {"requirements": "核实资料"}, schema, 48000)
        assert small["fits"] and small["context_window"] == 32768
        context = {"requirements": "按照资料说明，存储带宽与网络互连决定集群的实际效率。\n" * 2000}
        large = await runtime.measure("directory_research", context, schema, 48000)
        assert not large["fits"] and large["context_window"] == 32768
        assert large["estimated_input_tokens"] < large["max_input_tokens"] == 600000
        assert not runtime.fits_prompt_budget("directory_research", context, schema, 48000)
    finally:
        await runtime.aclose()


async def test_server_counter_enforces_input_limit_and_keeps_tools(tmp_path):
    count, sent = 600000, []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "route-alias"}]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"count": count})

    runtime = await make_runtime(tmp_path, handler)
    schema = PlanningResearchStep.model_json_schema()
    try:
        good = await runtime.measure("directory_research", {"requirements": "核实资料"}, schema, 48000)
        assert good["input_tokens"] == 600000 and good["fits"]
        assert good["max_input_tokens"] == 600000 and good["context_window"] == 922000
        assert sent[0]["tools"] and sent[0]["messages"]
        count = 600001
        bad = await runtime.measure("directory_research", {"requirements": "核实另一份资料"}, schema, 48000)
        assert not bad["fits"]
    finally:
        await runtime.aclose()


async def test_missing_counter_uses_explicit_local_estimate_not_character_limit(tmp_path):
    runtime = await make_runtime(tmp_path, lambda request: httpx.Response(404))
    schema = PlanningResearchStep.model_json_schema()
    context = {"requirements": "Research paragraph about storage and network performance.\n" * 2000}
    try:
        result = await runtime.measure("directory_research", context, schema, 48000)
        assert result["characters"] > 48000 and result["fits"]
        assert result["input_tokens"] is None
        assert result["measurement"] == "local_tokenizer_estimate"
        assert 0 < result["estimated_input_tokens"] < 600000
        assert runtime.fits_prompt_budget("directory_research", context, schema, 48000)
        more = context | {"observations": [{"model_text": "已有资料中的明确结论。" * 1000}]}
        larger = await runtime.measure("directory_research", more, schema, 48000)
        assert larger["estimated_input_tokens"] > result["estimated_input_tokens"]
        runtime.config["max_input_tokens"] = result["estimated_input_tokens"]
        assert runtime.fits_prompt_budget("directory_research", context, schema, 48000)
        runtime.config["max_input_tokens"] -= 1
        assert not runtime.fits_prompt_budget("directory_research", context, schema, 48000)
        assert not (await runtime.measure("directory_research", context, schema, 48000))["fits"]
    finally:
        await runtime.aclose()


async def test_smaller_advertised_window_overrides_operator_window(tmp_path):
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "route-alias", "max_model_len": 32768}]})
        return httpx.Response(200, json={"count": 30000})

    runtime = await make_runtime(tmp_path, handler)
    try:
        measured = await runtime.measure("directory_research", {"requirements": "分析"}, {}, 48000)
        assert measured["input_tokens"] < measured["max_input_tokens"]
        assert measured["context_window"] == 32768
        assert not measured["fits"]  # Input alone fits, input plus reserved output does not.
    finally:
        await runtime.aclose()


async def test_over_budget_direct_native_call_never_reaches_generation(tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 600001})
        pytest.fail("Over-budget input reached the generation endpoint")

    runtime = await make_runtime(tmp_path, handler)
    try:
        from folio.runtime.errors import ModelCallFailed

        with pytest.raises(ModelCallFailed, match="capacity"):
            await runtime.complete("directory_research", {"requirements": "核对"},
                                   PlanningResearchStep.model_json_schema(), "budget")
        assert requests == ["/v1/models", "/tokenize"]
    finally:
        await runtime.aclose()


def test_image_estimate_reserves_image_tokens_instead_of_base64_text():
    from folio.runtime.image_inputs import IMAGE_TOKEN_RESERVE
    from folio.runtime.token_estimate import estimate_input

    text = [{"role": "user", "content": [{"type": "text", "text": "核对页面标题"}]}]
    with_image = [{"role": "user", "content": text[0]["content"] + [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}}
    ]}]
    assert estimate_input(with_image, tokenizer_encoding="o200k_base") == (
        estimate_input(text, tokenizer_encoding="o200k_base") + IMAGE_TOKEN_RESERVE)


async def test_directory_generation_uses_token_budget_above_old_192k_chars(tmp_path):
    from folio.document.prepare import prepare_with_model

    generated = []

    def handler(request):
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404)
        generated.append(json.loads(request.content))
        proposal = {"sections": [{"node_id": "scope", "title": "研究范围", "level": 1,
                                  "instruction": "解释资料适用范围。", "content_mode": "generate"}], "notes": []}
        event = {"choices": [{"delta": {"content": json.dumps(proposal)}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    runtime = await make_runtime(tmp_path, handler)
    try:
        output = tmp_path / "preview"
        await prepare_with_model("Analyze the available source materials in this research.\n" * 4000,
                                 [], output, runtime=runtime)
        attempt = json.loads((output / "planning-attempts.json").read_text())[0]
        assert attempt["prompt_chars"] > 192000
        assert attempt["capacity"]["fits"] and attempt["validated"]
        assert attempt["capacity"]["measurement"] == "local_tokenizer_estimate"
        assert len(generated) == 1
    finally:
        await runtime.aclose()


async def test_directory_research_review_keeps_complete_evidence_above_old_192k_chars(tmp_path):
    from folio.document.research_review import review_directory_research
    from folio.models import PlanItem, PlanProposal

    generated = []

    def handler(request):
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404)
        generated.append(json.loads(request.content))
        review = {"checks": [{"requirement_id": "r1", "disposition": "covered", "node_ids": ["n1"],
                              "evidence_ids": ["e1"], "reason": "目录明确安排存储带宽分析"}]}
        event = {"choices": [{"delta": {"content": json.dumps(review)}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    runtime = await make_runtime(tmp_path, handler)
    original = "Research paragraph about storage and network performance.\n" * 6000
    source = {
        "status": "ready",
        "requirements": [{"requirement_id": "r1", "text": "分析存储带宽", "evidence_ids": ["e1"]}],
        "evidence": [{"evidence_id": "e1", "source_id": "storage.pdf", "source_version": "v1",
                      "locator": "全文", "text": original}],
    }
    directory = PlanProposal(sections=[PlanItem(node_id="n1", title="存储", level=1,
                                               instruction="分析存储带宽")])
    try:
        result = await review_directory_research(runtime, directory, source)
        assert result["status"] == "passed" and len(generated) == 1
        call = result["calls"][0]
        assert call["capacity"]["characters"] > 192000 and call["capacity"]["fits"]
        assert call["capacity"]["estimated_input_tokens"] < 600000
        assert call["input"]["evidence"][0]["text"] == original
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("stage", ["planning", "research_review"])
async def test_directory_capacity_failure_reports_smaller_token_window_not_character_budget(tmp_path, stage):
    from folio.document.prepare import prepare_with_model
    from folio.document.research_review import review_directory_research
    from folio.models import PlanItem, PlanProposal
    from folio.runtime.errors import PlanningContextTooLarge, public_model_error

    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "route-alias", "max_model_len": 32768}]})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 30000})
        pytest.fail("A request above the known context window must not reach generation")

    runtime = await make_runtime(tmp_path, handler)
    try:
        if stage == "planning":
            with pytest.raises(PlanningContextTooLarge) as caught:
                await prepare_with_model("分析存储带宽", [], tmp_path / "preview", runtime=runtime)
            message = public_model_error(caught.value)["text"]
        else:
            source = {
                "status": "ready",
                "requirements": [{"requirement_id": "r1", "text": "存储带宽", "evidence_ids": ["e1"]}],
                "evidence": [{"evidence_id": "e1", "source_id": "storage.pdf", "source_version": "v1",
                              "locator": "第1页", "text": "存储带宽要求"}],
            }
            directory = PlanProposal(sections=[PlanItem(node_id="n1", title="存储", level=1,
                                                       instruction="分析存储带宽")])
            result = await review_directory_research(runtime, directory, source)
            assert result["status"] == "gaps" and result["issue_kind"] == "review_capacity"
            assert not result["capacity"]["fits"] and result["calls"] == []
            message = result["issues"][0]
        assert "输入实测 30,000 Token" in message
        assert "输入预算 600,000 Token" in message
        assert "模型上下文窗口 32,768 Token" in message
        assert "超过模型上下文窗口" in message
        assert "max_planning_prompt_chars" not in message and "192,000" not in message
        assert requests == ["/v1/models", "/tokenize"]
    finally:
        await runtime.aclose()
