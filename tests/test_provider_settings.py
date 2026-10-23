import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.model_settings import (
    ModelSettingsStore,
    feature_enabled,
    load_model_config,
)
from folio.model_settings import (
    test_tavily_connection as tavily_probe,
)

CUSTOM = {"base_url": "http://llm.test/v1", "model": "custom-model", "api_key": "llm-test-secret", "decision_base_url": "https://judge.test/v1", "decision_model": "judge"}
KEYS = {"decision_api_key": "jev-test-secret", "tavily_api_key": "tavily-test-secret"}
ROUTER = {
    "provider_mode": "custom",
    "decision_base_url": "https://judge.test/v1",
    "decision_model": "judge",
    "decision_api_key": "judge-secret",
    "base_url": "https://router.test/auto/v1",
    "model": "gpt-sol",
    "api_key": "router-test-secret",
}


@pytest.fixture
def probes(monkeypatch):
    calls = []
    outcomes = {"llm": True, "decision": True, "tavily": True}

    for component, name in [
        ("llm", "test_connection"),
        ("decision", "test_decision_connection"),
        ("tavily", "test_tavily_connection"),
    ]:

        async def probe(config, component=component):
            calls.append((component, config))
            return {"ok": outcomes[component], "message": "probe result"}

        monkeypatch.setattr(f"folio.api.app.{name}", probe)
    return calls, outcomes


def test_optional_connections_can_be_tested_without_any_llm_fields(tmp_path, probes):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/settings/model/test/decision", json={}).status_code == 422
        assert client.post("/settings/model/test/tavily", json={}).status_code == 422
        result = client.post(
            "/settings/model/test/decision",
            json={"decision_api_key": KEYS["decision_api_key"], "decision_base_url": "https://judge.test/v1", "decision_model": "judge", "base_url": "unfinished"},
        )
        assert result.status_code == 200 and result.json()["ok"]
        result = client.post(
            "/settings/model/test/tavily", json={"tavily_api_key": KEYS["tavily_api_key"]}
        )
        assert result.status_code == 200 and result.json()["ok"]
        assert not client.get("/settings/model").json()["configured"]
        assert not (tmp_path / "settings/model.json").exists()
        proof = tmp_path / "settings/probe-state.json"
        assert stat.S_IMODE(proof.stat().st_mode) == 0o600
        assert set(json.loads(proof.read_text())) == {"decision", "tavily"}
        assert all(secret not in proof.read_text() for secret in KEYS.values())
        assert [component for component, _ in probes[0]] == ["decision", "tavily"]


def test_optional_enable_requires_server_proof_and_secrets_never_roundtrip(tmp_path, probes):
    config = CUSTOM | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    with TestClient(create_app(tmp_path)) as client:
        assert client.put("/settings/model", json=config).status_code == 422
        for component in ("llm", "decision", "tavily"):
            result = client.post(f"/settings/model/test/{component}", json=config)
            assert result.status_code == 200 and result.json()["ok"]
        response = client.put("/settings/model", json=config)
        assert response.status_code == 200
        public = response.json()
        assert public["decision_enabled"] and public["tavily_enabled"]
        assert public["test_status"] == {"llm": True, "decision": True, "tavily": True}
        assert all(
            public[field] for field in ("has_api_key", "has_decision_api_key", "has_tavily_api_key")
        )
        assert not (
            {"api_key", "decision_api_key", "tavily_api_key", "verified_capabilities"} & public.keys()
        )
        for secret in [CUSTOM["api_key"], *KEYS.values()]:
            assert secret not in response.text
            assert secret not in client.get("/settings/model").text
        assert (
            client.put(
                "/settings/model", json=config | {name: None for name in ["api_key", *KEYS]}
            ).status_code
            == 200
        )
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/settings/model").json()["test_status"] == public["test_status"]
    raw = load_model_config(tmp_path / "settings/model.json")
    assert raw["api_key"] == CUSTOM["api_key"] and raw["decision_api_key"] == KEYS["decision_api_key"]
    assert feature_enabled(raw, "decision") and feature_enabled(raw, "tavily")
    assert stat.S_IMODE((tmp_path / "settings/model.json").stat().st_mode) == 0o600


