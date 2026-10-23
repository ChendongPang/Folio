"""Exercise display localization at the API boundary without model calls."""

import gzip
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.localization import LocalizationMiddleware
from folio.i18n import localize_payload, translate_text


@pytest.mark.parametrize("language", ["en", "en-US"])
def test_real_api_errors_and_validation_follow_accept_language(tmp_path, language):
    with TestClient(create_app(tmp_path)) as client:
        headers = {"Accept-Language": language}
        missing = client.get("/conversations/missing", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "The conversation does not exist"
        assert missing.json()["_translations"]["会话不存在"] == missing.json()["detail"]
        assert missing.headers["content-language"] == "en"
        assert "Accept-Language" in missing.headers["vary"]
        conversation = client.post("/conversations").json()["id"]
        invalid = client.patch(f"/conversations/{conversation}", headers=headers, json={"title": ""})
        assert invalid.status_code == 422
        assert "Conversation names must contain 1 to 200 characters" in invalid.text
        assert invalid.json()["detail"][0]["input"] == ""
        assert int(invalid.headers["content-length"]) == len(invalid.content)
        chinese = client.get("/conversations/missing", headers={"Accept-Language": "zh-CN"})
        assert chinese.json()["detail"] == "会话不存在"


def test_composed_diagnostics_have_sentence_spacing_and_reversible_metadata():
    original = (
        "《中文正文》的提取事实要点未完成：模型达到输出长度上限，结果仍不完整。"
        "已尝试 2 次。已保存 25/41 个正文章节，已有内容和检索进度保留。"
    )
    english = localize_payload({"detail": original}, "en")
    assert "“中文正文”" in english["detail"]
    assert ". Attempted 2 time(s). Saved 25/41" in english["detail"]
    assert localize_payload(english, "zh-CN")["detail"] == original
    assert localize_payload(english, "en")["_translations"] == english["_translations"]
    assert translate_text("自定义原文。未知说明；第三句。", "en") == "自定义原文。未知说明；第三句。"


def test_empty_conversation_title_is_localized_but_user_titles_are_preserved(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        cid = client.post("/conversations").json()["id"]
        headers = {"Accept-Language": "en"}
        empty = client.get(f"/conversations/{cid}", headers=headers).json()
        assert empty["title_is_default"] is True
        assert empty["title"] == "New document"
        listing = client.get("/conversations", headers=headers).json()["conversations"][0]
        assert listing["title"] == "New document" and listing["title_is_default"] is True
        renamed = client.patch(f"/conversations/{cid}", json={"title": "新文档"}, headers=headers).json()
        assert renamed["title_is_default"] is False and renamed["title"] == "新文档"
        assert client.get(f"/conversations/{cid}", headers=headers).json()["title"] == "新文档"


def test_user_and_model_prose_source_titles_and_protocol_values_are_preserved():
    source = {
        "messages": [
            {"role": "user", "content": "文件不能为空。"},
            {"role": "assistant", "content": "是", "content_origin": "model"},
            {"role": "assistant", "content": "文件不能为空。"},
            {"role": "assistant", "content": "文件不能为空。", "content_origin": "system"},
        ],
        "title": "正文", "filename": "标题.docx", "code": "protocol",
        "sections": [{"title": "标题", "description": "文件不能为空。"}],
        "evidence": [{"text": "文件不能为空。"}],
        "input": {"description": "文件不能为空。"},
        "detail": "文件不能为空。",
    }
    before = deepcopy(source)
    translated = localize_payload(source, "en")
    assert source == before
    assert translated["messages"][:3] == source["messages"][:3]
    assert translated["messages"][3]["content"] == "Files must not be empty."
    for key in ("title", "filename", "code", "sections", "evidence", "input"):
        assert translated[key] == source[key]
    assert translated["detail"] == "Files must not be empty."
    assert "“正文”" in translate_text("《正文》正在进行第 2 次提取事实要点，修正上次未完成的结果。", "en")


@pytest.mark.parametrize("key", ["progress_note", "layout_note", "purpose_note", "issues"])
def test_additional_runtime_fields_receive_translation_metadata(key):
    value = ["文件不能为空。"] if key == "issues" else "文件不能为空。"
    translated = localize_payload({key: value}, "en")
    assert translated[key] == (["Files must not be empty."] if key == "issues" else "Files must not be empty.")
    assert translated["_translations"] == {"文件不能为空。": "Files must not be empty."}


async def run_asgi(chunks, headers=None, language="en", after_chunk=None):
    sent = []
    response_headers = headers or [(b"content-type", b"application/json")]

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": response_headers})
        for index, body in enumerate(chunks):
            await send({"type": "http.response.body", "body": body, "more_body": index < len(chunks) - 1})
            if after_chunk:
                after_chunk(index, sent)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(deepcopy(message))

    await LocalizationMiddleware(app)({
        "type": "http", "headers": [(b"accept-language", language.encode())],
    }, receive, send)
    return sent


async def test_chunked_json_and_existing_vary_are_preserved():
    raw = json.dumps({"detail": "文件不能为空。"}, ensure_ascii=False).encode()
    messages = await run_asgi([raw[:23], raw[23:]], headers=[
        (b"content-type", b"application/problem+json"),
        (b"vary", b"Origin, Accept-Encoding"), (b"content-length", str(len(raw)).encode()),
    ])
    headers = dict(messages[0]["headers"])
    assert headers[b"vary"] == b"Origin, Accept-Encoding, Accept-Language"
    assert headers[b"content-language"] == b"en"
    assert json.loads(messages[1]["body"])["detail"] == "Files must not be empty."
    assert int(headers[b"content-length"]) == len(messages[1]["body"])


async def test_sse_preserves_incremental_delivery_utf8_ids_and_model_language():
    first = ('id: 17\r\nevent: work.update\r\ndata: {"text":"文件不能为空。"}\r\n\r\n').encode()
    # Split inside the first multibyte character and inside a CRLF frame boundary.
    split = first.index("文件".encode()) + 1
    chunks = [first[:split], first[split:-1], first[-1:]]
    chunks += [
        b'event: message.delta\ndata: {"text":"' + "是".encode() + b'"}\n\n',
        b'event: message.completed\ndata: {"text":"' + "是".encode() + b'","content_origin":"model"}\n\n',
        b': keepalive\n\ndata: [DONE]\n\n',
    ]

    def streamed(index, sent):
        bodies = [message for message in sent if message["type"] == "http.response.body"]
        if index < 2:
            assert bodies == []
        if index == 2:
            assert b"Files must not be empty." in bodies[-1]["body"]

    messages = await run_asgi(chunks, [(b"content-type", b"text/event-stream")], after_chunk=streamed)
    body = b"".join(message.get("body", b"") for message in messages)
    assert b"id: 17" in body
    assert b"Files must not be empty." in body
    events = [json.loads(line[6:]) for line in body.decode().splitlines()
              if line.startswith("data: {")]
    assert sum(event.get("text") == "是" for event in events) == 2
    assert b"data: [DONE]" in body and b": keepalive" in body
    assert messages[-1]["more_body"] is False
    assert len([message for message in messages if message["type"] == "http.response.body"]) == 4


@pytest.mark.parametrize("header", [
    (b"content-disposition", b'attachment; filename="research.json"'),
    (b"content-disposition", b'inline; filename="style.json"'),
    (b"content-encoding", b"gzip"),
])
async def test_artifacts_and_encoded_responses_are_byte_identical(header):
    raw = b'{"detail":"' + "文件不能为空。".encode() + b'"}'
    if header[0] == b"content-encoding":
        raw = gzip.compress(raw)
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()), header]
    messages = await run_asgi([raw[:13], raw[13:]], headers)
    assert messages[0]["headers"] == headers
    assert b"".join(message.get("body", b"") for message in messages) == raw


async def test_invalid_stream_frames_pass_through_without_localizer_failure():
    raw = b'event: work.update\ndata: {"text":"\xff"}\n\n'
    messages = await run_asgi([raw], [(b"content-type", b"text/event-stream")])
    assert messages[1]["body"] == raw
