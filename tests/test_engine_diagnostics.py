import asyncio
import json

import httpx
import pytest
from test_request_context import engine_for

from folio.models import Extraction, Query
from folio.runtime.demo import DemoRuntime
from folio.runtime.diagnostics import capture_partial_output
from folio.runtime.errors import ModelCallFailed


@pytest.mark.parametrize("external_timeout", [False, True])
async def test_failed_engine_calls_keep_private_output_separate_from_usage_and_events(
    store, inputs, external_timeout
):
    private_output = '{"正文":"PRIVATE_MODEL_OUTPUT"'

    class Failure(DemoRuntime):
        async def complete(self, task, context, schema, session):
            failure = (
                asyncio.CancelledError()
                if external_timeout
                else ModelCallFailed("Incomplete response", "length_limit")
            )
            failure.diagnostics = {
                "phase": "streaming_content",
                "received_chars": len(private_output),
                "first_content_seconds": 0.75,
                "unsafe_provider_text": "PROVIDER_SECRET",
            }
            capture_partial_output(failure, private_output)
            if external_timeout:
                raise TimeoutError() from failure
            raise failure

    engine = engine_for(store, inputs, Failure())
    engine.limits.call_attempts = 1
    with pytest.raises((TimeoutError, ModelCallFailed)):
        await engine._call("query", {"question": "需要什么资料"}, Query)
    with store.connect() as conn:
        rows = list(conn.execute("SELECT usage,error FROM tasks WHERE job_id=?", (engine.job_id,)))
        events = list(conn.execute("SELECT data FROM events WHERE job_id=?", (engine.job_id,)))
    assert len(rows) == 1
    usage = json.loads(rows[0]["usage"])
    assert usage["diagnostics"] == {
        "phase": "streaming_content",
        "received_chars": len(private_output),
        "first_content_seconds": 0.75,
    }
    public = json.dumps([dict(row) for row in rows] + [dict(row) for row in events])
    assert "PRIVATE_MODEL_OUTPUT" not in public and "PROVIDER_SECRET" not in public
    archives = list(
        (store.root / "jobs" / engine.job_id / "contexts" / "failed_outputs").glob("*.json")
    )
    assert len(archives) == 1
    archive = json.loads(archives[0].read_text())
    assert archive["partial_output"]["content_head"] == private_output
    assert archive["partial_output"]["truncated"] is False
    assert "PROVIDER_SECRET" not in archives[0].read_text()


