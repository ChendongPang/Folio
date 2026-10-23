import asyncio
import base64
import hashlib
import io
import json

import httpx
import pytest
from decision_wire import payload as decision_payload
from decision_wire import transport as decision_transport
from PIL import Image
from pydantic import ValidationError

from folio.decisions.layout_scoring import probe_layout_vision, score_layout
from folio.document.layout_scoring import PageLayoutScore
from folio.runtime.errors import ModelCallBudgetExceeded, ModelCallFailed
from folio.runtime.llm import LLMRuntime
from folio.runtime.timeouts import model_call_timeout


@pytest.fixture
def page_context(tmp_path):
    path = tmp_path / "page.png"
    # A real rendered portrait page exceeds the provider's maximum dimension.
    image = Image.new("RGB", (1200, 1800), "white")
    image.paste("black", (0, 0, 1200, 100))
    image.paste("red", (0, 1700, 1200, 1800))
    image.save(path)
    return {
        "page_number": 3,
        "total_pages": 10,
        "page_images": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "page": 3}],
        "style_reference": {"template": "Preserve the supplied serif headings."},
    }


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=decision_transport(handler), **kwargs)
    )


def score_answer(value):
    low = int(value)
    probabilities = {str(index): 0.0 for index in range(10)}
    probabilities[str(low)] = 1 - (value - low)
    if low < 9:
        probabilities[str(low + 1)] = value - low
    return {"type": "score", "score": value, "confidence": 0.91, "probabilities": probabilities}


