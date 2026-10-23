import json
import stat
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.model_settings import test_connection as probe
from folio.models import JobInput
from folio.runtime.demo import DemoRuntime
from folio.service import execute

DATA = {"base_url": "http://127.0.0.1:9000/v1", "model": "test-model", "api_key": "secret-test-key"}
CAPACITY = {"max_input_tokens": 600000, "context_window": 922000, "tokenizer_encoding": "o200k_base"}


def test_capacity_settings_survive_ui_save_and_null_restores_default_budget(tmp_path):
    from folio.model_settings import load_model_config

    with TestClient(create_app(tmp_path)) as client:
        response = client.put("/settings/model", json=DATA | CAPACITY)
        assert response.status_code == 200
        assert {name: response.json()[name] for name in CAPACITY} == CAPACITY
        # The existing settings form does not send these advanced fields.
        response = client.put("/settings/model", json=DATA | {"api_key": None, "temperature": 0.2})
        assert response.status_code == 200
        assert {name: response.json()[name] for name in CAPACITY} == CAPACITY
        saved = load_model_config(tmp_path / "settings/model.json")
        assert {name: saved[name] for name in CAPACITY} == CAPACITY
        assert saved["api_key"] == DATA["api_key"]
        assert DATA["api_key"] not in client.get("/settings/model").text

        # A known smaller window constrains requests at runtime, while the
        # application input ceiling remains the same.
        response = client.put("/settings/model", json=DATA | {"context_window": 500000})
        assert response.status_code == 200
        assert response.json()["context_window"] == 500000
        assert response.json()["max_input_tokens"] == 600000

        response = client.put("/settings/model", json=DATA | {"max_input_tokens": None})
        assert response.status_code == 200
        assert response.json()["max_input_tokens"] == 600000
        assert response.json()["context_window"] == 500000
        assert response.json()["tokenizer_encoding"] == CAPACITY["tokenizer_encoding"]
        response = client.put("/settings/model", json=DATA | dict.fromkeys(CAPACITY))
        assert response.status_code == 200
        expected = {"max_input_tokens": 600000, "context_window": None, "tokenizer_encoding": None}
        assert {name: response.json()[name] for name in CAPACITY} == expected
        assert {name: load_model_config(tmp_path / "settings/model.json")[name] for name in CAPACITY} == expected


@pytest.mark.parametrize("changed", [
    {"model": "other-model"},
    {"base_url": "http://other.test/v1"},
])
def test_target_change_keeps_default_budget_without_inheriting_window_or_encoding(tmp_path, changed):
    settings = ModelSettingsStore(tmp_path)
    original = DATA | {"model": "gpt-sol"}
    settings.save(ModelSettingsInput(**(original | CAPACITY)))
    updated = settings.save(ModelSettingsInput(**(original | changed)))
    assert updated["max_input_tokens"] == 600000
    assert updated["context_window"] is None and updated["tokenizer_encoding"] is None


@pytest.mark.parametrize("invalid", [
    {"max_input_tokens": 0}, {"max_input_tokens": -1}, {"max_input_tokens": True},
    {"context_window": 0}, {"context_window": 1.5}, {"tokenizer_encoding": "unknown"},
])
def test_capacity_settings_validate_units_and_bounds(tmp_path, invalid):
    with TestClient(create_app(tmp_path)) as client:
        assert client.put("/settings/model", json=DATA | invalid).status_code == 422
        assert not (tmp_path / "settings/model.json").exists()


def test_input_limit_equal_to_window_leaves_output_reservation_to_runtime():
    settings = ModelSettingsInput(**DATA, max_input_tokens=600000, context_window=600000)
    assert settings.max_input_tokens == settings.context_window == 600000


@pytest.mark.parametrize("old_capacity", [{}, dict.fromkeys(CAPACITY)])
def test_legacy_settings_load_default_budget_without_rewriting_secrets(tmp_path, old_capacity):
    from folio.model_settings import load_model_config

    store = ModelSettingsStore(tmp_path)
    store.directory.mkdir()
    legacy = ModelSettingsInput(**DATA).model_dump(mode="json") | DATA
    for name in CAPACITY:
        legacy.pop(name, None)
    store.path.write_text(json.dumps(legacy | old_capacity))
    before = store.path.read_bytes()
    assert load_model_config(store.path)["max_input_tokens"] == 600000
    assert store.public()["max_input_tokens"] == 600000
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("supports_images", [False, True])
def test_retired_page_scoring_is_ignored_in_saved_settings_and_old_clients(tmp_path, supports_images):
    from folio.model_settings import load_model_config

    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(**DATA, supports_image_input=supports_images))
    legacy = json.loads(store.path.read_text()) | {"layout_review_enabled": True}
    store.path.write_text(json.dumps(legacy))
    before = store.path.read_bytes()
    config = load_model_config(store.path)
    assert "layout_review_enabled" not in config
    assert config["supports_image_input"] is supports_images
    assert "layout_review_enabled" not in store.public()
    assert store.path.read_bytes() == before

    with TestClient(create_app(tmp_path)) as client:
        saved = client.put("/settings/model", json=DATA | {
            "supports_image_input": supports_images, "layout_review_enabled": True,
        })
        assert saved.status_code == 200
        assert "layout_review_enabled" not in saved.json()
        persisted = json.loads(store.path.read_text())
        assert "layout_review_enabled" not in persisted
        assert persisted["api_key"] == DATA["api_key"]
        assert persisted["supports_image_input"] is supports_images


