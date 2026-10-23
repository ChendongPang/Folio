"""Measure the actual wire request with the serving tokenizer when available."""

import asyncio
import hashlib
import json

import httpx

from folio.research.native_tools import RESEARCH_TOOL_TASKS
from folio.research.transport import research_schema_chars
from folio.runtime.errors import ModelCallFailed
from folio.runtime.image_inputs import IMAGE_TOKEN_RESERVE, text_characters
from folio.runtime.interaction_audit import provider_attempt
from folio.runtime.provider_parameters import template_parameters
from folio.runtime.token_estimate import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    DEFAULT_TOKENIZER_ENCODING,
    estimate_input,
)
from folio.runtime.tool_calls import tool_characters

ADAPTIVE_OUTPUT_TASKS = {"generate", "repair", "validate", "review_work", "review"}


class Capacity:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ready = False
        self.window = runtime.config.get("context_window")
        self.counter = False
        self.lock = asyncio.Lock()
        self.cache = {}
        self.local_cache = {}

    @property
    def input_limit(self):
        return self.runtime.config.get("max_input_tokens") or DEFAULT_INPUT_TOKEN_BUDGET

    def local_measure(self, task, context, schema, char_limit):
        """Use the same token policy while assembling a request, without HTTP."""
        runtime = self.runtime
        messages, wire_schema, *_ = runtime.request_parts(task, context, schema)
        tools = runtime.request_tools(task, context, wire_schema)
        strict_schema = wire_schema if runtime.config.get("structured_output") == "json_schema" else None
        chars = runtime.prompt_chars(task, context, schema)
        requested = runtime.output_tokens(task, context)
        tokenizer_encoding = runtime.config.get("tokenizer_encoding")
        parallel = task in RESEARCH_TOOL_TASKS
        key = hashlib.sha256(json.dumps([messages, tools, strict_schema, tokenizer_encoding, parallel],
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if key not in self.local_cache:
            if len(self.local_cache) >= 128:
                self.local_cache.pop(next(iter(self.local_cache)))
            self.local_cache[key] = estimate_input(
                messages, tools, schema=strict_schema, tokenizer_encoding=tokenizer_encoding,
                parallel_tool_calls=parallel)
        estimated = self.local_cache[key]
        image_reserve = IMAGE_TOKEN_RESERVE * sum(
            part.get("type") in {"image_url", "input_image"}
            for message in messages if isinstance(message.get("content"), list)
            for part in message["content"]
        )
        output = self.bounded_output(task, estimated, requested)
        return {
            "characters": chars,
            **({"tool_characters": tool_characters(tools)} if tools else {}),
            "input_tokens": None,
            "estimated_input_tokens": estimated,
            # Already included by estimate_input; expose the allocation without
            # adding it a second time to either the estimate or capacity check.
            **({"image_token_reserve": image_reserve} if image_reserve else {}),
            "max_input_tokens": self.input_limit,
            "output_tokens": output,
            "requested_output_tokens": requested,
            "context_window": self.window,
            "margin_tokens": 512,
            "measurement": "local_tokenizer_estimate" if tokenizer_encoding else "default_tokenizer_estimate",
            "tokenizer_encoding": tokenizer_encoding or DEFAULT_TOKENIZER_ENCODING,
            "fits": estimated <= self.input_limit
            and (self.window is None or estimated + output + 512 <= self.window),
        }

    async def request(self, method, url, **kwargs):
        # A temporary outage does not mean the serving tokenizer is unsupported.
        # Never permanently downgrade a known token window to a character check.
        try:
            async with asyncio.timeout(30):
                for attempt in range(4):
                    try:
                        with provider_attempt(
                            "llm_auxiliary", "models" if method == "GET" else "tokenize",
                            model=getattr(self.runtime, "model", None),
                        ) as audit:
                            response = await self.runtime.client.request(method, url, **kwargs)
                            audit["status_code"] = response.status_code
                            if response.status_code == 429 or (
                                response.status_code >= 500 and response.status_code != 501
                            ):
                                response.raise_for_status()
                        return response
                    except httpx.HTTPError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(2 ** (attempt + 1))
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ModelCallFailed(
                f"Capacity measurement temporarily unavailable ({type(exc).__name__}); original sources retained",
                "capacity_unavailable",
            ) from exc

    async def measure(self, task, context, schema, char_limit):
        runtime = self.runtime
        messages, wire_schema, *_ = runtime.request_parts(task, context, schema)
        tools = runtime.request_tools(task, context, wire_schema)
        output = runtime.output_tokens(task, context)
        schema_reserve = research_schema_chars(
            wire_schema, structured_output=runtime.config["structured_output"] == "json_schema"
        ) if task == "style_analysis" else 0
        tool_size = tool_characters(tools)
        chars = text_characters(messages) + schema_reserve + tool_size
        headers = (
            {"Authorization": "Bearer " + runtime.config["api_key"]}
            if runtime.config["api_key"]
            else {}
        )
        base = runtime.config["base_url"]
        counter_url = (base[:-3] if base.endswith("/v1") else base) + "/tokenize"
        if not self.ready:
            async with self.lock:
                if not self.ready:
                    try:
                        response = await self.request(
                            "GET", base + "/models", headers=headers, timeout=10
                        )
                        if response.status_code == 200:
                            model = next(
                                (
                                    m
                                    for m in response.json().get("data", [])
                                    if m.get("id") == runtime.model
                                ),
                                {},
                            )
                            advertised = model.get("max_model_len")
                            if type(advertised) is int and advertised > 0:
                                self.window = min(self.window, advertised) if self.window else advertised
                    except (ValueError, TypeError, KeyError):
                        pass
                    self.counter = type(self.window) is int and self.window > 0
                    self.ready = True
        tokens = None
        if self.counter:
            body = {"model": runtime.model, "messages": messages, "add_generation_prompt": True}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "required"
                body["parallel_tool_calls"] = task in RESEARCH_TOOL_TASKS
            body.update(template_parameters(runtime.config))
            key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if key in self.cache:
                tokens = self.cache[key]
            else:
                try:
                    response = await self.request(
                        "POST", counter_url, headers=headers, json=body, timeout=15
                    )
                    data = response.json() if response.status_code == 200 else {}
                    count = data.get("count")
                    if type(count) is int and count >= 0:
                        tokens = count
                        if len(self.cache) >= 512:
                            self.cache.pop(next(iter(self.cache)))
                        self.cache[key] = count
                    else:
                        self.counter = False
                except (ValueError, TypeError):
                    self.counter = False
        if tokens is None:
            return self.local_measure(task, context, schema, char_limit)
        return {
            "characters": chars,
            **({"tool_characters": tool_size} if tool_size else {}),
            "input_tokens": tokens,
            "output_tokens": self.bounded_output(task, tokens, output),
            "requested_output_tokens": output,
            "context_window": self.window,
            "max_input_tokens": self.input_limit,
            "margin_tokens": 512,
            "measurement": "server_tokenizer_with_schema_reserve" if schema_reserve else "server_tokenizer",
            **({"schema_characters": schema_reserve, "schema_token_reserve": schema_reserve} if schema_reserve else {}),
            "fits": tokens + schema_reserve <= self.input_limit
            and tokens + schema_reserve + self.bounded_output(task, tokens, output) + 512 <= self.window,
        }

    def bounded_output(self, task, tokens, requested):
        # max_tokens is an upper bound, not a requirement to reserve all of it.
        # Keep a useful writing allowance; smaller windows still require fewer inputs.
        if task in ADAPTIVE_OUTPUT_TASKS and tokens is not None and self.window:
            available = self.window - tokens - 512
            minimum = 4096 if task in {"generate", "repair"} else 2048
            if available >= min(requested, minimum):
                return min(requested, available)
        return requested
