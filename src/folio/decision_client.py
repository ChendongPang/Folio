"""Decision requests over OpenAI-compatible or native System One endpoints."""

import asyncio
import json
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class DecisionServiceError(RuntimeError):
    """Only fixed, non-sensitive messages cross the service boundary."""

    def __init__(self, message, code="protocol", *, retryable=None, retry_after=0):
        super().__init__(message)
        self.code = code
        self.retryable = (
            code in {"rate_limit", "upstream", "timeout", "connection"}
            if retryable is None
            else retryable
        )
        self.retry_after = retry_after
        self.diagnostics = {}


_HTTP_AUDIT = ContextVar("decision_http_audit", default=None)
_HTTP_ATTEMPT = ContextVar("decision_http_attempt", default=1)


@contextmanager
def collect_http_attempts(attempts):
    token = _HTTP_AUDIT.set(attempts)
    try:
        yield
    finally:
        _HTTP_AUDIT.reset(token)


@contextmanager
def http_attempt_number(attempt):
    token = _HTTP_ATTEMPT.set(attempt)
    try:
        yield
    finally:
        _HTTP_ATTEMPT.reset(token)


def _retry_after(response):
    """Honor Retry-After without retaining its arbitrary header text."""
    value = response.headers.get("retry-after", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0.0, seconds) if math.isfinite(seconds) else 0


def is_systemone_endpoint(endpoint: str) -> bool:
    """An explicit System One endpoint opts into its native decision protocol."""
    return urlsplit(endpoint).path.rstrip("/").endswith("/systemone")


def decision_target(config: dict) -> tuple[str, str, str]:
    base = config.get("decision_base_url", "").rstrip("/")
    endpoint = base if is_systemone_endpoint(base) else base + "/chat/completions" if base else ""
    return endpoint, config.get("decision_model", ""), config.get("decision_api_key", "")


def answer_schema(questions):
    properties = {}
    for name, question in questions.items():
        kind = question["type"]
        fields = {"type": {"type": "string", "enum": [kind]}}
        probability = {"type": "number", "minimum": 0, "maximum": 1}
        if kind == "noul":
            fields["noul"] = probability
        else:
            choices = list(question.get("criteria", {}))
            if kind == "score":
                choices = [str(i) for i in range(len(choices))]
                fields["score"] = {"type": "number", "minimum": 0, "maximum": len(choices) - 1}
            else:
                fields["choice"] = {"type": "string", "enum": choices}
            fields["confidence"] = probability
            fields["probabilities"] = {"type": "object", "properties": {key: probability for key in choices}, "required": choices, "additionalProperties": False}
        properties[name] = {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    return {"type": "object", "properties": {"answers": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}, "required": ["answers"], "additionalProperties": False}


def _number(value, low=0, high=1):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def _valid_answers(data, questions):
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return False
    answers = data["answers"]
    if set(answers) != set(questions):
        return False
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question.get("type"):
            return False
        kind = question["type"]
        if kind == "noul":
            if not _number(answer.get("noul")):
                return False
            continue
        if kind not in {"choice", "score"} or not _number(answer.get("confidence")):
            return False
        probabilities = answer.get("probabilities")
        choices = question.get("criteria", {})
        if kind == "score":
            choices = {str(i) for i in range(len(choices))}
            if not _number(answer.get("score"), 0, len(choices) - 1):
                return False
        elif answer.get("choice") not in choices:
            return False
        if (
            not isinstance(probabilities, dict)
            or set(probabilities) != set(choices)
            or any(not _number(value) for value in probabilities.values())
            or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.03)
        ):
            return False
    return True


