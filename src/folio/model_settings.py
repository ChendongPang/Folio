"""Workspace model settings shared by the local GUI and workers."""

import asyncio
import json
import logging
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from filelock import FileLock
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from folio.runtime.token_estimate import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    DEFAULT_OUTPUT_TOKEN_BUDGET,
)

SECRET_FIELDS = {"api_key", "decision_api_key", "tavily_api_key", "deepseek_api_key"}
CAPACITY_FIELDS = ("max_input_tokens", "context_window", "tokenizer_encoding")


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=3500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None  # Blank means retain an existing key at the same endpoint.
    no_auth: bool = False
    max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKEN_BUDGET, ge=256, strict=True)
    planning_max_tokens: int | None = Field(default=None, ge=256, strict=True)
    max_input_tokens: int = Field(default=DEFAULT_INPUT_TOKEN_BUDGET, gt=0, strict=True)
    context_window: int | None = Field(default=None, gt=0, strict=True)
    # Explicit local estimate for provider aliases, never a claim of exact serving tokenization.
    tokenizer_encoding: Literal["o200k_base", "cl100k_base"] | None = None
    temperature: float = Field(default=0.1, ge=0, le=2)
    enable_thinking: bool | None = None  # Optional Qwen/vLLM chat-template parameter.
    structured_output: Literal["prompt", "json_schema"] = "prompt"
    # Explicit operator-declared capability; a text connection probe cannot prove vision.
    supports_image_input: bool = False
    provider_mode: Literal["custom"] = "custom"
    decision_model: str = ""
    decision_base_url: str = ""
    decision_api_key: SecretStr | None = None
    decision_enabled: bool = False
    search_provider: Literal["tavily", "deepseek"] = "tavily"
    deepseek_api_key: SecretStr | None = None
    # Retain the persisted flag name; it controls whichever search provider is selected.
    tavily_enabled: bool = False
    tavily_api_key: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def retire_page_scoring(cls, value):
        # Older settings files and open browser tabs may still send this switch.
        # Accept it for compatibility without persisting or enabling the retired flow.
        if isinstance(value, dict):
            value = dict(value)
            value.pop("layout_review_enabled", None)
            value.pop("jev_api_key", None)
            if value.get("provider_mode", "custom") != "custom":
                value["provider_mode"] = "custom"
                value["enable_thinking"] = None
                value["decision_enabled"] = False
            return value
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        value = value.strip().rstrip("/")
        try:
            url = urlsplit(value)
        except ValueError:
            raise ValueError("接口地址无效，请输入完整 HTTP(S) Base URL") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() for c in value)
            or "${" in value
        ):
            raise ValueError("请输入完整 HTTP(S) Base URL，不含账号、查询参数或片段")
        try:
            _ = url.port
        except ValueError as exc:
            raise ValueError("接口端口无效") from exc
        if url.path.endswith(("/chat/completions", "/responses")):
            raise ValueError("请输入 Base URL，例如 http://host:8000/v1，不含具体生成端点")
        return value

    @field_validator("model")
    @classmethod
    def model_name(cls, value):
        value = value.strip()
        if not value or "${" in value or any(ord(c) < 32 for c in value):
            raise ValueError("请输入有效模型名称")
        return value

    @field_validator("api_key", "decision_api_key", "tavily_api_key", "deepseek_api_key")
    @classmethod
    def key(cls, value):
        if value is not None:
            secret = value.get_secret_value()
            if len(secret) > 4096 or "${" in secret or any(ord(c) < 32 for c in secret):
                raise ValueError("API Key 格式无效")
        return value

    @field_validator("max_input_tokens", mode="before")
    @classmethod
    def input_budget(cls, value):
        # Old settings stored null when switching models. Null now restores the
        # application budget; it must never reactivate a hidden character cap.
        # A smaller model window is enforced alongside this budget at runtime,
        # including output and the safety margin.
        return DEFAULT_INPUT_TOKEN_BUDGET if value is None else value

    @field_validator("decision_base_url")
    @classmethod
    def decision_endpoint(cls, value):
        return ModelSettingsInput.endpoint(value) if value.strip() else ""

    @field_validator("decision_model")
    @classmethod
    def decision_name(cls, value):
        return ModelSettingsInput.model_name(value) if value.strip() else ""


def decision_provider_name(data):
    from folio.decision_client import is_systemone_endpoint

    if not data.decision_enabled:
        return "llm"
    return "systemone" if is_systemone_endpoint(data.decision_base_url) else "openai"


