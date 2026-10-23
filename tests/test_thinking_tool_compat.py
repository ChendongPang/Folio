"""Discover tool-choice compatibility without disabling native tool validation."""

import json

import httpx
import pytest
from test_native_tool_runtime import TOOLS, call_delta, response, runtime_for

from folio.runtime.errors import ModelCallFailed

REJECTION = {'error': {'message': 'tool_choice required is not supported in thinking mode'}}


def tool_response():
    return response([
        {'choices': [{'delta': {'reasoning_content': 'private reasoning '}}]},
        {'choices': [{'delta': {'reasoning_content': 'continuation', 'tool_calls': [call_delta()]}}]},
        {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
    ])


async def test_explicit_rejection_retries_auto_and_remembers_runtime_capability(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload['tool_choice'] == 'required':
            return httpx.Response(400, json=REJECTION)
        return tool_response()

    runtime = await runtime_for(tmp_path, handler)
    try:
        for _ in range(2):
            events = [item async for item in runtime.stream([{'role': 'user', 'content': 'read'}], tools=TOOLS)]
            completed = events[-1][1]
            assert completed['tool_calls'][0]['id'] == 'call_1'
            assert completed['reasoning_content'] == 'private reasoning continuation'
            assert 'private reasoning' not in json.dumps([item for item in events if item[0] != 'completed'])
        assert [item['tool_choice'] for item in requests] == ['required', 'auto', 'auto']
        assert requests[0] | {'tool_choice': 'auto'} == requests[1]
        assert not any('thinking' in item or 'reasoning_effort' in item for item in requests)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize('mode', ['text', 'reject_again', 'wrong_tool'])
async def test_auto_does_not_accept_text_or_retry_indefinitely(tmp_path, mode):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1 or mode == 'reject_again':
            return httpx.Response(400, json=REJECTION)
        if mode == 'text':
            return response([{'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]}])
        return response([{'choices': [{'delta': {'tool_calls': [call_delta(function={'name': 'unknown', 'arguments': '{}'})]}, 'finish_reason': 'tool_calls'}]}])

    runtime = await runtime_for(tmp_path, handler)
    try:
        with pytest.raises(ModelCallFailed):
            _ = [item async for item in runtime.stream([{'role': 'user', 'content': 'read'}], tools=TOOLS)]
        assert len(requests) == 2
    finally:
        await runtime.aclose()


@pytest.mark.parametrize('status', [400, 401, 429, 502])
async def test_unrelated_rejection_is_not_retried(tmp_path, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'message': 'unrelated error'}})

    runtime = await runtime_for(tmp_path, handler)
    try:
        with pytest.raises(ModelCallFailed):
            _ = [item async for item in runtime.stream([{'role': 'user', 'content': 'read'}], tools=TOOLS)]
        assert len(requests) == 1
    finally:
        await runtime.aclose()


async def test_plain_chat_never_exposes_reasoning_content(tmp_path):
    def handler(request):
        return response([{'choices': [{'delta': {'reasoning_content': 'private reasoning', 'content': 'hello'}, 'finish_reason': 'stop'}]}])

    runtime = await runtime_for(tmp_path, handler)
    try:
        events = [item async for item in runtime.stream([{'role': 'user', 'content': 'hello'}])]
        assert events[-1][1]['text'] == 'hello'
        assert 'private reasoning' not in json.dumps(events)
    finally:
        await runtime.aclose()


async def test_compatibility_retry_is_counted_as_a_separate_provider_attempt(tmp_path):
    from folio.runtime.interaction_audit import interaction_scope

    class Audit:
        def __init__(self):
            self.reservations = []
            self.events = []

        def begin_provider_attempt(self, job_id, entry):
            self.reservations.append(entry)

        def audit_event(self, job_id, kind, entry):
            self.events.append((kind, entry))

    def handler(request):
        if json.loads(request.content)['tool_choice'] == 'required':
            return httpx.Response(400, json=REJECTION)
        return tool_response()

    audit = Audit()
    runtime = await runtime_for(tmp_path, handler)
    try:
        with interaction_scope(audit, 'job', 'research', 'task'):
            _ = [item async for item in runtime.stream([{'role': 'user', 'content': 'read'}], tools=TOOLS)]
        assert len(audit.reservations) == 2
        assert [entry['outcome'] for _, entry in audit.events] == ['failed', 'success']
        assert audit.events[0][1]['error_code'] == 'tool_choice_unsupported'
        assert 'private reasoning' not in json.dumps(audit.events)
    finally:
        await runtime.aclose()


async def test_connection_probe_accepts_thinking_tools_after_explicit_rejection(tmp_path, monkeypatch):
    from folio.model_settings import ModelSettingsInput, ModelSettingsStore
    from folio.model_settings import test_connection as probe

    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload['tool_choice'] == 'required':
            return httpx.Response(400, json=REJECTION)
        return response([{'choices': [{'delta': {'reasoning_content': 'private probe reasoning', 'tool_calls': [call_delta(function={'name': 'confirm_connection', 'arguments': '{"ok":true}'})]}, 'finish_reason': 'tool_calls'}]}])

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    config = ModelSettingsStore(tmp_path).resolve(ModelSettingsInput(base_url='https://example.test', model='any-thinking-model', api_key='synthetic-key'))
    result = await probe(config)
    assert result['ok'] is True
    assert result['code'] == 'native_tools_verified'
    assert [item['tool_choice'] for item in requests] == ['required', 'auto']
    assert 'private probe reasoning' not in json.dumps(result)
