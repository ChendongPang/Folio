"""Standard Chat Completions wire helpers for decision HTTP fixtures."""
import inspect
import json

import httpx


def payload(request):
    data = json.loads(request.content)
    if any(tool.get('function', {}).get('name') == 'submit_decisions' for tool in data.get('tools', [])):
        content = data['messages'][-1]['content']
        if isinstance(content, list):
            assets = {}
            for index, part in enumerate(content):
                if part.get('type') == 'image_url':
                    identity = content[index - 1]['text'].removeprefix('Image identifier: ')
                    url = part['image_url']['url']
                    assets[identity] = {'type': 'image', 'format': url.split('/')[1].split(';')[0], 'data_base64': url.split(',', 1)[1]}
            return {**data, **json.loads(content[0]['text']), 'assets': assets}
        return {**data, **json.loads(content)}
    return data


def transport(handler):
    async def wrapped(request):
        response = handler(request)
        if inspect.isawaitable(response):
            response = await response
        if response.status_code != 200 or 'application/json' not in response.headers.get('content-type', ''):
            return response
        try:
            data = response.json()
        except ValueError:
            return response
        if not isinstance(data, dict) or 'answers' not in data:
            return response
        return httpx.Response(200, json={
            'choices': [{'message': {'tool_calls': [{'function': {'name': 'submit_decisions', 'arguments': json.dumps({'answers': data['answers']})}}]}}],
            'model': data.get('model', 'decision-model'), 'usage': data.get('usage', {}),
        })
    return httpx.MockTransport(wrapped)