class ModelProbeInput(ModelSettingsInput):
    """Optional services may be tested before an LLM endpoint is configured."""

    base_url: str = Field(default="", max_length=3500)
    model: str = Field(default="", max_length=200)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        return value.strip().rstrip("/")

    @field_validator("model")
    @classmethod
    def model_name(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def provider_models(self):
        return self


def probe_fingerprint(config: dict, component: str) -> str:
    if component == "llm":
        fields = (
            "provider_mode",
            "base_url",
            "model",
            "api_key",
            "no_auth",
            "max_tokens",
            "planning_max_tokens",
            "temperature",
            "enable_thinking",
            "structured_output",
            "supports_image_input",
        )
        payload = {key: config.get(key) for key in fields}
    elif component == "decision":
        from folio.decision_client import decision_target

        endpoint, model, key = decision_target(config)
        payload = {
            "provider_mode": config.get("provider_mode", "custom"),
            "endpoint": endpoint,
            "model": model,
            "key": key,
        }
    elif component == "tavily":
        payload = {
            "endpoint": "https://api.deepseek.com/v1/tools/search" if config.get("search_provider") == "deepseek" else "https://api.tavily.com/search",
            "key": config.get("deepseek_api_key" if config.get("search_provider") == "deepseek" else "tavily_api_key", ""),
        }
    else:
        raise ValueError("Unknown connection component")
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def feature_enabled(config: dict, component: str) -> bool:
    """Enable only the exact optional service settings tested before saving."""
    if component not in {"decision", "tavily"} or not config.get(f"{component}_enabled"):
        return False
    try:
        return config.get("verified_capabilities", {}).get(component) == probe_fingerprint(
            config, component
        )
    except (ValueError, TypeError, AttributeError):
        return False


class ModelSettingsStore:
    def __init__(self, root: Path):
        self.directory = root / "settings"
        self.path = self.directory / "model.json"
        self.probe_path = self.directory / "probe-state.json"
        self._migrate()

    def _migrate(self):
        from folio.migrations import import_model, legacy_model_path

        old = legacy_model_path(self.directory)
        if not old.is_file():
            return
        with FileLock(self.directory / "settings.lock"):
            if not old.is_file():
                return
            if not self.path.exists():
                config = import_model(old)
                validate_config(config)
                self._write(config)
            # Retain conflicting credentials until a user deliberately resaves.
            elif load_model_config(self.path) != validate_config(import_model(old)):
                raise ValueError("新旧模型配置冲突，请先核对配置后迁移。")
            old.unlink()

    def _write(self, config, target=None):
        fd, filename = tempfile.mkstemp(dir=self.directory, prefix=".model-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, target or self.path)
        finally:
            Path(filename).unlink(missing_ok=True)

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        config = json.loads(self.path.read_text(encoding="utf-8"))
        if config.get("provider_mode", "custom") != "custom":
            config["enable_thinking"] = None
        if config.get("provider_mode", "custom") != "custom" or "jev_api_key" in config:
            config["provider_mode"] = "custom"
            config["decision_enabled"] = False
            config["decision_provider"] = "llm"
            config.pop("jev_api_key", None)
        return config

    def public(self) -> dict:
        config = self._read()
        if not config:
            return {
                "configured": False,
                "has_api_key": False,
                "provider_mode": "custom",
                "decision_enabled": False,
                "decision_model": "",
                "decision_base_url": "",
                "search_provider": "tavily",
                "has_deepseek_api_key": False,
                "tavily_enabled": False,
                "supports_image_input": False,
                "has_decision_api_key": False,
                "has_tavily_api_key": False,
                **dict.fromkeys(CAPACITY_FIELDS),
                "max_input_tokens": DEFAULT_INPUT_TOKEN_BUDGET,
                "max_tokens": DEFAULT_OUTPUT_TOKEN_BUDGET,
                "planning_max_tokens": None,
                "test_status": {"llm": False, "decision": False, "tavily": False},
            }
        result = {
            "configured": True,
            "provider_mode": config.get("provider_mode", "custom"),
            "decision_provider": config.get("decision_provider", "llm"),
            "decision_model": config.get("decision_model", ""),
            "decision_base_url": config.get("decision_base_url", ""),
            "search_provider": config.get("search_provider", "tavily"),
            "has_deepseek_api_key": bool(config.get("deepseek_api_key")),
            "decision_enabled": feature_enabled(config, "decision"),
            "tavily_enabled": feature_enabled(config, "tavily"),
            "has_decision_api_key": bool(config.get("decision_api_key")),
            "has_tavily_api_key": bool(config.get("tavily_api_key")),
            "base_url": config["base_url"],
            "model": config["model"],
            "has_api_key": bool(config.get("api_key")),
            "no_auth": config["no_auth"],
            "max_tokens": config.get("max_tokens", DEFAULT_OUTPUT_TOKEN_BUDGET),
            "planning_max_tokens": config.get("planning_max_tokens"),
            **{name: config.get(name) for name in CAPACITY_FIELDS},
            "max_input_tokens": config.get("max_input_tokens") or DEFAULT_INPUT_TOKEN_BUDGET,
            "temperature": config["temperature"],
            "enable_thinking": config.get("enable_thinking"),
            "structured_output": config.get("structured_output", "prompt"),
            "supports_image_input": config.get("supports_image_input", False),
        }
        result["test_status"] = {
            component: self.probe_matches(config, component)
            for component in ("llm", "decision", "tavily")
        }
        return result

    def _probes(self):
        try:
            value = json.loads(self.probe_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def probe_matches(self, config, component):
        try:
            return self._probes().get(component) == probe_fingerprint(config, component)
        except (ValueError, TypeError, AttributeError):
            return False

    def record_probe(self, config, component, succeeded):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(self.directory / "settings.lock"):
            probes = self._probes()
            fingerprint = probe_fingerprint(config, component)
            if succeeded:
                probes[component] = fingerprint
            else:
                # A failed experiment with unsaved credentials must not revoke a
                # different, still-active configuration's successful connection.
                if probes.get(component) == fingerprint:
                    probes.pop(component, None)
                if component in {"decision", "tavily"}:
                    saved = self._read()
                    if saved and probe_fingerprint(saved, component) == fingerprint:
                        verified = dict(saved.get("verified_capabilities") or {})
                        verified.pop(component, None)
                        saved["verified_capabilities"] = verified
                        saved[f"{component}_enabled"] = False
                        if component == "decision":
                            saved["decision_provider"] = "llm"
                        self._write(saved)
            self._write(probes, self.probe_path)

    def resolve(self, data: ModelSettingsInput, *, component=None) -> dict:
        old = self._read()
        same_model = (
            old.get("base_url") == data.base_url
            and old.get("model") == data.model
            and old.get("provider_mode", "custom") == data.provider_mode
        )
        # Preserve omitted capacity settings for the same model, including
        # window/encoding values not exposed by the UI. Model changes clear them,
        # while an omitted/null input budget always resolves to the app default.
        values = data.model_dump()
        for name in CAPACITY_FIELDS:
            if name not in data.model_fields_set:
                values[name] = old.get(name) if same_model else None
        try:
            # LLM probes validate the full model settings; optional service
            # probes can still run before an LLM endpoint is configured.
            settings_model = ModelSettingsInput if component in {None, "llm"} else type(data)
            data = settings_model.model_validate(values)
        except ValidationError:
            raise ValueError("请检查模型接口地址、模型名称和高级参数。") from None
        key = data.api_key.get_secret_value().strip() if data.api_key else ""
        if data.no_auth:
            key = ""
        elif not key:
            matching = (
                old.get("base_url") == data.base_url
                and old.get("provider_mode", "custom") == data.provider_mode
            )
            if matching and old.get("api_key"):
                key = old["api_key"]
            elif component in {None, "llm"}:
                raise ValueError(
                    "请填写 API Key；免认证接口请勾选无需 API Key。更换地址需重新填写密钥。"
                )
        config = {
            "version": 1,
            "base_url": data.base_url,
            "model": data.model,
            "api_key": key,
            "no_auth": not bool(key),
            "max_tokens": data.max_tokens,
            "planning_max_tokens": data.planning_max_tokens,
            **{name: getattr(data, name) for name in CAPACITY_FIELDS},
            "temperature": data.temperature,
            "enable_thinking": data.enable_thinking,
            "structured_output": data.structured_output,
            "supports_image_input": data.supports_image_input,
            "provider_mode": data.provider_mode,
            "decision_model": data.decision_model,
            "decision_base_url": data.decision_base_url,
            "search_provider": data.search_provider,
            "decision_enabled": data.decision_enabled,
            "tavily_enabled": data.tavily_enabled,
            "decision_provider": decision_provider_name(data),
        }
        for name in ("decision_api_key", "tavily_api_key", "deepseek_api_key"):
            supplied = getattr(data, name)
            if name == "decision_api_key" and old.get("decision_base_url") != data.decision_base_url:
                old = {**old, "decision_api_key": ""}
            config[name] = (supplied.get_secret_value().strip() if supplied else "") or old.get(
                name, ""
            )
        if component == "decision" or data.decision_enabled:
            if not data.decision_base_url or not data.decision_model or not config["decision_api_key"]:
                raise ValueError("Configure the decision model URL, API key and model ID.")
        search_key = "deepseek_api_key" if data.search_provider == "deepseek" else "tavily_api_key"
        if component == "tavily" and not config[search_key]:
            raise ValueError("Enter the selected search provider's API key.")
        return config

    def save(self, data: ModelSettingsInput) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(self.directory / "settings.lock"):
            config = self.resolve(data)
            verified = {}
            for component, label in (("decision", "决策模型"), ("tavily", "联网搜索")):
                if config[f"{component}_enabled"]:
                    if not self.probe_matches(config, component):
                        raise ValueError(f"请先使用当前配置测试{label}连接，通过后才能启用。")
                    verified[component] = probe_fingerprint(config, component)
            config["verified_capabilities"] = verified
            self._write(config)
            return self.public()


def validate_config(config: dict) -> dict:
    if config.get("version", 1) != 1 or config.get("decision_provider", "llm") not in {
        "llm",
        "jev",
        "luna-decisions",
        "openai",
        "systemone",
    }:
        raise ValueError("Unsupported model configuration")
    data = ModelSettingsInput.model_validate(
        {
            key: value
            for key, value in config.items()
            if key not in {"version", "decision_provider", "verified_capabilities"}
        }
    )
    key = data.api_key.get_secret_value() if data.api_key else ""
    if not data.no_auth and not key:
        raise ValueError("Missing model API key")
    return {
        "version": 1,
        "decision_provider": decision_provider_name(data),
        **data.model_dump(exclude=SECRET_FIELDS),
        "api_key": "" if data.no_auth else key,
        "decision_api_key": data.decision_api_key.get_secret_value() if data.decision_api_key else "",
        "deepseek_api_key": data.deepseek_api_key.get_secret_value() if data.deepseek_api_key else "",
        "tavily_api_key": data.tavily_api_key.get_secret_value() if data.tavily_api_key else "",
        "verified_capabilities": {
            k: v
            for k, v in (config.get("verified_capabilities") or {}).items()
            if k in {"decision", "tavily"}
            and isinstance(v, str)
            and re.fullmatch(r"[0-9a-f]{64}", v)
        },
    }


def load_model_config(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        def resolve(value):
            if not isinstance(value, str):
                return value

            def env(match):
                name = match.group(1)
                if name not in os.environ:
                    raise ValueError("Missing model environment variable")
                return os.environ[name]

            return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}", env, value)

        return validate_config({key: resolve(value) for key, value in raw.items()})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        from folio.runtime.errors import ModelCallFailed

        raise ModelCallFailed("Invalid model configuration", "configuration") from exc


async def test_connection(config: dict) -> dict:
    """Verify the native tool protocol required by research, without side effects."""
    from folio.runtime.diagnostics import safe_failure_diagnostics
    from folio.runtime.errors import ModelCallFailed, public_model_error
    from folio.runtime.llm import LLMRuntime

    fd, filename = tempfile.mkstemp(suffix=".json")
    runtime = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            # A connection probe does not enable optional services or use their keys.
            json.dump(
                config
                | {
                    "decision_enabled": False,
                    "tavily_enabled": False,
                    "decision_provider": "llm",
                    "decision_api_key": "",
                    "tavily_api_key": "",
                },
                stream,
            )
        runtime = LLMRuntime(Path(filename))
        tools = [{
            "type": "function",
            "function": {
                "name": "confirm_connection",
                "description": "Confirm this connection test, which has no side effects.",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean", "enum": [True]}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                "strict": False,
            },
        }]
        completed = False
        async with asyncio.timeout(45):
            async for kind, data in runtime.stream(
                [{"role": "user", "content": "Connection test: call confirm_connection with ok set to true."}],
                max_tokens=runtime.config["max_tokens"], tools=tools,
            ):
                if kind == "completed":
                    calls = data.get("tool_calls", [])
                    args = json.loads(calls[0]["function"]["arguments"]) if len(calls) == 1 else {}
                    if set(args) != {"ok"} or args["ok"] is not True:
                        raise ModelCallFailed("Invalid native connection probe", "protocol")
                    completed = True
        if not completed:
            raise ModelCallFailed("Native connection probe did not complete", "protocol")
        return {
            "ok": True,
            "code": "native_tools_verified",
            "message": "连接测试通过，已验证模型支持原生工具调用。",
        }
    except Exception as exc:
        error = public_model_error(exc)
        result = {
            "ok": False,
            "code": error["code"],
            "message": error["text"],
            "diagnostics": safe_failure_diagnostics(exc),
        }
        if getattr(exc, "code", None) == "protocol":
            result.update(
                code="native_tools_required",
                message="未收到有效的原生工具调用（tool_calls）；当前接口或模型不满足资料研究要求。",
            )
        # Log only application-owned classifications and allowlisted numeric metrics.
        # Never include the exception body, request, URL, headers, or credentials.
        logging.getLogger(__name__).warning(
            "model_connection_test_failed code=%s diagnostics=%s",
            result["code"], json.dumps(result["diagnostics"], sort_keys=True),
        )
        return result
    finally:
        Path(filename).unlink(missing_ok=True)
        if runtime:
            await runtime.aclose()


async def test_decision_connection(config: dict) -> dict:
    from folio.decision_client import DecisionServiceError, evaluate

    try:
        async with asyncio.timeout(45):
            await evaluate(
                config,
                {"status": "ready"},
                {"ready": {"type": "noul", "instructions": "Does status equal ready?"}},
            )
        return {
            "ok": True,
            "code": "decision_verified",
            "message": "决策模型测试通过，已收到有效的结构化判断。",
        }
    except DecisionServiceError as exc:
        from folio.runtime.diagnostics import safe_failure_diagnostics

        diagnostics = safe_failure_diagnostics(exc)
        logging.getLogger(__name__).warning(
            "decision_connection_test_failed code=%s diagnostics=%s",
            exc.code, json.dumps(diagnostics, sort_keys=True),
        )
        return {"ok": False, "code": exc.code, "message": str(exc), "diagnostics": diagnostics}
    except TimeoutError:
        return {
            "ok": False,
            "code": "decision_timeout",
            "message": "决策服务响应超时，请稍后重试。",
        }
    except Exception:
        return {
            "ok": False,
            "code": "decision_test_failed",
            "message": "决策服务测试失败，请检查配置或稍后重试。",
        }


async def test_tavily_connection(config: dict) -> dict:
    if config.get("search_provider") == "deepseek":
        from folio.knowledge.search import DeepSeekKnowledge
        try:
            await DeepSeekKnowledge(config["deepseek_api_key"]).search("DeepSeek documentation", limit=1, max_chars=1000)
            return {"ok": True, "code": "deepseek_verified", "message": "DeepSeek Search connection verified."}
        except Exception:
            return {"ok": False, "code": "deepseek_failed", "message": "DeepSeek Search connection failed. Check the API key and network."}
    import httpx

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": "Bearer " + config["tavily_api_key"]},
                json={
                    "query": "Tavily documentation",
                    "search_depth": "basic",
                    "max_results": 1,
                    "include_answer": False,
                    "include_raw_content": False,
                },
            )
        if response.status_code in {401, 403}:
            return {
                "ok": False,
                "code": "search_auth_failed",
                "message": "Tavily 认证失败，请检查 API Key。",
            }
        if response.status_code != 200:
            return {
                "ok": False,
                "code": "search_request_failed",
                "message": "Tavily 测试失败，请检查服务额度或稍后重试。",
            }
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            return {
                "ok": False,
                "code": "search_invalid_response",
                "message": "Tavily 未返回有效的检索结果。",
            }
        return {
            "ok": True,
            "code": "search_verified",
            "message": "联网搜索测试通过，已收到 Tavily 检索响应。",
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "code": "search_timeout",
            "message": "Tavily 响应超时，请稍后重试。",
        }
    except Exception:
        return {
            "ok": False,
            "code": "search_connection_failed",
            "message": "无法连接 Tavily，请检查 API Key 和网络。",
        }
