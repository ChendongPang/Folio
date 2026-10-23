"""Explicit local estimates for routes without a serving token counter.

An operator-selected or default encoding is not proof of the route's actual tokenizer.
Keep these estimates separate from provider-measured input_tokens in audits.
"""

import json
from functools import lru_cache

DEFAULT_INPUT_TOKEN_BUDGET = 600_000
DEFAULT_OUTPUT_TOKEN_BUDGET = 128_000
DEFAULT_TOKENIZER_ENCODING = "o200k_base"


@lru_cache(maxsize=4)
def encoding(name):
    import tiktoken

    return tiktoken.get_encoding(name)


def estimate_input(messages, tools=(), *, schema=None, tokenizer_encoding=None,
                   parallel_tool_calls=False):
    images = 0
    text_messages = []
    for message in messages:
        value = dict(message)
        if isinstance(value.get("content"), list):
            images += sum(part.get("type") in {"image_url", "input_image"}
                          for part in value["content"])
            value["content"] = [part for part in value["content"] if part.get("type") in {"text", "input_text"}]
        text_messages.append(value)
    payload = {"messages": text_messages}
    if tools:
        payload |= {"tools": tools, "tool_choice": "required", "parallel_tool_calls": parallel_tool_calls}
    elif schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": schema}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoder = encoding(tokenizer_encoding or DEFAULT_TOKENIZER_ENCODING)
    # Bounded chunks avoid pathological tokenizer work on long repeated text.
    # Split boundaries may slightly overcount, which is appropriate here.
    tokens = sum(len(encoder.encode_ordinary(text[start:start + 8192]))
                 for start in range(0, len(text), 8192))
    from folio.runtime.image_inputs import IMAGE_TOKEN_RESERVE

    return tokens + 128 + images * IMAGE_TOKEN_RESERVE