@pytest.mark.parametrize("fault", ["missing_comma", "unescaped_quote", None])
async def test_completed_gap_response_decode_failure_preserves_wire_text_privately(
    store, inputs, tmp_path, monkeypatch, fault
):
    from folio.model_settings import ModelSettingsInput, ModelSettingsStore
    from folio.orchestration.work import GapCompletion
    from folio.runtime.llm import LLMRuntime

    value = {"proposals": [{
        "gap_id": "g1", "section_ids": ["s1"], "disposition": "body",
        "content": 'PRIVATE_MODEL_RESPONSE：建议对“审批”建立完整的申请、复核和留痕流程。',
    }]}
    wire_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if fault == "missing_comma":
        wire_text = wire_text.replace(',"content":', '"content":')
    elif fault == "unescaped_quote":
        wire_text = wire_text.replace('“审批”', '"审批"')
    events = [
        {"choices": [{"delta": {"content": wire_text, "reasoning_content": "PRIVATE_REASONING"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 70}},
    ]
    body = "".join("data: " + json.dumps(item, ensure_ascii=False) + "\n\n" for item in events).encode()
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime.capacity.ready = True
    engine = engine_for(store, inputs, runtime)
    engine.limits.call_attempts = 1
    context = {"gaps": [{"gap_id": "g1", "section_ids": ["s1"], "question": "补充审批流程"}],
               "sections": [{"section_id": "s1", "title": "审批"}]}
    if fault is None:
        def unexpected_copy(*args):
            pytest.fail("Successful responses must not create a private failure copy")

        monkeypatch.setattr("folio.runtime.llm.capture_partial_output", unexpected_copy)
    try:
        if fault is None:
            result = await engine._call("complete_gaps", context, GapCompletion)
            assert result.proposals[0].content == value["proposals"][0]["content"]
        else:
            with pytest.raises(json.JSONDecodeError) as caught:
                await engine._call("complete_gaps", context, GapCompletion)
            assert caught.value.msg == "Expecting ',' delimiter"
            assert caught.value.doc == wire_text
            assert wire_text[caught.value.pos - 3:caught.value.pos + 3]
    finally:
        await runtime.aclose()
    assert len(calls) == 1  # No extra recovery model call or tolerant parsing.
    archives = list((store.root / "jobs" / engine.job_id / "contexts" / "failed_outputs").glob("*.json"))
    if fault is None:
        assert not archives
        return
    assert len(archives) == 1
    archive = json.loads(archives[0].read_text())
    assert archive["task"] == "complete_gaps" and archive["attempt"] == 1
    assert archive["partial_output"]["content_head"] == wire_text
    assert archive["partial_output"]["truncated"] is False
    assert "PRIVATE_REASONING" not in archives[0].read_text()
    with store.connect() as connection:
        tasks = list(connection.execute("SELECT usage,error FROM tasks WHERE job_id=?", (engine.job_id,)))
        public_events = list(connection.execute("SELECT data FROM events WHERE job_id=?", (engine.job_id,)))
    public = json.dumps([dict(row) for row in tasks] + [dict(row) for row in public_events])
    assert "PRIVATE_MODEL_RESPONSE" not in public and "PRIVATE_REASONING" not in public


async def test_extraction_retry_reports_paths_and_rules_without_echoing_rejected_facts(
    store, inputs, tmp_path
):
    from folio.model_settings import ModelSettingsInput, ModelSettingsStore
    from folio.runtime.llm import LLMRuntime

    settings = ModelSettingsStore(tmp_path / "model")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    runtime.capacity.ready = True
    messages_sent, outputs = [], []
    valid_statement = "条" * 249

    def handler(request):
        payload = json.loads(request.content)
        messages_sent.append(payload["messages"])
        context = json.loads(payload["messages"][1]["content"])
        evidence_ref = context["evidence"][0]["evidence_id"]
        section_ref = context["section"]["section_id"]
        first = len(messages_sent) == 1
        value = {
            "s": "保留完整限定条件", "e": ["审批"], "c": [{
                "i": "c1", "b": ["b1"], "t": "PRIVATE_FACT_" + "私" * 3500 if first else valid_statement,
                "s": "审批", "p": "责任", "v": "主管", "o": "当前项目", "k": "single",
                "e": [section_ref if first else evidence_ref],
            }],
        }
        wire = json.dumps(value, ensure_ascii=False)
        outputs.append(wire)
        chunk = {"choices": [{"delta": {"content": wire}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = engine_for(store, inputs, runtime)
    context = {
        "section": {"section_id": "sec_conclusion", "title": "结论"},
        "content": {"blocks": [{"block_id": "b1", "text": "审批由主管负责。", "origin": "evidence"}]},
        "evidence": [{"evidence_id": "ev_approval", "text": "审批由主管负责。"}],
    }
    try:
        result = await engine._call("extract", context, Extraction)
    finally:
        await runtime.aclose()
    assert len(messages_sent) == 2
    assert result.claims[0].statement == valid_statement
    assert result.claims[0].evidence_ids == ["ev_approval"]
    feedback = json.loads(messages_sent[1][1]["content"])["format_feedback"]
    assert "c[0].t: maxLength; expected <= 3500 characters; actual 3513" in feedback
    assert "c[0].e[0]: enum; expected one of 1 allowed evidence references" in feedback
    assert "PRIVATE_FACT" not in feedback
    archives = list((store.root / "jobs" / engine.job_id / "contexts" / "failed_outputs").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["partial_output"]["content_head"] == outputs[0]
    with store.connect() as connection:
        events = "\n".join(row[0] for row in connection.execute(
            "SELECT data FROM events WHERE job_id=?", (engine.job_id,)
        ))
    assert "PRIVATE_FACT" not in events
