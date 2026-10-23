"""Model configuration failures keep actionable, localized and safe explanations."""

import json

import pytest

from folio.i18n import translate_text
from folio.runtime.diagnostics import safe_failure_diagnostics
from folio.runtime.errors import model_http_error, public_model_error


@pytest.mark.parametrize("message", [
    "tool_choice='required' is not supported in thinking mode",
    "Reasoning mode does not support tool_choice",
    "Unsupported tool choice in THINKING mode",
    "Forced tool use is incompatible with reasoning mode",
])
def test_thinking_tool_choice_rejection_has_specific_safe_explanation(message):
    failure = model_http_error({"error": {
        "message": message + " private-api-key private-document-text",
    }}, 400)
    assert failure.code == "tool_choice_unsupported"
    assert safe_failure_diagnostics(failure) == {"status_code": 400}
    public = public_model_error(failure)
    assert "tool_choice" in public["text"]
    english = translate_text(public["text"], "en")
    assert "thinking mode" in english
    assert "private" not in str(failure) + json.dumps(public) + english


@pytest.mark.parametrize("message", [
    "tool_choice and thinking mode are configured; model private-model is unavailable",
    "tool_choice is not supported",
    "thinking mode is unsupported",
    "private-api-key invalid model ID",
])
def test_other_bad_requests_do_not_invent_thinking_tool_incompatibility(message):
    failure = model_http_error({"error": {"message": message}}, 400)
    assert failure.code == "invalid_request"
    public = public_model_error(failure)
    assert "HTTP 400" in public["text"]
    assert "model ID" in translate_text(public["text"], "en")
    assert "private" not in str(failure) + json.dumps(public)


@pytest.mark.parametrize("status,code", [
    (401, "authentication"), (402, "billing"), (403, "authentication"),
    (404, "endpoint_not_found"), (429, "rate_limit"), (502, "model_error"),
])
def test_http_status_is_authoritative_and_provider_text_stays_private(status, code):
    failure = model_http_error({"error": {
        "message": "tool_choice is not supported in thinking mode private-api-key",
    }}, status)
    assert failure.code == code
    public = public_model_error(failure)
    assert public["code"] == code
    assert "private" not in str(failure) + json.dumps(public)
    if status == 404:
        assert "HTTP 404" in public["text"]
        assert "endpoint URL" in translate_text(public["text"], "en")


@pytest.mark.parametrize("specific,code", [
    ({"code": "invalid_json_schema"}, "schema_unsupported"),
    ({"code": "context_length_exceeded"}, "context_limit"),
])
def test_context_and_schema_classification_take_priority_over_tool_choice(specific, code):
    failure = model_http_error({
        "message": "tool_choice is not supported in thinking mode private-api-key",
        "error": {"metadata": {"raw": json.dumps({"error": specific})}},
    }, 400)
    assert failure.code == code
    assert "private" not in str(failure) + json.dumps(public_model_error(failure))