@pytest.mark.parametrize("mode,expected_model", [("custom", "luna-decisions")])
@pytest.mark.parametrize("provider_score", [0, 7.199, 7.2, 9])
async def test_native_jev_scores_whole_page_on_ten_point_scale(
    monkeypatch, page_context, mode, expected_model, provider_score
):
    requests = []

    def handler(request):
        requests.append(request)
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        assert request.extensions["timeout"]["read"] == 123
        payload = decision_payload(request)
        assert payload["model"] == expected_model
        assert len(payload["questions"]["layout"]["criteria"]) == 10
        identity = payload["state"]["page_image"]
        assert list(payload["assets"]) == [identity]
        assert "path" not in json.dumps(payload["state"])
        assert payload["state"]["style_reference"] == page_context["style_reference"]
        asset = payload["assets"][identity]
        assert asset["type"] == "image" and asset["format"] == "png"
        with Image.open(io.BytesIO(base64.b64decode(asset["data_base64"]))) as image:
            assert image.height == 1568 and 280 <= image.width <= 1568
            # Both top and bottom content survive resizing: no hidden crop.
            assert image.getpixel((image.width // 2, 5)) == (0, 0, 0)
            assert image.getpixel((image.width // 2, image.height - 5)) == (255, 0, 0)
        return httpx.Response(200, json={
            "answers": {"layout": score_answer(provider_score)},
            "model": "jev-real-version", "usage": {"input_tokens": 500, "output_tokens": 1},
        })

    mock_http(monkeypatch, handler)
    config = {
        "provider_mode": mode, "base_url": "https://router.test/auto/v1",
        "api_key": "router-secret", "decision_base_url": "https://judge.test/v1", "decision_api_key": "judge-secret",
        "decision_enabled": False, "decision_model": "luna-decisions",
        "supports_image_input": False,
    }
    runtime = object.__new__(LLMRuntime)
    runtime.config = config
    runtime._layout_vision_lock = asyncio.Lock()
    runtime._layout_vision_probe = {"ok": True}
    with model_call_timeout(123):
        result = await runtime.complete("score_layout", page_context, PageLayoutScore.model_json_schema(), "layout")
    value = PageLayoutScore.model_validate_json(result.content)
    assert value.page == 3 and value.score == provider_score * 10 / 9
    assert (value.score >= 8) == (provider_score >= 7.2)
    assert value.provider_score == provider_score and value.confidence == 0.91
    assert value.reason == ""  # Jev did not generate an explanation.
    assert result.usage["decision_model"] == "jev-real-version"
    assert result.usage["decision_usage"][0]["input_tokens"] == 500
    assert result.usage["decision_calls"] == 1
    assert config["decision_model"] == "luna-decisions" and not config["decision_enabled"]
    assert "secret" not in json.dumps(result.usage)
    assert len(requests) == 1


@pytest.mark.parametrize("fault", ["changed", "wrong_page", "no_image", "multiple_images"])
async def test_invalid_page_never_reaches_provider(monkeypatch, page_context, fault):
    def unexpected_request(request):
        pytest.fail("Invalid page must be rejected before HTTP")

    mock_http(monkeypatch, unexpected_request)
    if fault == "changed":
        page_context["page_images"][0]["sha256"] = "wrong"
    elif fault == "wrong_page":
        page_context["page_number"] = 4
    elif fault == "no_image":
        page_context["page_images"] = []
    else:
        page_context["page_images"] *= 2
    with pytest.raises(ModelCallFailed, match="screenshot"):
        await score_layout({}, page_context)


async def test_image_rejection_never_falls_back_to_llm(monkeypatch, page_context):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"detail": "secret provider response"})

    mock_http(monkeypatch, handler)
    runtime = object.__new__(LLMRuntime)
    runtime.config = {"decision_base_url": "https://judge.test/v1", "decision_model": "judge", "decision_api_key": "test-secret"}
    runtime._layout_vision_lock = asyncio.Lock()
    runtime._layout_vision_probe = {"ok": True}
    with pytest.raises(ModelCallFailed, match="Jev 图片排版评分未完成") as error:
        await runtime.complete("score_layout", page_context, {}, "layout")
    assert "secret" not in str(error.value)
    assert error.value.diagnostics["status_code"] == 400
    assert len(requests) == 1


@pytest.mark.parametrize("correct", [True, False])
async def test_image_capability_probe_checks_pixels_not_http_success(monkeypatch, correct):
    def handler(request):
        payload = decision_payload(request)
        identity = payload["state"]["image"]
        value = base64.b64decode(payload["assets"][identity]["data_base64"])
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        assert list(payload["state"]) == ["image"]  # No answer leaked as text.
        assert list(payload["questions"]) == ["red", "green", "blue", "yellow"]
        with Image.open(io.BytesIO(value)) as image:
            pixels = [image.getpixel(point) for point in [(80, 80), (240, 80), (80, 240), (240, 240)]]
        colors = {"red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0)}
        positions = ["top_left", "top_right", "bottom_left", "bottom_right"]
        answers = {}
        for color in payload["questions"]:
            index = pixels.index(colors[color])
            choice = positions[index if correct else (index + 1) % 4]
            answers[color] = {"type": "choice", "choice": choice, "confidence": 0.95,
                              "probabilities": {name: float(name == choice) for name in positions}}
        return httpx.Response(200, json={"answers": answers, "model": "jev-vision", "usage": {}})

    mock_http(monkeypatch, handler)
    result = await probe_layout_vision({"decision_base_url": "https://judge.test/v1", "decision_model": "judge", "decision_api_key": "test-secret"})
    assert result["ok"] is correct and result["model"] == "jev-vision"
    assert result["usage"]["decision_calls"] == 1
    assert (result["diagnostics"]["actual"] == result["diagnostics"]["expected"]) is correct


@pytest.mark.parametrize("value", [-1, 10.1, float("nan"), float("inf")])
def test_page_score_rejects_invalid_numbers(value):
    with pytest.raises(ValidationError):
        PageLayoutScore(page=1, score=value)


async def test_jev_scoring_does_not_swallow_job_budget(monkeypatch, page_context):
    async def exhausted(*args, **kwargs):
        raise ModelCallBudgetExceeded(3500, 3500)

    monkeypatch.setattr("folio.decisions.layout_scoring.evaluate", exhausted)
    with pytest.raises(ModelCallBudgetExceeded):
        await score_layout({}, page_context)
    with pytest.raises(ModelCallBudgetExceeded):
        await probe_layout_vision({})


async def test_decision_fallback_does_not_swallow_job_budget(monkeypatch):
    from folio.decisions.routing import DecisionRouter

    async def exhausted(*args, **kwargs):
        raise ModelCallBudgetExceeded(3500, 3500)

    monkeypatch.setattr("folio.model_settings.feature_enabled", lambda *args: True)
    router = DecisionRouter({})
    monkeypatch.setattr(router, "questions", lambda *args, **kwargs: ({"ready": {"type": "noul"}}, lambda answer: answer))
    monkeypatch.setattr(router, "_evaluate_page", exhausted)
    with pytest.raises(ModelCallBudgetExceeded):
        await router.try_complete("intent", {}, {})


@pytest.mark.parametrize("supported", [True, False])
async def test_runtime_requires_single_vision_probe_before_parallel_page_scores(
    monkeypatch, page_context, supported
):
    probe_calls, score_calls = [], []

    async def probe(config):
        probe_calls.append(True)
        await asyncio.sleep(0.01)
        return {"ok": supported, "usage": {"decision_calls": 1, "decision_http_attempts": [{"outcome": "success"}], "decision_usage": [{"input_tokens": 100}]}}

    def handler(request):
        score_calls.append(request)
        return httpx.Response(200, json={"answers": {"layout": score_answer(8)}, "usage": {}, "model": "jev"})

    monkeypatch.setattr("folio.decisions.layout_scoring.probe_layout_vision", probe)
    mock_http(monkeypatch, handler)
    runtime = object.__new__(LLMRuntime)
    runtime.config = {"decision_base_url": "https://judge.test/v1", "decision_model": "judge", "decision_api_key": "test-secret"}
    runtime._layout_vision_lock = asyncio.Lock()
    runtime._layout_vision_probe = None
    results = await asyncio.gather(
        *(runtime.complete("score_layout", page_context, {}, str(index)) for index in range(3)),
        return_exceptions=True,
    )
    assert len(probe_calls) == 1
    if supported:
        assert len(score_calls) == 3
        assert sorted(result.usage["decision_calls"] for result in results) == [1, 1, 2]
    else:
        assert not score_calls
        assert all(isinstance(result, ModelCallFailed) and "未通过图片能力验证" in str(result) for result in results)


async def test_layout_capacity_uses_native_jev_budget_without_primary_model_probes(page_context):
    runtime = object.__new__(LLMRuntime)
    runtime.config = {"max_input_tokens": 600000, "max_tokens": 128000, "context_window": 2000}
    schema = PageLayoutScore.model_json_schema()
    capacity = await runtime.measure("score_layout", page_context, schema, 600000)
    assert capacity["fits"] and capacity["context_window"] == 32000
    assert capacity["output_tokens"] == 0 and capacity["image_token_reserve"] == 4096
    assert capacity["measurement"] == "native_jev_image_estimate"
    assert runtime.prompt_chars("score_layout", page_context, schema) == capacity["characters"]
    assert runtime.fits_prompt_budget("score_layout", page_context, schema, 600000)
    page_context["style_reference"] = {"rule": "This is a very long explicit template requirement. " * 10000}
    capacity = await runtime.measure("score_layout", page_context, schema, 600000)
    assert not capacity["fits"]
