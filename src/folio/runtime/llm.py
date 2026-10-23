"""Folio-owned Chat Completions transport; no agent-framework dependency."""

import asyncio
import json
from collections import deque
from contextlib import aclosing
from pathlib import Path
from time import monotonic

import httpx
from jsonschema import Draft202012Validator

from folio.document.planning_transport import PLANNING_TASKS
from folio.model_settings import load_model_config
from folio.research.native_tools import NATIVE_TOOL_TASKS, RESEARCH_TOOL_TASKS, tool_definitions
from folio.research.transport import research_messages, research_schema_chars
from folio.runtime.activity import ModelActivity, model_activity_task
from folio.runtime.base import RuntimeResult, planning_messages, prompt_for
from folio.runtime.business_transport import (
    BUSINESS_TASKS,
    WIRE_INSTRUCTIONS,
    business_payload,
    evidence_schema,
    restore_empty_fields,
    restore_material_kinds,
    translate_ids,
)
from folio.runtime.capacity import ADAPTIVE_OUTPUT_TASKS, Capacity
from folio.runtime.diagnostics import (
    capture_partial_output,
    safe_failure_diagnostics,
    tool_delta_for_audit,
)
from folio.runtime.errors import (
    ModelCallFailed,
    ModelContextLimit,
    model_http_error,
    model_stream_error,
    protocol_failure,
)
from folio.runtime.extraction_transport import (
    WIRE_INSTRUCTION as EXTRACTION_WIRE_INSTRUCTION,
)
from folio.runtime.extraction_transport import (
    extraction_context,
    extraction_schema,
    is_compact_extraction,
    restore_extraction,
)
from folio.runtime.image_inputs import IMAGE_TOKEN_RESERVE, image_messages, text_characters
from folio.runtime.output_guard import RepeatedParagraphGuard, validate_paragraph_cycles
from folio.runtime.plan_repair_transport import (
    WIRE_INSTRUCTION,
    repair_transport,
    restore_repair,
)
from folio.runtime.provider_parameters import generation_parameters
from folio.runtime.schema_compat import STRICT_FIELDS_INSTRUCTION, strict_output_schema
from folio.runtime.timeouts import current_model_call_timeout
from folio.runtime.token_estimate import DEFAULT_OUTPUT_TOKEN_BUDGET, estimate_input
from folio.runtime.tool_calls import ToolCallStream, tool_characters
from folio.runtime.work_plan_transport import restore_work_plan, work_plan_payload
from folio.runtime.writing_transport import (
    remove_duplicate_inline_citations,
    restore_primary_table,
    writing_schema,
)


