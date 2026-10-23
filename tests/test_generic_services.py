import json

import httpx
import pytest

from folio.decision_client import evaluate
from folio.knowledge.search import DeepSeekKnowledge
from folio.knowledge.tavily import WebSearchUnavailable
from folio.model_settings import ModelSettingsInput, ModelSettingsStore, feature_enabled

CONFIG = dict(base_url='https://llm.example/v1', model='any-model', api_key='generation-secret',
              decision_base_url='https://judge.example/v1', decision_model='any-judge',
              decision_api_key='decision-secret', search_provider='deepseek', deepseek_api_key='search-secret')


def test_independent_settings_and_secret_retention(tmp_path):
    store = ModelSettingsStore(tmp_path)
    data = ModelSettingsInput(**CONFIG)
    config = store.resolve(data)
    for component in ('decision', 'tavily'):
        store.record_probe(config, component, True)
    public = store.save(data.model_copy(update={'decision_enabled': True, 'tavily_enabled': True}))
    assert public['decision_enabled'] and public['tavily_enabled']
    assert public['search_provider'] == 'deepseek'
    assert all(key not in json.dumps(public) for key in ('generation-secret', 'decision-secret', 'search-secret'))
    blank = {**CONFIG, 'api_key': None, 'decision_api_key': None, 'deepseek_api_key': None}
    retained = store.resolve(ModelSettingsInput(**blank))
    assert retained['decision_api_key'] == 'decision-secret'
    with pytest.raises(ValueError):
        store.resolve(ModelSettingsInput(**{**blank, 'decision_base_url': 'https://other.example/v1'}), component='decision')
    assert not feature_enabled({**config, 'search_provider': 'tavily', 'tavily_enabled': True}, 'tavily')


async def test_generic_decision_uses_native_tool_call(monkeypatch):
    def handler(request):
        assert str(request.url) == 'https://judge.example/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer decision-secret'
        data = json.loads(request.content)
        assert data['model'] == 'any-judge'
        assert 'state' not in data and 'questions' not in data
        assert data['tools'][0]['function']['name'] == 'submit_decisions'
        return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{'function': {
            'name': 'submit_decisions', 'arguments': json.dumps({'answers': {'ready': {'type': 'noul', 'noul': 1}}})}}]}}], 'usage': {'total_tokens': 50}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = await evaluate(CONFIG, {'ready': True}, {'ready': {'type': 'noul'}})
    assert result['answers']['ready']['noul'] == 1
    assert result['usage']['total_tokens'] == 50


async def test_deepseek_official_search_and_evidence(monkeypatch):
    def handler(request):
        assert str(request.url) == 'https://api.deepseek.com/v1/tools/search'
        assert request.headers['authorization'] == 'Bearer search-secret'
        assert json.loads(request.content) == {'query': 'public keywords', 'count': 3, 'need_content': True}
        return httpx.Response(200, json={'results': [
            {'url': 'https://example.org/source', 'title': 'Source', 'content': '', 'summary': 'Supported public evidence'},
            {'url': 'file:///etc/passwd', 'summary': 'invalid'},
        ]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    evidence = await DeepSeekKnowledge('search-secret').search('public keywords', limit=3, max_chars=100)
    assert len(evidence) == 1 and evidence[0].text == 'Supported public evidence'
    assert evidence[0].metadata['project_fact'] is False
    assert evidence[0].metadata['search_provider'] == 'deepseek'
    with pytest.raises(WebSearchUnavailable):
        await DeepSeekKnowledge('search-secret').search('x' * 129, limit=3, max_chars=100)


@pytest.mark.parametrize('mode', ['empty', 'auth', 'malformed', 'timeout'])
async def test_deepseek_probe_and_errors_do_not_leak_keys(monkeypatch, mode):
    from folio.model_settings import test_tavily_connection
    def handler(request):
        if mode == 'timeout':
            raise httpx.ReadTimeout('secret-in-upstream-error')
        if mode == 'auth':
            return httpx.Response(401, text='secret-in-upstream-error')
        return httpx.Response(200, json={'results': []} if mode == 'empty' else {'unexpected': 'secret-in-upstream-error'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = await test_tavily_connection(CONFIG)
    assert result['ok'] is (mode == 'empty')
    assert 'secret-in-upstream-error' not in json.dumps(result)


def test_legacy_model_settings_keep_generation_credentials_without_private_dispatch(tmp_path):
    store = ModelSettingsStore(tmp_path)
    store.directory.mkdir()
    store.path.write_text(json.dumps({
        **CONFIG, 'provider_mode': 'airouter', 'model': 'old-alias',
        'no_auth': False, 'temperature': 0.1, 'enable_thinking': False,
        'decision_enabled': True, 'jev_api_key': 'legacy-key',
    }))
    public = store.public()
    assert public['base_url'] == CONFIG['base_url']
    assert public['model'] == 'old-alias' and public['has_api_key']
    assert public['provider_mode'] == 'custom'
    assert public['enable_thinking'] is None
    assert not public['decision_enabled']
    assert 'legacy-key' not in json.dumps(public)