def test_custom_input_budget_is_preserved_until_explicitly_reset(tmp_path):
    store = ModelSettingsStore(tmp_path)
    store.save(ModelSettingsInput(**DATA, max_input_tokens=200000))
    assert store.save(ModelSettingsInput(**DATA))["max_input_tokens"] == 200000
    assert store.save(ModelSettingsInput(**DATA, max_input_tokens=None))["max_input_tokens"] == 600000


def test_smaller_known_model_window_does_not_disable_default_budget():
    settings = ModelSettingsInput(**DATA, context_window=32768)
    assert settings.max_input_tokens == 600000
    assert settings.context_window == 32768


def test_default_output_budget_and_large_overrides_roundtrip(tmp_path):
    from folio.model_settings import load_model_config

    with TestClient(create_app(tmp_path)) as client:
        empty = client.get("/settings/model").json()
        assert empty["max_tokens"] == 128000
        assert empty["max_input_tokens"] == 600000
        assert empty["planning_max_tokens"] is None
        response = client.put("/settings/model", json=DATA)
        assert response.status_code == 200
        assert response.json()["max_tokens"] == 128000
        assert response.json()["planning_max_tokens"] is None

        # The application no longer imposes a 65,536-token ceiling on a
        # provider whose output limit is larger. Explicit lower budgets work too.
        for maximum, planning in [(128000, 128000), (256000, 196000), (1024, 512)]:
            response = client.put(
                "/settings/model",
                json=DATA | {"max_tokens": maximum, "planning_max_tokens": planning},
            )
            assert response.status_code == 200
            config = load_model_config(tmp_path / "settings/model.json")
            assert response.json()["max_tokens"] == config["max_tokens"] == maximum
            assert response.json()["planning_max_tokens"] == config["planning_max_tokens"] == planning


