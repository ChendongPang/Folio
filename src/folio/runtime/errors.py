"""Public model failures without provider bodies or credentials."""

import json
import re

import httpx

from folio.runtime.diagnostics import (
    PROTOCOL_MESSAGES,
    SAFE_SCHEMA_KEYWORDS,
    safe_failure_diagnostics,
)


def capacity_limit_detail(capacity: dict | None) -> str | None:
    """Describe numeric request limits without confusing tokens with characters."""
    if not capacity:
        return None

    def number(name):
        value = capacity.get(name)
        return value if type(value) is int and value >= 0 else None

    tokens = number("input_tokens")
    label = "实测" if tokens is not None else "估算"
    if tokens is None:
        tokens = number("estimated_input_tokens")
    if tokens is None:
        return None
    parts = [f"输入{label} {tokens:,} Token"]
    limit, window = number("max_input_tokens"), number("context_window")
    output, margin = number("output_tokens"), number("margin_tokens")
    if limit is not None:
        parts.append(f"输入预算 {limit:,} Token")
    if output is not None:
        parts.append(f"预留输出 {output:,} Token")
    if margin is not None:
        parts.append(f"安全余量 {margin:,} Token")
    if window is not None:
        parts.append(f"模型上下文窗口 {window:,} Token")
    reasons = []
    if limit is not None and tokens > limit:
        reasons.append("输入超过预算")
    if window is not None and output is not None and margin is not None:
        total = tokens + output + margin
        if total > window:
            reasons.append(f"输入、输出与余量合计 {total:,} Token，超过模型上下文窗口")
    return "，".join(parts) + ("；" + "；".join(reasons) if reasons else "")


class PlanningContextTooLarge(ValueError):
    def __init__(self, characters: int, limit: int, *, capacity: dict | None = None):
        detail = capacity_limit_detail(capacity)
        if detail is not None:
            self.public_message = (
                f"目录规划请求超过当前容量限制：{detail}。"
                "尚未发送生成请求，材料未截断。请核对输入预算和模型上下文容量后重试。"
            )
        else:
            self.public_message = (
                f"完整目录规划请求共 {characters:,} 字符，超过当前目录规划预算 {limit:,} 字符；"
                "尚未调用模型，材料未截断或分批。请调整 max_planning_prompt_chars 后重试。"
            )
        super().__init__(self.public_message)


class ModelCallFailed(RuntimeError):
    def __init__(self, message="模型调用失败", code="model_error"):
        super().__init__(message)
        self.code = code


def protocol_failure(issue, *, code="protocol", **diagnostics):
    failure = ModelCallFailed(PROTOCOL_MESSAGES[issue], code)
    failure.diagnostics = {"protocol_issue": issue, **diagnostics}
    return failure


class ModelCallBudgetExceeded(RuntimeError):
    """The application's durable call allowance, not an upstream provider error."""

    code = "call_budget"

    def __init__(self, used: int, limit: int):
        self.used, self.limit = used, limit
        super().__init__(f"Job model-call budget exhausted ({used}/{limit})")


class TaskRetriesExhausted(RuntimeError):
    def __init__(self, task_key: str, last_error: str | None):
        self.task_key = task_key
        self.last_error = last_error
        super().__init__(
            f"Task retries exhausted: {task_key}; create a new job after correcting the cause"
        )


class ModelOutputRepetition(ModelCallFailed):
    def __init__(self):
        super().__init__(
            "Model repeated the same paragraph or paragraph sequence three times consecutively",
            "repetition",
        )


