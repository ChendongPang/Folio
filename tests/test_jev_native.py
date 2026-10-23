"""Native Jev URLs retain their protocol without affecting generic endpoints."""

import json

import httpx
import pytest

from folio.decision_client import (
    DecisionServiceError,
    collect_http_attempts,
    decision_target,
    evaluate,
)
from folio.model_settings import test_decision_connection as probe

CONFIG = {'decision_base_url': 'https://api.typesafe.test/v1/systemone', 'decision_model': 'jev-latest', 'decision_api_key': 'synthetic-key'}
QUESTIONS = {'ready': {'type': 'noul', 'instructions': 'Is status ready?'}}
ANSWERS = {'ready': {'type': 'noul', 'noul': 0.95}}


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize('base,expected', [
    ('https://api.typesafe.test/v1/systemone/', 'https://api.typesafe.test/v1/systemone'),
    ('https://proxy.test/decision/systemone', 'https://proxy.test/decision/systemone'),
    ('https://judge.test/v1', 'https://judge.test/v1/chat/completions'),
    ('https://judge.test/systemone-other', 'https://judge.test/systemone-other/chat/completions'),
])
def test_endpoint_protocol_is_selected_by_explicit_path(base, expected):
    assert decision_target(CONFIG | {'decision_base_url': base}) == (expected, 'jev-latest', 'synthetic-key')


async def test_native_probe_sends_state_questions_and_keeps_usage(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == CONFIG['decision_base_url']
        assert request.headers['authorization'] == 'Bearer synthetic-key'
        assert json.loads(request.content) == {'state': {'status': 'ready'}, 'questions': QUESTIONS, 'model': 'jev-latest'}
        return httpx.Response(200, json={'answers': ANSWERS, 'model': 'jev-1.14.0', 'usage': {'total_tokens': 17}})

    transport(monkeypatch, handler)
    attempts = []
    with collect_http_attempts(attempts):
        result = await evaluate(CONFIG, {'status': 'ready'}, QUESTIONS)
    assert result == {'answers': ANSWERS, 'model': 'jev-1.14.0', 'usage': {'total_tokens': 17}}
    assert len(attempts) == len(requests) == 1
    assert attempts[0]['outcome'] == 'success'


@pytest.mark.parametrize('status,code', [(401, 'authentication'), (404, 'endpoint_not_found'), (422, 'upstream')])
async def test_native_probe_exposes_status_without_credentials(monkeypatch, caplog, status, code):
    transport(monkeypatch, lambda request: httpx.Response(status, json={'error': 'synthetic-key private data'}))
    result = await probe(CONFIG)
    assert result['ok'] is False
    assert result['code'] == code
    assert result['diagnostics']['status_code'] == status
    assert 'decision_connection_test_failed' in caplog.text
    assert 'synthetic-key' not in json.dumps(result) + caplog.text
    assert 'private data' not in json.dumps(result) + caplog.text


@pytest.mark.parametrize('body', [{'answers': {}}, {'answers': {'ready': {'type': 'noul', 'noul': 2}}}, {'choices': []}])
async def test_native_response_still_requires_complete_valid_answers(monkeypatch, body):
    transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(DecisionServiceError) as caught:
        await evaluate(CONFIG, {'status': 'ready'}, QUESTIONS)
    assert caught.value.code == 'protocol'


async def test_native_image_assets_use_native_header_and_schema(monkeypatch):
    assets = {'image': {'type': 'image', 'format': 'png', 'data_base64': 'AAAA'}}

    def handler(request):
        assert request.headers['TypeSafe-Danger-Zone-Images'] == 'true'
        payload = json.loads(request.content)
        assert payload['assets'] == assets
        assert 'messages' not in payload and 'tools' not in payload
        return httpx.Response(200, json={'answers': ANSWERS})

    transport(monkeypatch, handler)
    assert (await evaluate(CONFIG, {'status': 'ready'}, QUESTIONS, assets=assets))['answers'] == ANSWERS


def test_native_configuration_round_trip_retains_protocol_and_verified_capability(tmp_path):
    from folio.model_settings import (
        ModelSettingsInput,
        ModelSettingsStore,
        feature_enabled,
        load_model_config,
    )

    store = ModelSettingsStore(tmp_path)
    data = ModelSettingsInput(base_url='https://generator.test/v1', model='writer', api_key='generation-key', **CONFIG, decision_enabled=True)
    config = store.resolve(data, component='decision')
    store.record_probe(config, 'decision', True)
    store.save(data)
    loaded = load_model_config(store.path)
    assert loaded['decision_provider'] == 'systemone'
    assert feature_enabled(loaded, 'decision')
    assert decision_target(loaded)[0] == CONFIG['decision_base_url']
    assert store.public()['has_decision_api_key']