class LLMRuntime:
    provider = "llm"

    def __init__(self, config_path: Path):
        self.config = load_model_config(config_path)
        self.model = self.config["model"]
        from folio.decisions.routing import DecisionRouter
        from folio.model_settings import feature_enabled

        self.decision_router = DecisionRouter(self.config)
        from folio.decisions.selection import MaterialSelector

        self.material_selector = MaterialSelector(self)
        self.capacity = Capacity(self)
        # Auxiliary requests use short transport defaults. Generation streams
        # override only the read timeout and retain their total call deadline.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10, write=10, pool=10),
            follow_redirects=False,
            trust_env=False,
        )
        self.identity = {
            key: self.config[key]
            for key in (
                "base_url",
                "model",
                "max_tokens",
                "planning_max_tokens",
                "temperature",
                "enable_thinking",
                "structured_output",
                "supports_image_input",
            )
        }
        self.identity["optional_services"] = {
            "provider_mode": self.config.get("provider_mode", "custom"),
            "decision_enabled": feature_enabled(self.config, "decision"),
            "decision_model": self.config.get("decision_model", ""),
            "decision_base_url": self.config.get("decision_base_url", ""),
            "tavily_enabled": feature_enabled(self.config, "tavily"),
            "search_provider": self.config.get("search_provider", "tavily"),
        }
        self.identity["generation_parameters"] = generation_parameters(self.config)
        self._native_tool_choice = "required"
        self.material_selection_policy = 3
        self._layout_vision_lock = asyncio.Lock()
        self._layout_vision_probe = None

    async def stream(
        self, messages, *, max_tokens=None, schema=None, tools=None, allow_context_retry=True,
        parallel_tool_calls=False, capacity_checked=False,
    ):
        headers = {}
        if self.config["api_key"]:
            headers["Authorization"] = "Bearer " + self.config["api_key"]
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config["max_tokens"],
            **generation_parameters(self.config),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload.update(tools=tools, tool_choice=getattr(self, "_native_tool_choice", "required"), parallel_tool_calls=parallel_tool_calls)
        elif schema is not None and self.config["structured_output"] == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "folio_result",
                    "schema": strict_output_schema(self.config, schema),
                    "strict": True,
                },
            }
        if not capacity_checked:
            # Chat and connection probes supply raw messages rather than a task
            # context. Enforce the same input allowance without discarding history.
            # Task completions have already measured their exact wire request.
            wire_schema = payload.get("response_format", {}).get("json_schema", {}).get("schema")
            estimated = estimate_input(
                messages, tools or (), schema=wire_schema,
                tokenizer_encoding=self.config.get("tokenizer_encoding"),
                parallel_tool_calls=parallel_tool_calls,
            )
            window = self.capacity.window
            if estimated > self.capacity.input_limit or (
                window is not None and estimated + payload["max_tokens"] + 512 > window
            ):
                failure = ModelCallFailed("Request exceeds estimated token capacity", "context_limit")
                failure.diagnostics = {
                    "estimated_input_tokens": estimated,
                    "max_input_tokens": self.capacity.input_limit,
                    "requested_output_tokens": payload["max_tokens"],
                    "margin_tokens": 512,
                    **({"context_window": window} if window is not None else {}),
                }
                raise failure
        try:
            async with asyncio.timeout(current_model_call_timeout()):
                for attempt in range(3):
                    try:
                        async with aclosing(self._stream_tool_compatible(payload, headers)) as events:
                            async for kind, data in events:
                                if kind == "completed" and attempt:
                                    data["usage"] = data["usage"] | {
                                        "context_rejections": attempt,
                                        "effective_max_tokens": payload["max_tokens"],
                                    }
                                yield kind, data
                        return
                    except ModelContextLimit as exc:
                        if not allow_context_retry:
                            raise
                        reduced = exc.reduced_output(payload["max_tokens"])
                        if attempt == 2 or reduced is None:
                            raise
                        # Only retry a rejected HTTP request: no text was emitted.
                        # Preserve the complete input and schema.
                        payload = payload | {"max_tokens": reduced}
        except TimeoutError as exc:
            failure = ModelCallFailed("Model timed out", "timeout")
            failure.diagnostics = safe_failure_diagnostics(exc)
            raise failure from exc

    async def _stream_tool_compatible(self, payload, headers):
        """Retry only an explicit forced-tool rejection, keeping native validation."""
        try:
            async with aclosing(self._stream_once(payload, headers)) as events:
                async for event in events:
                    yield event
            return
        except ModelCallFailed as exc:
            if not (
                payload.get("tools")
                and payload.get("tool_choice") != "auto"
                and exc.code == "tool_choice_unsupported"
                and safe_failure_diagnostics(exc).get("status_code") == 400
            ):
                raise
        # Keep the provider's reasoning mode. Auto only removes forced selection;
        # ToolCallStream still requires valid native calls before anything executes.
        # Cache the discovered capability on this runtime, not by provider/model name.
        self._native_tool_choice = "auto"
        async with aclosing(self._stream_once(payload | {"tool_choice": "auto"}, headers)) as events:
            async for kind, data in events:
                if kind == "completed":
                    data = data | {"usage": data.get("usage", {}) | {"tool_choice_rejections": 1}}
                yield kind, data

    async def _stream_once(self, payload, headers):
        from folio.runtime.interaction_audit import provider_attempt

        with provider_attempt("llm", "chat.completions", model=self.model) as audit:
            async with aclosing(self._stream_http(payload, headers, audit)) as events:
                async for kind, data in events:
                    yield kind, data

    async def _stream_http(self, payload, headers, audit):
        content, usage, finish, size = "", {}, None, 0
        reasoning_parts = None
        # SSE carries a JSON envelope for every delta, often one token at a
        # time. Its wire size must grow with the configured output allowance.
        stream_character_limit = max(8_000_000, payload["max_tokens"] * 1024)
        tool_stream = ToolCallStream(payload["tools"]) if payload.get("tools") else None
        started, phase = monotonic(), "connect_or_response_headers"
        reasoning_chars, first_content_seconds = 0, None
        stream_times = {}
        recent_tool_deltas = deque(maxlen=4)
        event_index, done_received, pending_finish = 0, False, None
        activity = ModelActivity()

        def partial_content():
            if recent_tool_deltas or tool_stream and tool_stream.calls:
                return json.dumps(
                    {"current_tool_delta": tool_stream.current_delta if tool_stream else None,
                     "recent_tool_deltas": list(recent_tool_deltas),
                     "tool_calls": list(tool_stream.calls.values()) if tool_stream else [],
                     "content": content},
                    ensure_ascii=False,
                )
            return content

        def diagnostics(exception):
            values = {
                "exception": type(exception).__name__,
                "phase": phase,
                "received_chars": len(content),
                **({"tool_call_chars": tool_stream.characters} if tool_stream else {}),
                "reasoning_chars": reasoning_chars,
                "elapsed_seconds": round(monotonic() - started, 2),
                "call_timeout_seconds": current_model_call_timeout(),
                "stream_event_chars": size,
                "stream_character_limit": stream_character_limit,
                "requested_output_tokens": payload["max_tokens"],
                "trailing_whitespace_chars": len(content) - len(content.rstrip()),
                "stream_event_index": event_index,
                "done_received": done_received,
                **({"tool_calls_received": len(tool_stream.indices_seen)} if tool_stream else {}),
                **stream_times,
            }
            if "status_code" in audit:
                values["status_code"] = audit["status_code"]
            if first_content_seconds is not None:
                values["first_content_seconds"] = first_content_seconds
            observed_finish = finish if finish is not None else pending_finish
            if observed_finish is not None:
                values["finish_reason"] = (
                    observed_finish
                    if isinstance(observed_finish, str) and observed_finish in {"stop", "length", "content_filter", "tool_calls"}
                    else "unknown"
                )
            else:
                values["finish_reason"] = "missing"
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if type(value) is int and 0 <= value <= 10**12:
                    values[key] = value
            return values

        try:
            activity.emit("waiting")
            async with self.client.stream(
                "POST",
                self.config["base_url"] + "/chat/completions",
                json=payload,
                headers=headers,
                # A reasoning model can be silent between valid SSE events.
                # The enclosing call deadline bounds that wait; an independent
                # short idle timer must not abort an otherwise live generation.
                timeout=httpx.Timeout(
                    connect=self.client.timeout.connect, read=None,
                    write=self.client.timeout.write, pool=self.client.timeout.pool,
                ),
            ) as response:
                audit["status_code"] = response.status_code
                phase = "first_content"
                stream_times["response_headers_seconds"] = round(monotonic() - started, 2)
                if response.status_code != 200:
                    error_body = None
                    if response.status_code == 400:
                        await response.aread()
                        try:
                            error_body = response.json()
                        except ValueError:
                            pass
                    raise model_http_error(error_body, response.status_code)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise protocol_failure("response_not_sse")
                activity.emit("connected")
                async for line in response.aiter_lines():
                    size += len(line)
                    if size > stream_character_limit:
                        raise ModelCallFailed("Response exceeds budget", "length_limit")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_received = True
                        break
                    if not data:
                        continue
                    event_index += 1
                    try:
                        chunk = json.loads(data)
                    except ValueError as exc:
                        raise protocol_failure("stream_json_invalid") from exc
                    if not isinstance(chunk, dict):
                        raise protocol_failure("stream_shape_invalid")
                    stream_times["last_event_seconds"] = round(monotonic() - started, 2)
                    stream_times.setdefault(
                        "first_event_seconds", stream_times["last_event_seconds"]
                    )
                    if chunk.get("error"):
                        raise model_stream_error(chunk["error"])
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                        audit.update(
                            {
                                k: usage[k]
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                if k in usage
                            }
                        )
                    for choice in chunk.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        pending_finish = choice.get("finish_reason")
                        delta = choice.get("delta") or {}
                        if delta.get("function_call"):
                            recent_tool_deltas.append(tool_delta_for_audit([
                                {"function": delta["function_call"]},
                            ]))
                            raise protocol_failure("legacy_function_call")
                        if delta.get("tool_calls"):
                            recent_tool_deltas.append(tool_delta_for_audit(delta["tool_calls"]))
                            if tool_stream is None:
                                raise protocol_failure("unexpected_tool_call")
                            if finish is not None:
                                raise protocol_failure("tool_after_finish")
                            if first_content_seconds is None:
                                first_content_seconds = round(monotonic() - started, 2)
                                audit["first_content_ms"] = round((monotonic() - started) * 1000, 3)
                            phase = "streaming_tool_call"
                            stream_times["last_content_seconds"] = round(monotonic() - started, 2)
                            tool_stream.feed(delta["tool_calls"])
                            activity.emit("responding", response_chars=tool_stream.characters)
                        text = delta.get("content")
                        # Keep native assistant reasoning separately for tool continuation.
                        # Never forward it as visible answer text or progress.
                        native_reasoning = delta.get("reasoning_content")
                        if tool_stream is not None and isinstance(native_reasoning, str):
                            if reasoning_parts is None:
                                reasoning_parts = []
                            reasoning_parts.append(native_reasoning)
                        reasoning = native_reasoning or delta.get("reasoning")
                        if isinstance(reasoning, str):
                            reasoning_chars += len(reasoning)
                            if reasoning:
                                activity.emit("thinking")
                        if text:
                            if not isinstance(text, str):
                                raise protocol_failure("content_invalid")
                            if finish is not None:
                                raise protocol_failure("text_after_finish")
                            if first_content_seconds is None:
                                first_content_seconds = round(monotonic() - started, 2)
                                audit["first_content_ms"] = round(
                                    (monotonic() - started) * 1000, 3
                                )
                            content += text
                            activity.emit("responding", response_chars=len(content))
                            phase = "streaming_content"
                            stream_times["last_content_seconds"] = round(
                                monotonic() - started, 2
                            )
                            yield "text.delta", {"text": text}
                        if choice.get("finish_reason") is not None:
                            finish = choice["finish_reason"]
            if tool_stream is not None:
                calls = tool_stream.finish(finish)
            elif finish != "stop" or not content.strip():
                info = diagnostics(ModelCallFailed())
                failure = ModelCallFailed(
                    f"Incomplete response (finish_reason={info.get('finish_reason')!r}, "
                    f"content_chars={len(content)}, completion_tokens={info.get('completion_tokens')}, "
                    f"trailing_whitespace_chars={len(content) - len(content.rstrip())})",
                    "length_limit" if finish == "length" else "protocol",
                )
                failure.diagnostics = {"protocol_issue": "incomplete_response"}
                raise failure
            audit["termination"] = "completed"
            yield "completed", {
                "text": content, "usage": usage,
                **({"tool_calls": calls} if tool_stream else {}),
                **({"reasoning_content": "".join(reasoning_parts)} if reasoning_parts is not None else {}),
            }
        except ModelCallFailed as exc:
            if exc.code in {"length_limit", "timeout", "protocol"}:
                exc.diagnostics = diagnostics(exc) | safe_failure_diagnostics(exc)
                capture_partial_output(exc, partial_content())
            raise
        except asyncio.CancelledError as exc:
            # wait_for/timeout preserve this exception as TimeoutError.__cause__.
            # Keep diagnostics on the individual call, never shared runtime state.
            exc.diagnostics = diagnostics(exc)
            capture_partial_output(exc, partial_content())
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            failure = ModelCallFailed(
                f"Model timed out ({type(exc).__name__}, phase={phase}, received_chars={len(content)})",
                "timeout",
            )
            failure.diagnostics = diagnostics(exc)
            capture_partial_output(failure, partial_content())
            raise failure from exc
        except httpx.HTTPError as exc:
            raise ModelCallFailed("Model connection failed") from exc
        except (TypeError, AttributeError) as exc:
            failure = protocol_failure("malformed_response")
            failure.diagnostics = diagnostics(failure) | failure.diagnostics
            capture_partial_output(failure, partial_content())
            raise failure from exc

    def request_parts(self, task, context, schema):
        schema = writing_schema(task, context, schema)
        schema = repair_transport(task, context, schema)
        references = {}
        work_references = None
        business_references = {}
        if task == "style_analysis":
            from folio.document.style_profile import style_messages

            messages = style_messages(context, schema, structured_output=self.config["structured_output"] == "json_schema")
        elif task in NATIVE_TOOL_TASKS:
            messages = research_messages(context, schema, task=task, structured_output=False)
        elif task in PLANNING_TASKS:
            messages, schema = planning_messages(
                task,
                context,
                schema,
                structured_output=self.config["structured_output"] == "json_schema",
                schema_adapter=lambda value: strict_output_schema(self.config, value),
            )
            if task == "repair_plan" and "replacements" in schema.get("properties", {}):
                messages[0]["content"] += WIRE_INSTRUCTION
        else:
            if task == "work_plan" and "WorkTask" in schema.get("$defs", {}):
                context, schema, work_references = work_plan_payload(context, schema)
            if task == "extract":
                context = extraction_context(context)
            if task in BUSINESS_TASKS:
                context, schema, business_references = business_payload(context, schema)
            schema = extraction_schema(task, schema)
            schema = strict_output_schema(self.config, schema)
            system = prompt_for(
                task, {}, schema, has_writing_guide=bool(context.get("writing_guidance"))
            )
            # response_format remains enabled, but compatible providers may not
            # enforce it. The model also needs the complete compact wire schema.
            if task in BUSINESS_TASKS:
                system += WIRE_INSTRUCTIONS
            if task == "extract" and is_compact_extraction(schema):
                system += EXTRACTION_WIRE_INSTRUCTION
            messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                },
            ]
        if (
            self.config["structured_output"] == "json_schema"
            and task not in NATIVE_TOOL_TASKS
        ):
            messages[0]["content"] += STRICT_FIELDS_INSTRUCTION
        if task in {"review_layout", "verify_extraction", "verify_visual_extraction"} or (task in {"research_step", "style_analysis"} and context.get("page_images")):
            messages = image_messages(
                messages, context, supported=self.config.get("supports_image_input", False),
                preserve_user_text=task in {"research_step", "style_analysis"},
            )
        return messages, schema, references, work_references, business_references

    def request_tools(self, task, context, schema):
        if task not in NATIVE_TOOL_TASKS:
            return []
        tools = tool_definitions(task, context, schema)
        if not tools:
            raise ModelCallFailed("No native tools available for this task", "configuration")
        return tools

    async def complete(self, task, context, schema, session_key) -> RuntimeResult:
        if task == "score_layout":
            from folio.decisions.layout_scoring import score_layout

            # Final layout scoring is an explicit Jev image operation. It does
            # not enable all optional decision routing or fall back to the LLM.
            with model_activity_task(task):
                probe = await self._ensure_layout_vision()
                result = await score_layout(self.config, context)
                if probe is not None:
                    probe_usage = probe["usage"]
                    for key in ("decision_http_attempts", "decision_usage"):
                        result.usage[key] = probe_usage.get(key, []) + result.usage[key]
                    result.usage["decision_calls"] += probe_usage.get("decision_calls", 0)
                    result.usage["layout_vision_verified"] = True
                return result
        if task == "select_context":
            return await self.material_selector.complete(context, schema, session_key)
        if task in NATIVE_TOOL_TASKS:
            return await self._complete_llm(task, context, schema, session_key)
        if task == "intent" and context.get("intent_format_retry") is True:
            # A format correction needs the primary model, not the same optional
            # classifier again. Keep the Runtime protocol unchanged for callers.
            decision, fallback = None, "intent_format_retry"
        else:
            decision, fallback = await self.decision_router.try_complete(task, context, schema)
        if decision is not None:
            return decision
        return await self._complete_llm(task, context, schema, session_key, fallback=fallback)

    async def _ensure_layout_vision(self):
        """Verify native image forwarding once per runtime, including concurrency."""
        from folio.decisions.layout_scoring import probe_layout_vision

        async with self._layout_vision_lock:
            first = self._layout_vision_probe is None
            if first:
                self._layout_vision_probe = await probe_layout_vision(self.config)
            probe = self._layout_vision_probe
            if not probe["ok"]:
                error = ModelCallFailed("Jev 未通过图片能力验证，无法执行页面美观评分。", "configuration")
                error.diagnostics = {
                    "decision_http_attempts": probe.get("usage", {}).get("decision_http_attempts", [])
                }
                raise error
            return probe if first else None

    async def _complete_llm(self, task, context, schema, session_key, *, fallback=None):
        with model_activity_task(task):
            return await self._complete_llm_response(
                task, context, schema, session_key, fallback=fallback
            )

    async def _complete_llm_response(self, task, context, schema, session_key, *, fallback=None):
        result_schema = evidence_schema(schema, context) if task in BUSINESS_TASKS else schema
        messages, schema, references, work_references, business_references = self.request_parts(
            task, context, schema
        )
        tools = self.request_tools(task, context, schema)
        planning_output = self.output_tokens(task, context)
        capacity_checked = False
        if self.config.get("max_input_tokens") is not None or (
            task in ADAPTIVE_OUTPUT_TASKS and self.capacity.counter
        ):
            measured = await self.measure(
                task, context, result_schema, self.prompt_chars(task, context, result_schema)
            )
            if not measured["fits"]:
                raise ModelCallFailed("Request exceeds measured token capacity", "context_limit")
            planning_output = measured["output_tokens"]
            capacity_checked = True
        guard = RepeatedParagraphGuard() if task in {"generate", "repair"} else None
        received = ""
        async with aclosing(
            self.stream(
                messages,
                schema=schema,
                **({"tools": tools, "parallel_tool_calls": task in RESEARCH_TOOL_TASKS} if tools else {}),
                max_tokens=planning_output,
                capacity_checked=capacity_checked,
                **(
                    {"allow_context_retry": False}
                    if task
                    in {
                        "select_context",
                        "integrate_supplement",
                        "check_supplement_conflicts",
                        "repair_supplement_conflicts",
                    }
                    else {}
                ),
            )
        ) as events:
            async for kind, data in events:
                if kind == "text.delta":
                    if guard:
                        guard.feed(data["text"])
                    received += data["text"]
                    # Some constrained decoders keep padding a finished object
                    # until max_tokens. Only stop if the WHOLE response is one
                    # valid object satisfying the actual wire schema. Partial
                    # JSON, unfinished strings and missing fields still fail.
                    if not tools and len(received) - len(received.rstrip()) >= 512:
                        try:
                            value = json.loads(received)
                        except ValueError:
                            value = None
                        if isinstance(value, dict) and Draft202012Validator(schema).is_valid(value):
                            kind, data = (
                                "completed",
                                {
                                    "text": received.rstrip(),
                                    "usage": {
                                        "completion_termination": "validated_json_padding",
                                        "usage_incomplete": True,
                                    },
                                },
                            )
                        else:
                            from folio.runtime.output_guard import outside_json_string

                            if not outside_json_string(received):
                                continue
                            failure = ModelCallFailed(
                                "Structured response stalled in whitespace before schema completion",
                                "stalled_output",
                            )
                            failure.diagnostics = {
                                "received_chars": len(received),
                                "trailing_whitespace_chars": len(received) - len(received.rstrip()),
                            }
                            capture_partial_output(failure, received)
                            raise failure
                if kind == "completed":
                    text = data["text"]
                    try:
                        if tools:
                            calls = data.get("tool_calls")
                            if not calls:
                                raise ModelCallFailed("Missing native tool call", "protocol")
                            usage = data["usage"]
                            if fallback:
                                usage = usage | {"decision_fallback": fallback}
                            return RuntimeResult(text, usage=usage, tool_calls=calls, reasoning_content=data.get("reasoning_content"))
                        if task == "repair_plan" and "replacements" in schema.get("properties", {}):
                            from folio.runtime.base import parse_json

                            try:
                                value = parse_json(text)
                                if not Draft202012Validator(schema).is_valid(value):
                                    raise ValueError("Invalid typed directory repair response")
                                text = json.dumps(restore_repair(value), ensure_ascii=False)
                            except ValueError as exc:
                                # The planning owner can retry an invalid completed response;
                                # keep it separate from safe/public transport diagnostics.
                                exc.received_planning_response = {
                                    "content": data["text"], "usage": data.get("usage", {})
                                }
                                raise
                        if work_references is not None:
                            from folio.runtime.base import parse_json

                            text = json.dumps(
                                restore_work_plan(
                                    parse_json(text),
                                    work_references,
                                    context.get("fixed_work_tasks", []),
                                ),
                                ensure_ascii=False,
                            )
                        if task == "extract" and is_compact_extraction(schema):
                            from folio.runtime.base import parse_json

                            text = json.dumps(
                                restore_extraction(parse_json(text), schema), ensure_ascii=False
                            )
                        if business_references:
                            from folio.runtime.base import parse_json

                            text = json.dumps(
                                translate_ids(parse_json(text), business_references), ensure_ascii=False
                            )
                        if task in BUSINESS_TASKS:
                            from folio.runtime.base import parse_json

                            text = json.dumps(
                                restore_empty_fields(parse_json(text), result_schema),
                                ensure_ascii=False,
                            )
                        if task in {"generate", "repair"}:
                            from folio.runtime.base import parse_json

                            value = remove_duplicate_inline_citations(
                                restore_primary_table(task, context, parse_json(text)),
                                business_references,
                                [e["evidence_id"] for e in context.get("evidence", [])],
                            )
                            validate_paragraph_cycles(value)
                            text = json.dumps(value, ensure_ascii=False)
                        if task in {"read_gap_materials", "read_review_materials"}:
                            text = json.dumps(
                                restore_material_kinds(json.loads(text), context), ensure_ascii=False
                            )
                        usage = data["usage"]
                        if fallback:
                            usage = usage | {"decision_fallback": fallback}
                        return RuntimeResult(text, usage=usage)
                    except Exception as exc:
                        # Keep the original wire response for private failure audits,
                        # even if JSON decoding or reference restoration fails.
                        # This does not change validation, retries, or public errors.
                        capture_partial_output(exc, data["text"])
                        raise
        raise ModelCallFailed("Missing result", "protocol")

    def output_tokens(self, task, context=None):
        if task in PLANNING_TASKS:
            return self.config.get("planning_max_tokens") or self.config["max_tokens"]
        return self.config["max_tokens"]

    async def planning_retry_budget(self, task, context, schema, char_limit):
        """Grow only this planner instance's output when measured window space permits."""
        measured = await self.measure(task, context, schema, char_limit)
        current = self.output_tokens(task)
        target = current
        if measured["input_tokens"] is not None and measured["context_window"]:
            room = measured["context_window"] - measured["input_tokens"] - measured["margin_tokens"]
            ceiling = max(DEFAULT_OUTPUT_TOKEN_BUDGET, self.config["max_tokens"], current)
            target = max(current, min(current * 3 // 2, room, ceiling))
        if target > current:
            self.config["planning_max_tokens"] = target
        return {"previous_output_tokens": current, "retry_output_tokens": target, **measured}

    async def measure(self, task, context, schema, char_limit):
        if task == "score_layout":
            from folio.decisions.layout_scoring import layout_context_capacity

            return layout_context_capacity(self.config, context, char_limit)
        if task in {"review_layout", "verify_extraction", "verify_visual_extraction"} or (task in {"research_step", "style_analysis"} and context.get("page_images")):
            if self.config.get("max_input_tokens") is not None:
                return self.capacity.local_measure(task, context, schema, char_limit)
            # Serving text tokenizers often cannot count image inputs. Keep their
            # capability probes out of this explicitly enabled optional stage.
            messages, wire_schema, *_ = self.request_parts(task, context, schema)
            tools = self.request_tools(task, context, wire_schema)
            schema_reserve = tool_characters(tools) if tools else research_schema_chars(
                wire_schema, structured_output=self.config["structured_output"] == "json_schema"
            ) if task == "style_analysis" else 0
            chars = text_characters(messages) + schema_reserve
            reserved = len(context["page_images"]) * IMAGE_TOKEN_RESERVE
            output = self.output_tokens(task, context)
            window = self.capacity.window
            estimate = chars + reserved  # one token/character is conservative for this JSON.
            return {
                "characters": chars,
                "input_tokens": None,
                "estimated_input_tokens": estimate,
                "image_token_reserve": reserved,
                **({"schema_characters": schema_reserve, "schema_token_reserve": schema_reserve} if schema_reserve else {}),
                "output_tokens": output,
                "context_window": window,
                "measurement": "bounded_image_estimate",
                "fits": chars <= char_limit
                and (window is None or estimate + output + 512 <= window),
            }
        if task == "select_context":
            return await self.material_selector.measure(context, schema)
        return await self.capacity.measure(task, context, schema, char_limit)

    def fits_prompt_budget(self, task, context, schema, char_limit):
        """Preflight uses tokens when configured; measure() is the final check."""
        if task == "score_layout":
            from folio.decisions.layout_scoring import layout_context_capacity

            return layout_context_capacity(self.config, context, char_limit)["fits"]
        return self.capacity.local_measure(task, context, schema, char_limit)["fits"]

    def prompt_chars(self, task, context, schema):
        if task == "score_layout":
            from folio.decisions.layout_scoring import layout_context_capacity

            return layout_context_capacity(self.config, context, 600000)["characters"]
        messages, wire_schema, *_ = self.request_parts(task, context, schema)
        tools = self.request_tools(task, context, wire_schema)
        reserve = tool_characters(tools) if tools else research_schema_chars(
            wire_schema, structured_output=self.config["structured_output"] == "json_schema"
        ) if task == "style_analysis" else 0
        return text_characters(messages) + reserve

    async def aclose(self):
        await self.client.aclose()