class ModelContextLimit(ModelCallFailed):
    """Keep only numerical context diagnostics, never the upstream error body."""

    def __init__(self, provider_message):
        window = re.search(r"maximum context length is ([\d,]+) tokens", provider_message)
        prompt = re.search(r"prompt contains (?:at least )?([\d,]+) input tokens", provider_message)
        self.window = int(window[1].replace(",", "")) if window else None
        self.input_tokens = int(prompt[1].replace(",", "")) if prompt else None
        super().__init__(
            f"Model context limit (window={self.window}, input_tokens>={self.input_tokens})",
            "context_limit",
        )

    def reduced_output(self, requested):
        if self.window is None or self.input_tokens is None:
            return None
        # Some services stop counting once the input allowance is exceeded.
        # Leave headroom rather than treating that lower bound as exact, while
        # retaining most of the output budget for structured results.
        reduced = min(requested * 3 // 4, self.window - self.input_tokens - 256)
        return reduced if 512 <= reduced < requested else None


def _provider_errors(body):
    """Inspect bounded error envelopes only; their contents must never escape this module."""
    pending = [(body, 0)]
    for _ in range(12):
        if not pending:
            return
        value, depth = pending.pop(0)
        if depth > 4:
            continue
        if isinstance(value, str):
            if len(value) > 64_000:
                continue
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                continue
        if not isinstance(value, dict):
            continue
        yield value
        for key in ("error", "metadata", "raw"):
            child = value.get(key)
            if isinstance(child, (dict, str)):
                pending.append((child, depth + 1))


def model_http_error(body, status_code: int) -> ModelCallFailed:
    """Classify HTTP rejections without retaining provider text, keys or schema field names."""
    code = {
        400: "invalid_request",
        401: "authentication",
        403: "authentication",
        402: "billing",
        404: "endpoint_not_found",
        429: "rate_limit",
    }.get(status_code, "model_error")
    failure = ModelCallFailed(f"Model HTTP {status_code}", code)
    failure.diagnostics = {"status_code": status_code}
    if status_code != 400:
        return failure

    schema_rejected = False
    required_properties = False
    tool_choice_rejected = False
    for error in _provider_errors(body):
        message = error.get("message")
        message = message[:64_000] if isinstance(message, str) else ""
        lowered = message.lower()
        provider_codes = (error.get("code"), error.get("provider_error_code"))
        if "context_length_exceeded" in provider_codes or "maximum context length" in lowered:
            failure = ModelContextLimit(message)
            failure.diagnostics = {"status_code": status_code}
            return failure
        if "invalid_json_schema" in provider_codes or (
            "invalid schema" in lowered
            and ("response_format" in lowered or "text.format.schema" in lowered)
        ):
            schema_rejected = True
        if "required" in lowered and "including every key in properties" in lowered:
            required_properties = True
        # Classify the incompatibility without retaining arbitrary provider text.
        # A reference to these settings alone is not evidence of a rejection.
        if (
            any(term in lowered for term in ("tool_choice", "tool choice", "forced tool"))
            and any(term in lowered for term in ("thinking", "reasoning"))
            and any(term in lowered for term in (
                "not supported", "does not support", "unsupported", "incompatible",
                "not allowed", "cannot be used", "can't be used",
            ))
        ):
            tool_choice_rejected = True
    if schema_rejected:
        failure = ModelCallFailed("Model interface rejected JSON Schema", "schema_unsupported")
        failure.diagnostics = {
            "status_code": status_code,
            "backend_error": "invalid_json_schema",
        }
        if required_properties:
            failure.diagnostics["schema_constraint"] = "required_all_properties"
    elif tool_choice_rejected:
        failure = ModelCallFailed(
            "Model reasoning mode rejects forced tool choice", "tool_choice_unsupported"
        )
        failure.diagnostics = {"status_code": status_code}
    return failure


def model_stream_error(error) -> ModelCallFailed:
    """Describe known backend failures without storing their arbitrary error body."""
    message = error.get("message", "") if isinstance(error, dict) else error
    message = message if isinstance(message, str) else ""
    if "grammar error" in message.lower() or "unimplemented keys:" in message.lower():
        # Only JSON Schema keywords are safe to quote. An upstream error may
        # also echo a prompt, URL, key, or a user-defined property name.
        match = re.search(r"Unimplemented keys:\s*\[([^\]]*)\]", message, re.IGNORECASE)
        unsupported = (
            sorted(set(re.findall(r'["\']([^"\']+)["\']', match[1])) & SAFE_SCHEMA_KEYWORDS)
            if match
            else []
        )
        detail = ": " + ", ".join(unsupported) if unsupported else ""
        failure = ModelCallFailed(
            "Model backend grammar rejected JSON Schema" + detail, "schema_unsupported"
        )
        failure.diagnostics = {
            "backend_error": "grammar",
            "unsupported_schema_keywords": unsupported,
        }
        return failure
    backend_type = error.get("type") if isinstance(error, dict) else None
    safe_types = {
        "BadRequestError",
        "ValidationError",
        "ValueError",
        "RuntimeError",
        "InternalServerError",
    }
    detail = (
        f" (backend_type={backend_type})"
        if isinstance(backend_type, str) and backend_type in safe_types
        else ""
    )
    failure = protocol_failure("upstream_stream_error")
    failure.args = ("Invalid model response" + detail,)
    return failure


def public_model_error(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, PlanningContextTooLarge):
        return {"code": "directory_context_limit", "text": exc.public_message}
    code = getattr(exc, "code", "model_error")
    # Compatibility for application/test exceptions; never return their raw text.
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        code = "timeout"
    elif code == "model_error" and ("401" in str(exc) or "authentication" in str(exc).lower()):
        code = "authentication"
    details = safe_failure_diagnostics(exc)
    if code == "schema_unsupported" and details.get("backend_error") == "invalid_json_schema":
        detail = (
            "对象字段未全部声明为必填。"
            if details.get("schema_constraint") == "required_all_properties"
            else "格式约束与接口要求不兼容。"
        )
        return {
            "code": code,
            "text": "模型接口拒绝了本次结构化输出 Schema："
            + detail
            + "需要修正结构化输出兼容性后再试。",
        }
    if code == "timeout" and details:
        phase = details.get("phase")
        if details.get("exception") == "ConnectTimeout":
            text = "连接模型接口超时，尚未收到模型响应。请检查服务入口和网络后重试。"
        elif phase == "streaming_content":
            text = "模型返回正文过程中超时，结果不完整，本轮结果未采用。"
        elif phase == "first_content":
            text = "模型接口已响应，但等待正文超时，本轮结果未采用。"
        else:
            text = "等待模型接口响应超时，尚未收到完整结果。"
        return {"code": code, "text": text}
    texts = {
        "call_budget": "本任务的模型调用次数已达到上限，已停止新增调用并保留已有内容和未完成检查。",
        "decision_unavailable": "资料筛选的判断模型未返回有效结果，本次未切换到主模型。请检查判断模型服务后重试。",
        "selection_context_limit": "资料筛选的共同问题或指令超过单次32,000字符预算，无法安全拆分，请缩小当前问题范围后重试。",
        "context_limit": "模型服务拒绝了请求：输入与预留输出超过服务的上下文上限。请调整模型部署上下文或输出预算后重试。",
        "authentication": "模型认证失败，请在模型设置中检查 API Key。",
        "billing": "模型服务返回余额或计费限制，本次已停止调用。请检查该服务账户。",
        "rate_limit": "模型接口限流或额度不足，请稍后重试。",
        "invalid_request": "模型服务拒绝了请求参数（HTTP 400）。请检查模型名称、输出 Token 上限及接口支持的参数。",
        "endpoint_not_found": "模型服务返回 HTTP 404。请检查接口地址及模型名称是否正确、是否有访问权限。",
        "tool_choice_unsupported": "当前模型的思考模式不支持强制工具调用（tool_choice）。请关闭该模型的思考模式，或选择支持此工具调用方式的模型。",
        "timeout": "模型响应超时，请稍后重试。",
        "configuration": "模型配置无法读取，请检查模型设置。",
        "length_limit": "模型输出不完整，请减少本次内容或调整输出长度。",
        "repetition": "模型连续重复输出相同正文，本次结果未采用。请重试。",
        "protocol": "模型响应格式不兼容，请检查接口配置。",
        "schema_unsupported": "模型服务不支持本次结构化输出约束，结果未采用；请调整兼容性后重试。",
        "model_error": "模型未正常完成回答，请检查连接或稍后重试。",
    }
    return {"code": code, "text": texts.get(code, texts["model_error"])}