@pytest.mark.parametrize("value", [0, -1, True, 128000.5])
@pytest.mark.parametrize("field", ["max_tokens", "planning_max_tokens"])
def test_output_budgets_require_integer_token_counts(value, field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelSettingsInput(**(DATA | {field: value}))


def test_settings_roundtrip_keeps_secrets_server_side(tmp_path):
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        assert not client.get("/settings/model").json()["configured"]
        response = client.put("/settings/model", json=DATA)
        assert response.status_code == 200
        assert response.json()["has_api_key"]
        assert DATA["api_key"] not in response.text
        assert DATA["api_key"] not in client.get("/settings/model").text
        settings = ModelSettingsStore(root)
        assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
        # Blank input preserves the saved key, but only for the same endpoint.
        assert client.put("/settings/model", json=DATA | {"api_key": None}).status_code == 200
        assert json.loads(settings.path.read_text())["api_key"] == DATA["api_key"]
        changed = DATA | {"base_url": "http://127.0.0.1:9001/v1", "api_key": None}
        assert client.put("/settings/model", json=changed).status_code == 422
        assert client.post("/settings/model/test", json=changed).status_code == 422
        assert client.get("/settings/model").json()["base_url"] == DATA["base_url"]
        assert client.put("/settings/model", json=changed | {"no_auth": True}).status_code == 200
        assert not client.get("/settings/model").json()["has_api_key"]
        assert json.loads(settings.path.read_text())["api_key"] == ""


def test_settings_validation_and_origin_do_not_expose_credentials(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        for extra in [
            {"api_key": "secret-test-key\n"},
            {"api_key": {"secret": "secret-test-key"}},
            {"base_url": "file:///etc/passwd"},
            {"base_url": "http://host/v1?key=secret-test-key"},
            {"base_url": "http://host/v1/chat/completions"},
            {"model": "${SECRET}"},
        ]:
            response = client.put("/settings/model", json=DATA | extra)
            assert response.status_code == 422
            assert "secret-test-key" not in response.text
        response = client.put(
            "/settings/model", json=DATA, headers={"Origin": "https://another.test"}
        )
        assert response.status_code == 403
        assert not client.get("/settings/model").json()["configured"]
        assert (
            client.put(
                "/settings/model", json=DATA, headers={"Origin": "http://testserver"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "mode", ["ok", "unauthorized", "incomplete", "bad_stream", "redirect", "timeout", "text_only", "wrong_argument"]
)
@pytest.mark.parametrize("max_tokens", [1024, 128000])
async def test_real_probe_protocol_and_safe_failures(tmp_path, monkeypatch, mode, max_tokens):
    requests = []

    def handler(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("secret-test-key should not leak")
        if mode == "unauthorized":
            return httpx.Response(401, text="secret-test-key upstream sensitive details")
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "http://elsewhere.test"})
        if mode == "bad_stream":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, text="data: not-json\n\n"
            )
        call = {"index": 0, "id": "probe_1", "type": "function", "function": {
            "name": "confirm_connection", "arguments": json.dumps({"ok": mode != "wrong_argument"}),
        }}
        delta = {"content": "OK"} if mode == "text_only" else {"tool_calls": [call]}
        text = "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"
        if mode != "incomplete":
            text += "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop" if mode == "text_only" else "tool_calls"}]}) + "\n\n"
        text += "data: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=text)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = ModelSettingsStore(tmp_path)
    result = await probe(settings.resolve(ModelSettingsInput(**DATA, max_tokens=max_tokens)))
    assert result["ok"] == (mode == "ok")
    assert DATA["api_key"] not in json.dumps(result)
    assert not settings.path.exists()  # Testing alone must not save or replace credentials.
    assert len(requests) == 1
    assert str(requests[0].url) == DATA["base_url"] + "/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer " + DATA["api_key"]
    assert json.loads(requests[0].content)["stream"] is True
    assert json.loads(requests[0].content)["max_tokens"] == max_tokens
    assert json.loads(requests[0].content)["tool_choice"] == "required"
    assert json.loads(requests[0].content)["parallel_tool_calls"] is False
    if mode in {"text_only", "wrong_argument"}:
        assert "tool_calls" in result["message"]


async def test_worker_uses_saved_model_configuration(store, inputs, monkeypatch):
    settings = ModelSettingsStore(store.root)
    settings.save(ModelSettingsInput(**DATA))
    paths = []

    class RecordingRuntime(DemoRuntime):
        def __init__(self, config_path):
            paths.append(config_path)

    monkeypatch.delenv("FOLIO_MODEL_CONFIG", raising=False)
    monkeypatch.setattr("folio.runtime.llm.LLMRuntime", RecordingRuntime)
    for explicit in [None, Path("explicit-config.json")]:
        job = store.create(JobInput(**inputs, runtime="llm"))
        assert (await execute(store, job, explicit))["status"] == "published"
        assert paths[-1] == (explicit or settings.path)


def test_saved_settings_load_in_native_runtime(tmp_path):
    from folio.model_settings import load_model_config

    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(**DATA))
    config = load_model_config(settings.path)
    assert config["model"] == DATA["model"]
    assert config["base_url"] == DATA["base_url"]
    assert config["decision_provider"] == "llm"
    assert settings.path.name == "model.json"


async def test_connection_probe_checks_tools_even_when_schema_mode_is_enabled(tmp_path, monkeypatch):
    def handler(request):
        payload = json.loads(request.content)
        assert "response_format" not in payload
        assert payload["tools"][0]["function"]["parameters"]["required"] == ["ok"]
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "probe_1", "type": "function", "function": {"name": "confirm_connection", "arguments": '{"ok":true}'}}]}, "finish_reason": "tool_calls"}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    config = ModelSettingsStore(tmp_path).resolve(
        ModelSettingsInput(**DATA, enable_thinking=False, structured_output="json_schema")
    )
    assert (await probe(config))["ok"]


@pytest.mark.parametrize('status,code', [(401, 'authentication'), (402, 'billing'), (429, 'rate_limit'), (400, 'invalid_request')])
async def test_probe_retains_safe_reason_and_logs_diagnostics(tmp_path, monkeypatch, caplog, status, code):
    def handler(request):
        return httpx.Response(status, json={'error': {'message': 'secret-test-key private upstream body'}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    config = ModelSettingsStore(tmp_path).resolve(ModelSettingsInput(**DATA))
    result = await probe(config)
    assert result['ok'] is False
    assert result['code'] == code
    assert result['diagnostics']['status_code'] == status
    assert f'code={code}' in caplog.text
    assert 'status_code' in caplog.text
    for text in (json.dumps(result), caplog.text):
        assert 'secret-test-key' not in text
        assert 'private upstream body' not in text