def test_capacity_policy_preserves_verified_connections_and_optional_probe_configuration(tmp_path, probes):
    config = ROUTER | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    capacity = {"max_input_tokens": 600000, "context_window": 922000, "tokenizer_encoding": "o200k_base"}
    with TestClient(create_app(tmp_path)) as client:
        for component in ("llm", "decision", "tavily"):
            assert client.post(f"/settings/model/test/{component}", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
        before = load_model_config(tmp_path / "settings/model.json")
        response = client.put("/settings/model", json=config | capacity)
        assert response.status_code == 200
        assert response.json()["test_status"] == {"llm": True, "decision": True, "tavily": True}
        for component in ("llm", "decision", "tavily"):
            assert client.post(f"/settings/model/test/{component}", json=config).json()["ok"]
            measured = probes[0][-1][1]
            assert {name: measured[name] for name in capacity} == capacity
        response = client.put("/settings/model", json=config | {name: None for name in ["api_key", *KEYS]})
        assert response.status_code == 200
        assert {name: response.json()[name] for name in capacity} == capacity
        after = load_model_config(tmp_path / "settings/model.json")
        assert after["verified_capabilities"] == before["verified_capabilities"]
        assert all(after[name] == before[name] for name in ["api_key", *KEYS])
        assert feature_enabled(after, "decision") and feature_enabled(after, "tavily")


def test_changed_credentials_invalidate_only_the_affected_connection(tmp_path, probes):
    config = CUSTOM | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    with TestClient(create_app(tmp_path)) as client:
        for component in ("llm", "decision", "tavily"):
            assert client.post(f"/settings/model/test/{component}", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
        before = (tmp_path / "settings/model.json").read_bytes()
        for changed in (
            {"decision_api_key": "replacement-jev"},
            {"tavily_api_key": "replacement-tavily"},
        ):
            assert client.put("/settings/model", json=config | changed).status_code == 422
            assert (tmp_path / "settings/model.json").read_bytes() == before
        result = client.put("/settings/model", json=config | {"model": "different-llm"})
        assert result.status_code == 200
        assert result.json()["test_status"] == {"llm": False, "decision": True, "tavily": True}
        result = client.put(
            "/settings/model",
            json=config | {"decision_api_key": "replacement-jev", "decision_enabled": False},
        )
        assert result.status_code == 200
        assert result.json()["test_status"] == {"llm": True, "decision": False, "tavily": True}


def test_independent_decision_target_binds_probe(tmp_path, probes):
    config = CUSTOM | KEYS | {"decision_enabled": True}
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/settings/model/test/decision", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
        for changed in ({"decision_model": "another"}, {"decision_api_key": "new-key"},
                        {"decision_base_url": "https://other.test/v1"}):
            assert client.put("/settings/model", json=config | changed).status_code == 422
        assert client.put("/settings/model", json=config | {"model": "any-model-name"}).status_code == 200


def test_failed_probe_revokes_enable_proof_without_saving_credentials(tmp_path, probes):
    config = CUSTOM | KEYS
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/settings/model/test/decision", json=config).json()["ok"]
        probes[1]["decision"] = False
        assert not client.post("/settings/model/test/decision", json=config).json()["ok"]
        assert (
            client.put("/settings/model", json=config | {"decision_enabled": True}).status_code
            == 422
        )
        assert not (tmp_path / "settings/model.json").exists()


@pytest.mark.parametrize("component", ["decision", "tavily"])
def test_failed_matching_probe_disables_saved_optional_capability_immediately(
    tmp_path, probes, component
):
    config = CUSTOM | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    other = "tavily" if component == "decision" else "decision"
    with TestClient(create_app(tmp_path)) as client:
        for name in ("decision", "tavily"):
            assert client.post(f"/settings/model/test/{name}", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
        probes[1][component] = False
        result = client.post(f"/settings/model/test/{component}", json=config)
        assert result.status_code == 200 and not result.json()["ok"]
        public = client.get("/settings/model").json()
        assert not public[f"{component}_enabled"] and not public["test_status"][component]
        assert public[f"{other}_enabled"] and public["test_status"][other]
        loaded = load_model_config(tmp_path / "settings/model.json")
        assert not feature_enabled(loaded, component)
        assert feature_enabled(loaded, other)
        assert component not in loaded["verified_capabilities"]
        assert loaded["api_key"] == CUSTOM["api_key"]
        assert all(loaded[field] == value for field, value in KEYS.items())
    with TestClient(create_app(tmp_path)) as client:
        assert not client.get("/settings/model").json()[f"{component}_enabled"]


@pytest.mark.parametrize("component", ["decision", "tavily"])
def test_failed_unsaved_credentials_preserve_saved_active_capability(tmp_path, probes, component):
    config = CUSTOM | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    changed_field = "decision_api_key" if component == "decision" else "tavily_api_key"
    with TestClient(create_app(tmp_path)) as client:
        for name in ("decision", "tavily"):
            assert client.post(f"/settings/model/test/{name}", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
        before = (tmp_path / "settings/model.json").read_bytes()
        probes[1][component] = False
        result = client.post(
            f"/settings/model/test/{component}",
            json=config | {changed_field: "different-unsaved-secret"},
        )
        assert result.status_code == 200 and not result.json()["ok"]
        assert (tmp_path / "settings/model.json").read_bytes() == before
        public = client.get("/settings/model").json()
        assert public[f"{component}_enabled"] and public["test_status"][component]
        assert feature_enabled(load_model_config(tmp_path / "settings/model.json"), component)


def test_configuration_proofs_cannot_be_supplied_by_browser_and_runtime_checks_snapshot(
    tmp_path, probes
):
    config = CUSTOM | KEYS | {"decision_enabled": True, "tavily_enabled": True}
    with TestClient(create_app(tmp_path)) as client:
        for field in ("verified_capabilities", "test_status"):
            assert (
                client.put("/settings/model", json=config | {field: {"decision": True}}).status_code
                == 422
            )
        for component in ("decision", "tavily"):
            assert client.post(f"/settings/model/test/{component}", json=config).json()["ok"]
        assert client.put("/settings/model", json=config).status_code == 200
    loaded = load_model_config(tmp_path / "settings/model.json")
    assert feature_enabled(loaded, "decision") and feature_enabled(loaded, "tavily")
    assert not feature_enabled(loaded | {"decision_api_key": "changed"}, "decision")
    assert not feature_enabled(loaded | {"tavily_api_key": "changed"}, "tavily")
    assert not feature_enabled(loaded | {"verified_capabilities": {}}, "decision")
    assert not feature_enabled(loaded | {"decision_enabled": False}, "decision")


def test_probe_validation_and_origin_errors_do_not_echo_any_credentials(tmp_path, probes):
    with TestClient(create_app(tmp_path)) as client:
        for component in ("llm", "decision", "tavily"):
            response = client.post(
                f"/settings/model/test/{component}",
                json=CUSTOM | KEYS,
                headers={"Origin": "https://elsewhere.test"},
            )
            assert response.status_code == 403
        response = client.post(
            "/settings/model/test",
            json=CUSTOM | {"base_url": "http://host/v1?key=" + CUSTOM["api_key"]},
        )
        assert response.status_code == 422 and CUSTOM["api_key"] not in response.text
        response = client.put(
            "/settings/model",
            json=CUSTOM | {"base_url": "http://" + CUSTOM["api_key"] + "＠host.test/v1"},
        )
        assert response.status_code == 422 and CUSTOM["api_key"] not in response.text
        for field in KEYS:
            response = client.post("/settings/model/test/tavily", json={field: KEYS[field] + "\n"})
            assert response.status_code == 422 and KEYS[field] not in response.text
        assert not probes[0]


@pytest.mark.parametrize(
    "mode", ["ok", "empty", "malformed", "unauthorized", "redirect", "timeout"]
)
async def test_tavily_probe_uses_official_endpoint_and_safe_protocol_errors(monkeypatch, mode):
    requests = []

    def handler(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("tavily-test-secret")
        if mode == "unauthorized":
            return httpx.Response(401, text="tavily-test-secret")
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://untrusted.test"})
        if mode == "malformed":
            return httpx.Response(200, json={"secret": "tavily-test-secret"})
        return httpx.Response(
            200,
            json={
                "results": []
                if mode == "empty"
                else [{"title": "Docs", "url": "https://tavily.com", "content": "documentation"}]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    result = await tavily_probe(KEYS)
    assert result["ok"] == (mode in {"ok", "empty"})
    assert result["code"] == {
        "ok": "search_verified",
        "empty": "search_verified",
        "malformed": "search_invalid_response",
        "unauthorized": "search_auth_failed",
        "redirect": "search_request_failed",
        "timeout": "search_timeout",
    }[mode]
    assert "tavily-test-secret" not in json.dumps(result)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.tavily.com/search"
    assert requests[0].headers["authorization"] == "Bearer tavily-test-secret"
    assert "api_key" not in json.loads(requests[0].content)


def test_legacy_configuration_keeps_optional_capabilities_disabled(tmp_path):
    store = ModelSettingsStore(tmp_path)
    store.directory.mkdir()
    store.path.write_text(
        json.dumps(
            CUSTOM
            | {
                "version": 1,
                "decision_provider": "llm",
                "no_auth": False,
                "max_tokens": 8192,
                "temperature": 0.1,
            }
        )
    )
    config = load_model_config(store.path)
    assert config["provider_mode"] == "custom"
    assert not feature_enabled(config, "decision") and not feature_enabled(config, "tavily")
    assert not store.public()["decision_enabled"] and not store.public()["tavily_enabled"]