def decision_payload(model, state, questions, assets=None, *, native=False):
    """Build a standard request shared by runtime and offline probe scripts."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Evaluate the supplied questions against the state. Call submit_decisions once with all answers. For noul, return a probability from 0 to 1. For choice/score, include confidence and probabilities summing to 1. Follow the interface language for explanations; preserve identifiers."},
            {"role": "user", "content": json.dumps({"state": state, "questions": questions}, ensure_ascii=False)},
        ],
        "tools": [{"type": "function", "function": {"name": "submit_decisions", "description": "Submit all judgments.", "parameters": answer_schema(questions)}}],
        "tool_choice": {"type": "function", "function": {"name": "submit_decisions"}},
        "stream": False,
    }
    if assets is not None:
        if not isinstance(assets, dict) or not assets:
            raise DecisionServiceError("Invalid decision image assets.", "configuration")
        content = [{"type": "text", "text": payload["messages"][-1]["content"]}]
        for identity, asset in assets.items():
            if not isinstance(asset, dict) or asset.get("type") != "image" or asset.get("format") not in {"png", "jpeg", "webp"} or not isinstance(asset.get("data_base64"), str):
                raise DecisionServiceError("Invalid decision image asset.", "configuration")
            content.extend([
                {"type": "text", "text": "Image identifier: " + identity},
                {"type": "image_url", "image_url": {"url": "data:image/" + asset["format"] + ";base64," + asset["data_base64"]}},
            ])
        payload["messages"][-1]["content"] = content
    if native:
        return {"model": model, "state": state, "questions": questions, **({"assets": assets} if assets is not None else {})}
    return payload


async def evaluate(config: dict, state, questions: dict, *, timeout=30.0, assets=None) -> dict:
    """Return validated answers, usage and actual model without exposing raw errors."""
    if not questions or not isinstance(questions, dict):
        raise DecisionServiceError("决策问题不能为空。", "configuration")
    try:
        endpoint, model, key = decision_target(config)
    except ValueError:
        raise DecisionServiceError("决策服务配置无效。", "configuration") from None
    if not endpoint or not model or not key:
        raise DecisionServiceError("请先配置决策服务。", "configuration")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    native = is_systemone_endpoint(endpoint)
    payload = decision_payload(model, state, questions, assets, native=native)
    if native and assets is not None:
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    outer_attempts = _HTTP_AUDIT.get()
    attempts = []
    try:
        with collect_http_attempts(attempts):
            try:
                return await _evaluate_request(endpoint, model, headers, payload, questions, timeout)
            except DecisionServiceError as exc:
                if native or exc.code != "tool_choice_unsupported":
                    raise
            # Some reasoning models accept tools but reject forcing a named
            # function. Keep the same schema and validate the native tool call.
            payload["tool_choice"] = "auto"
            with http_attempt_number(_HTTP_ATTEMPT.get() + 1):
                return await _evaluate_request(endpoint, model, headers, payload, questions, timeout)
    except DecisionServiceError as exc:
        exc.diagnostics["decision_http_attempts"] = attempts
        raise
    finally:
        if outer_attempts is not None:
            outer_attempts.extend(attempts)


async def _evaluate_request(endpoint, model, headers, payload, questions, timeout):
    """Execute one accounted HTTP request, including compatibility retries."""
    entry = {
        "request_id": uuid4().hex,
        "attempt": _HTTP_ATTEMPT.get(),
        "outcome": "protocol",
        "retryable": False,
    }
    started = None
    error = None
    try:
        from folio.runtime.interaction_audit import provider_attempt

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            with provider_attempt("decision", "evaluate", model=model) as audit:
                try:
                    started = time.monotonic()
                    entry["started_at"] = datetime.now(UTC).isoformat(timespec="milliseconds")
                    response = await client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    entry["status_code"] = audit["status_code"] = response.status_code
                    if response.status_code in {401, 403}:
                        raise DecisionServiceError(
                            "决策服务认证失败，请检查 API Key。", "authentication"
                        )
                    if response.status_code == 402:
                        raise DecisionServiceError(
                            "决策服务余额不足，请检查服务账户额度。", "billing"
                        )
                    if response.status_code == 429:
                        raise DecisionServiceError(
                            "决策服务请求过于频繁，请稍后重试。",
                            "rate_limit",
                            retry_after=_retry_after(response),
                        )
                    if response.status_code == 404:
                        raise DecisionServiceError(
                            "判断服务接口或模型不存在，请检查接口地址和模型名称。Jev 原生接口需填写完整 /v1/systemone 地址。",
                            "endpoint_not_found", retryable=False,
                        )
                    if response.status_code != 200:
                        if response.status_code == 400:
                            from folio.runtime.errors import model_http_error

                            try:
                                body = response.json()
                            except ValueError:
                                body = None
                            if model_http_error(body, 400).code == "tool_choice_unsupported":
                                raise DecisionServiceError(
                                    "Decision model reasoning mode rejects forced tool choice.",
                                    "tool_choice_unsupported",
                                )
                        raise DecisionServiceError(
                            "决策服务调用失败，请检查服务配置或稍后重试。",
                            "upstream",
                            retryable=response.status_code in {408, 425}
                            or response.status_code >= 500,
                            retry_after=_retry_after(response),
                        )
                    envelope = response.json()
                    if is_systemone_endpoint(endpoint):
                        result = envelope
                    else:
                        choices = envelope.get("choices", [])
                        calls = choices[0].get("message", {}).get("tool_calls", []) if choices else []
                        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "submit_decisions":
                            raise DecisionServiceError("Decision model did not return submit_decisions.")
                        result = json.loads(calls[0]["function"]["arguments"])
                    if not isinstance(result, dict):
                        raise DecisionServiceError("Invalid decision response.")
                    result["usage"] = envelope.get("usage", {})
                    result["model"] = envelope.get("model", model)
                    if not _valid_answers(result, questions):
                        raise DecisionServiceError("决策服务未返回有效的完整判断。")
                    usage = result.get("usage", {})
                    if isinstance(usage, dict):
                        audit["prompt_tokens"] = usage.get(
                            "prompt_tokens", usage.get("input_tokens")
                        )
                        audit["completion_tokens"] = usage.get(
                            "completion_tokens", usage.get("output_tokens")
                        )
                        audit["total_tokens"] = usage.get("total_tokens")
                    entry["outcome"] = "success"
                    return {
                        "answers": result["answers"],
                        "usage": result.get("usage", {}),
                        "model": result.get("model", model),
                    }
                except DecisionServiceError as exc:
                    audit["error_code"] = exc.code
                    raise
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    audit["error_code"] = "protocol"
                    raise DecisionServiceError("决策服务未返回有效的完整判断。") from None
    except DecisionServiceError as exc:
        error = exc
        raise
    except httpx.TimeoutException:
        error = DecisionServiceError("决策服务响应超时，请稍后重试。", "timeout")
        raise error from None
    except httpx.HTTPError:
        error = DecisionServiceError("无法连接决策服务，请检查网络和接口地址。", "connection")
        raise error from None
    except asyncio.CancelledError:
        entry["outcome"] = "cancelled"
        raise
    finally:
        if started is not None:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            if error is not None:
                entry["outcome"] = error.code
                entry["retryable"] = error.retryable
                error.diagnostics = {"decision_http_attempts": [entry]}
                if "status_code" in entry:
                    error.diagnostics["status_code"] = entry["status_code"]
            attempts = _HTTP_AUDIT.get()
            if attempts is not None:
                attempts.append(entry)
