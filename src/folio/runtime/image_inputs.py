"""Local, immutable PNG inputs for explicit source and output page requests."""

import base64
import hashlib
import json
from pathlib import Path

from folio.runtime.errors import ModelCallFailed

MAX_IMAGES = 2
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TOKEN_RESERVE = 4096


def image_messages(messages, context, *, supported, preserve_user_text=False):
    if not supported:
        raise ModelCallFailed("Image input is not explicitly enabled", "configuration")
    images = context.get("page_images", [])
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError("Page review requires one or two page images")
    text_context = context | {
        "page_images": [{k: v for k, v in item.items() if k != "path"} for item in images]
    }
    parts = [{
        "type": "text",
        "text": messages[1]["content"] if preserve_user_text else json.dumps(text_context, ensure_ascii=False),
    }]
    for item in images:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Invalid page image size")
        value = path.read_bytes()
        if not value.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Page input must be a PNG")
        if hashlib.sha256(value).hexdigest() != item.get("sha256"):
            raise ValueError("Page image changed after the request was prepared")
        label = f"Document page {item['page']}"
        if item.get("source_id"):
            label = f"Source {item['source_id']}, page {item['page']}; region: {item.get('region') or 'full page'}"
        parts.append({"type": "text", "text": label})
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(value).decode("ascii"),
                    "detail": "high",
                },
            }
        )
    return [messages[0], {"role": "user", "content": parts}, *messages[2:]]


def text_characters(messages):
    characters = 0
    for message in messages:
        if isinstance(message.get("reasoning_content"), str):
            characters += len(message["reasoning_content"])
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            characters += sum(len(part.get("text", "")) for part in content)
        if message.get("tool_calls"):
            characters += len(json.dumps(message["tool_calls"], ensure_ascii=False, separators=(",", ":")))
        if message.get("tool_call_id"):
            characters += len(message["tool_call_id"])
    return characters
