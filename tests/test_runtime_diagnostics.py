import asyncio
import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.diagnostics import (
    MAX_CAUSE_DEPTH,
    MAX_PARTIAL_OUTPUT_CHARS,
    capture_partial_output,
    partial_output_for_audit,
    safe_failure_diagnostics,
)
from folio.runtime.errors import ModelCallFailed, public_model_error
from folio.runtime.llm import LLMRuntime


def test_safe_failure_metrics_follow_bounded_causes_without_arbitrary_fields():
    inner = ModelCallFailed("PRIVATE_PROVIDER_BODY", "timeout")
    inner.diagnostics = {
        "exception": "ReadTimeout",
        "phase": "streaming_content",
        "received_chars": 123,
        "elapsed_seconds": 4.5,
        "first_content_seconds": 1.2,
        "last_event_seconds": float("inf"),
        "last_content_seconds": float("nan"),
        "reasoning_chars": True,
        "completion_tokens": "SECRET_KEY",
        "status_code": 429,
        "unsupported_schema_keywords": ["uniqueItems", "SECRET_KEY", {}, "uniqueItems"],
        "provider_body": "PRIVATE_PROVIDER_BODY",
        "base_url": "PRIVATE_URL",
    }
    outer = TimeoutError()
    outer.__cause__ = inner
    inner.__cause__ = outer  # Cycles must not trap failure handling.
    assert safe_failure_diagnostics(outer) == {
        "exception": "ReadTimeout",
        "phase": "streaming_content",
        "received_chars": 123,
        "elapsed_seconds": 4.5,
        "first_content_seconds": 1.2,
        "status_code": 429,
        "unsupported_schema_keywords": ["uniqueItems"],
    }
    assert "正文过程中超时" in public_model_error(outer)["text"]
    assert "PRIVATE" not in json.dumps(public_model_error(outer))


def test_failure_cause_depth_and_private_output_type_are_bounded():
    first = last = RuntimeError()
    for _ in range(MAX_CAUSE_DEPTH):
        child = RuntimeError()
        last.__cause__ = child
        last = child
    last.diagnostics = {"received_chars": 7}
    capture_partial_output(last, "must remain out of reach")
    assert safe_failure_diagnostics(first) == {}
    assert partial_output_for_audit(first) is None
    first._partial_model_output = {"content_head": "untrusted provider body"}
    assert partial_output_for_audit(first) is None


def test_decision_http_diagnostics_keep_safe_complete_rows_only():
    clean = {
        "request_id": "a" * 32,
        "attempt": 2,
        "started_at": "2026-10-12T02:29:40.123+00:00",
        "duration_ms": 1208.11149,
        "status_code": 503,
        "outcome": "upstream",
        "retryable": True,
    }
    failure = ModelCallFailed("PRIVATE_PROVIDER_BODY", "upstream")
    failure.diagnostics = {
        "decision_http_attempts": [
            clean | {"response_body": "PRIVATE_PROVIDER_BODY", "headers": {"key": "PRIVATE_KEY"}},
            clean,  # Do not inflate counts by repeated identities in wrapped exceptions.
            clean | {"request_id": "PRIVATE_KEY"},
            clean | {"request_id": "b" * 32, "attempt": True},
            clean | {"request_id": "c" * 32, "duration_ms": float("nan")},
            clean | {"request_id": "d" * 32, "outcome": "PRIVATE_PROVIDER_BODY"},
            clean | {"request_id": "e" * 32, "retryable": "PRIVATE_KEY"},
            clean | {"request_id": "f" * 32, "started_at": "2026-02-31T02:29:40+00:00"},
            clean | {"request_id": "1" * 32, "started_at": "2026-10-12T02:29:40 PRIVATE_KEY"},
            "PRIVATE_BODY",
            {},
        ]
    }
    assert safe_failure_diagnostics(failure) == {
        "decision_http_attempts": [clean | {"duration_ms": 1208.111}]
    }
    assert "PRIVATE" not in json.dumps(safe_failure_diagnostics(failure))
    # No HTTP on a circuit skip; the old provider failure is not a new request.
    skipped = ModelCallFailed("Unavailable", "upstream")
    skipped.diagnostics = {"decision_http_attempts": []}
    skipped.__cause__ = failure
    assert safe_failure_diagnostics(skipped) == {"decision_http_attempts": []}


@pytest.mark.parametrize("invalid", [True, 99, 600, 200.0, "PRIVATE_KEY"])
def test_decision_attempt_http_status_requires_an_integer_in_the_http_range(invalid):
    entry = {
        "request_id": "a" * 32,
        "attempt": 1,
        "started_at": "2026-10-12T02:29:40+00:00",
        "duration_ms": 1,
        "status_code": invalid,
        "outcome": "connection",
        "retryable": True,
    }
    failure = ModelCallFailed()
    failure.diagnostics = {"decision_http_attempts": [entry]}
    safe = safe_failure_diagnostics(failure)["decision_http_attempts"][0]
    assert "status_code" not in safe


def make_runtime(tmp_path, handler):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    # The old client has made no requests and owns no active connections.
    original = runtime.client
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return runtime, original


async def test_transport_restoration_error_retains_original_unmodified_response(tmp_path, monkeypatch):
    raw = '{"section_id":"sfref1","note":"PRIVATE_WIRE_CONTENT"}'
    body = b"".join([
        event({"choices": [{"delta": {"content": raw}}]}),
        event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    ])
    runtime, original = make_runtime(
        tmp_path,
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body),
    )
    await original.aclose()
    runtime.capacity.ready = True
    error = ValueError("Invalid business transport")

    def reject(value, schema):
        assert value["section_id"] == "original-section-id"
        raise error

    monkeypatch.setattr("folio.runtime.llm.restore_empty_fields", reject)
    try:
        with pytest.raises(ValueError) as caught:
            await runtime.complete("complete_gaps", {"section_id": "original-section-id"}, {"type": "object"}, "test")
        assert caught.value is error
        assert partial_output_for_audit(error)["content_head"] == raw
        assert "PRIVATE" not in str(error) + json.dumps(safe_failure_diagnostics(error))
    finally:
        await runtime.aclose()


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


async def test_length_failure_keeps_bounded_assistant_output_only_in_private_audit(tmp_path):
    content = "HEAD正文" + "中文业务规则；" * 6000 + "TAIL结尾"
    assert len(content) > MAX_PARTIAL_OUTPUT_CHARS
    body = b"".join(
        [
            event({"choices": [{"delta": {"content": content, "reasoning": "PRIVATE_REASONING"}}]}),
            event(
                {
                    "choices": [{"delta": {}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 8192},
                }
            ),
        ]
    )
    runtime, original = make_runtime(
        tmp_path,
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body),
    )
    await original.aclose()
    try:
        with pytest.raises(ModelCallFailed) as caught:
            async for _ in runtime.stream([{"role": "user", "content": "PRIVATE_INPUT"}]):
                pass
        failure = caught.value
        assert failure.code == "length_limit"
        safe = safe_failure_diagnostics(failure)
        assert safe["received_chars"] == len(content)
        assert safe["completion_tokens"] == 8192
        assert safe["finish_reason"] == "length"
        assert safe["first_content_seconds"] >= 0
        assert safe["last_content_seconds"] <= safe["elapsed_seconds"]
        audit = partial_output_for_audit(failure)
        assert audit == {
            "content_head": content[: MAX_PARTIAL_OUTPUT_CHARS // 2],
            "content_tail": content[-MAX_PARTIAL_OUTPUT_CHARS // 2 :],
            "received_chars": len(content),
            "retained_chars": MAX_PARTIAL_OUTPUT_CHARS,
            "truncated": True,
        }
        public = (
            str(failure)
            + repr(failure)
            + json.dumps(safe)
            + json.dumps(public_model_error(failure))
        )
        assert "HEAD正文" not in public and "PRIVATE" not in public
        assert "PRIVATE" not in json.dumps(audit)
    finally:
        await runtime.aclose()


async def test_read_timeout_audit_excludes_reasoning_provider_body_and_request(tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "实际模型正文",
                                "reasoning_content": "SECRET_REASONING",
                            }
                        }
                    ]
                }
            )
            raise httpx.ReadTimeout("SECRET_PROVIDER_KEY_AND_BODY")

    runtime, original = make_runtime(
        tmp_path,
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body()),
    )
    runtime.capacity.ready = True  # Isolate response-body failure from tokenizer probes.
    await original.aclose()
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await runtime.complete(
                "validate", {"private_request": "SECRET_INPUT"}, {"type": "object"}, "test"
            )
        failure = caught.value
        assert failure.code == "timeout"
        safe = safe_failure_diagnostics(failure)
        assert safe["exception"] == "ReadTimeout"
        assert safe["reasoning_chars"] == len("SECRET_REASONING")
        assert safe["received_chars"] == len("实际模型正文")
        assert safe["first_event_seconds"] >= safe["response_headers_seconds"]
        audit = partial_output_for_audit(failure)
        assert audit["content_head"] == "实际模型正文"
        assert audit["content_tail"] == "" and audit["truncated"] is False
        assert "SECRET" not in json.dumps(audit) + json.dumps(safe) + str(failure)
        assert "实际模型正文" not in json.dumps(safe, ensure_ascii=False) + str(failure)
    finally:
        await runtime.aclose()


async def test_external_deadlines_preserve_separate_concurrent_output_audits(tmp_path):
    ready = {name: asyncio.Event() for name in ("one", "two")}

    class Body(httpx.AsyncByteStream):
        def __init__(self, name):
            self.name = name

        async def __aiter__(self):
            yield event({"choices": [{"delta": {"content": self.name}}]})
            ready[self.name].set()
            await asyncio.Event().wait()

    def handler(request):
        name = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(name))

    runtime, original = make_runtime(tmp_path, handler)
    await original.aclose()

    async def consume(name):
        async for _ in runtime.stream([{"role": "user", "content": name}]):
            pass

    async def deadline(name):
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(consume(name), 0.1)
        return caught.value

    try:
        failures = await asyncio.gather(*(deadline(name) for name in ready))
        assert all(started.is_set() for started in ready.values())
        assert [partial_output_for_audit(exc)["content_head"] for exc in failures] == ["one", "two"]
        assert all(
            safe_failure_diagnostics(exc)["phase"] == "streaming_content" for exc in failures
        )
        assert all("first_content_seconds" in safe_failure_diagnostics(exc) for exc in failures)
        assert not hasattr(runtime, "last_call_diagnostics")
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("kind", ["http", "sse"])
async def test_provider_error_body_never_becomes_partial_output(tmp_path, kind):
    def handler(_):
        if kind == "http":
            return httpx.Response(429, text="SECRET_PROVIDER_BODY")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=event({"error": {"message": "SECRET_PROVIDER_BODY", "type": "SECRET_KEY"}}),
        )

    runtime, original = make_runtime(tmp_path, handler)
    await original.aclose()
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await runtime.complete("validate", {}, {"type": "object"}, "provider-error")
        assert partial_output_for_audit(caught.value) is None
        assert "SECRET" not in str(caught.value) + json.dumps(
            safe_failure_diagnostics(caught.value)
        )
    finally:
        await runtime.aclose()
