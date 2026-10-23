import asyncio
import json

import httpx
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime


@pytest.mark.parametrize(
    "body,accepted",
    [
        ('{"answer":"完整结果"}', True),
        ('{"answer":"未结束', False),
        ('{"answer":', False),
        ("{}", False),
        ('{"answer":3}', False),
        ('{"answer":"完整结果"} {}', False),
    ],
)
async def test_padding_stops_only_after_a_complete_schema_valid_object(tmp_path, body, accepted):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # This test replaces transport; keep token estimation local.
    closed, exhausted = [], []

    async def padded_stream(*args, **kwargs):
        try:
            yield "text.delta", {"text": body}
            yield "text.delta", {"text": " " * 512}
            exhausted.append(True)
            raise ModelCallFailed("Incomplete response", "length_limit")
        finally:
            closed.append(True)

    runtime.stream = padded_stream
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    try:
        if accepted:
            result = await runtime.complete("validate", {}, schema, "padding-test")
            assert json.loads(result.content) == {"answer": "完整结果"}
            assert result.usage["usage_incomplete"] is True
            assert not exhausted
        else:
            with pytest.raises(ModelCallFailed) as failure:
                await runtime.complete("validate", {}, schema, "padding-test")
            assert failure.value.code in {"length_limit", "stalled_output"}
            # In-string spaces are content; unfinished objects padded outside
            # strings are now stopped before wasting the rest of the budget.
            assert bool(exhausted) == body.endswith("未结束")
        assert closed == [True]
    finally:
        await runtime.aclose()


async def test_incomplete_json_whitespace_stall_is_rejected_early(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Exercise streamed output validation without HTTP probes.
    exhausted = []

    async def stalled_stream(*args, **kwargs):
        yield "text.delta", {"text": '{"answer":' + " " * 512}
        exhausted.append(True)

    runtime.stream = stalled_stream
    try:
        with pytest.raises(ModelCallFailed) as error:
            await runtime.complete("validate", {}, {"type": "object"}, "stall-test")
        assert error.value.code == "stalled_output"
        assert not exhausted
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "failure", [None, "length", "missing_stop", "tool", "malformed", "unauthorized"]
)
async def test_native_stream_protocol_no_hidden_reasoning(tmp_path, monkeypatch, failure):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if failure == "unauthorized":
            return httpx.Response(401, text="secret upstream response")
        delta = {"content": "你好", "reasoning_content": "private reasoning"}
        if failure == "tool":
            delta["tool_calls"] = [{"function": {"name": "exec"}}]
        chunks = [{"choices": [{"delta": delta, "finish_reason": None}]}]
        if failure != "missing_stop":
            chunks.append(
                {
                    "choices": [
                        {"delta": {}, "finish_reason": "length" if failure == "length" else "stop"}
                    ]
                }
            )
        chunks.append({"choices": [], "usage": {"total_tokens": 5}})
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        if failure == "malformed":
            body = "data: broken\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=body)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(base_url="http://example.test/v1", model="local", no_auth=True)
    )
    runtime = LLMRuntime(settings.path)
    try:
        events = []
        if failure:
            with pytest.raises(ModelCallFailed):
                async for event in runtime.stream([{"role": "user", "content": "你好"}]):
                    events.append(event)
            assert not any(kind == "completed" for kind, _ in events)
        else:
            events = [e async for e in runtime.stream([{"role": "user", "content": "你好"}])]
            assert events[-1] == ("completed", {"text": "你好", "usage": {"total_tokens": 5}})
        assert "private reasoning" not in json.dumps(events)
        assert "tools" not in requests[0] and "tool_choice" not in requests[0]
        assert "chat_template_kwargs" not in requests[0]
        assert "response_format" not in requests[0]
    finally:
        await runtime.aclose()


def test_private_configuration_migrates_without_key_loss(tmp_path):
    import stat

    from folio.migrations import legacy_model_path

    directory = tmp_path / "settings"
    directory.mkdir()
    old = legacy_model_path(directory)
    old.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"provider": "custom", "model": "existing"}},
                "providers": {
                    "custom": {"apiBase": "http://example.test/v1", "apiKey": "existing-secret"}
                },
            }
        )
    )
    settings = ModelSettingsStore(tmp_path)
    assert not old.exists()
    assert json.loads(settings.path.read_text())["api_key"] == "existing-secret"
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    assert "existing-secret" not in json.dumps(settings.public())
    assert ModelSettingsStore(tmp_path).public()["model"] == "existing"


def test_native_config_environment_substitution_and_safe_error(tmp_path, monkeypatch):
    from folio.model_settings import load_model_config

    config = tmp_path / "model.json"
    config.write_text(
        json.dumps({"base_url": "${MODEL_ENDPOINT}", "model": "test", "api_key": "${MODEL_SECRET}"})
    )
    monkeypatch.setenv("MODEL_ENDPOINT", "http://local.test/v1")
    monkeypatch.setenv("MODEL_SECRET", "never-print-this-secret")
    assert load_model_config(config)["api_key"] == "never-print-this-secret"
    monkeypatch.delenv("MODEL_ENDPOINT")
    with pytest.raises(ModelCallFailed) as error:
        load_model_config(config)
    assert error.value.code == "configuration"
    assert "never-print-this-secret" not in str(error.value)


def test_legacy_unfinished_job_is_not_silently_resumed(store, demo_job):
    from folio.migrations import migrate_jobs

    with store.connect() as conn:
        data = json.loads(
            conn.execute("SELECT input FROM jobs WHERE id=?", (demo_job,)).fetchone()[0]
        )
        # Build the retired identifier only in this migration fixture.
        data["runtime"] = "nano" + "bot"
        conn.execute(
            "UPDATE jobs SET input=?,status='running' WHERE id=?", (json.dumps(data), demo_job)
        )
        migrate_jobs(conn)
    assert store.get(demo_job)["status"] == "failed"
    assert demo_job not in store.candidates()


async def test_native_transport_runs_document_engine_and_persisted_progress(
    store, inputs, monkeypatch
):
    """Run the production transport and engine against deterministic HTTP responses."""
    from contextvars import ContextVar
    from pathlib import Path

    from docx import Document
    from jsonschema import Draft202012Validator

    from folio.api.workspace import job_events
    from folio.models import JobInput
    from folio.orchestration.engine import Engine
    from folio.runtime.demo import DemoRuntime

    calls = []
    research_messages = []
    fixture_runtime = DemoRuntime()
    current_request = ContextVar("native_transport_fixture_request")

    class FixtureRuntime(LLMRuntime):
        async def complete(self, task, context, schema, session_key):
            # The HTTP fixture needs structured state to generate deterministic
            # replies; the production serializer still sends the real wire text.
            token = current_request.set((task, context))
            try:
                return await super().complete(task, context, schema, session_key)
            finally:
                current_request.reset(token)

    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        system, user = payload["messages"][:2]
        task = system["content"].split("Task type: ", 1)[1].split(".", 1)[0]
        from folio.research.native_tools import NATIVE_TOOL_TASKS

        research_task = task in NATIVE_TOOL_TASKS
        if research_task:
            assert "response_format" not in payload
            assert "JSON Schema:" not in system["content"]
            fixture_task, context = current_request.get()
            assert fixture_task == task
            assert payload["tool_choice"] == "required"
            assert payload["parallel_tool_calls"] is (task in {"directory_research", "research_step"})
            assert payload["tools"]
            visible_schema = None
            research_messages.append(json.dumps(payload["messages"], ensure_ascii=False))
        else:
            visible_schema = json.JSONDecoder().raw_decode(
                system["content"].split("JSON Schema:\n", 1)[1]
            )[0]
            assert visible_schema == payload["response_format"]["json_schema"]["schema"]
        if task == "plan":
            from folio.document.planning_transport import planning_payload, planning_text

            context = json.loads(user["content"])
            assert user["content"] == planning_text(planning_payload(context))
            assert context["requirements"] == inputs["user_request"]
            assert "user_request" not in context
            assert context["source_catalog"] == expected_catalog
            assert context["source_catalog"][0]["name"] == "rules.md"
            assert "excerpt" not in context["source_catalog"][0]
            assert context["directory_research"]["status"] == "ready"
            assert "主管审批" in "\n".join(
                item["text"] for item in context["directory_research"]["evidence"]
            )
            assert set(visible_schema["$defs"]["PlanItem"]["properties"]) == {
                "node_id", "title", "level", "instruction", "content_mode"
            }
            assert set(visible_schema["properties"]) == {"sections", "notes", "document_title"}
            assert not {"template", "outline"}.intersection(context)
        elif not research_task:
            context = json.loads(user["content"])
            from folio.runtime.business_transport import unpack_rows

            context = unpack_rows(context)
        result = await fixture_runtime.complete(task, context, {}, "fixture")
        if task == "extract":
            # Strict structured output requires explicit defaults for every provider.
            from folio.runtime.base import RuntimeResult

            extraction = json.loads(result.content)
            for claim in extraction.get("claims", []):
                claim.setdefault("scope", "document")
                claim.setdefault("cardinality", "unspecified")
            result = RuntimeResult(json.dumps(extraction))
        if research_task:
            assert result.tool_calls
            offered = {tool["function"]["name"]: tool["function"] for tool in payload["tools"]}
            for call in result.tool_calls:
                function = call["function"]
                arguments = json.loads(function["arguments"])
                if arguments.get("path") == "rules.md":
                    del arguments["path"]
                    arguments["file"] = "F1"
                function["arguments"] = json.dumps(arguments, ensure_ascii=False)
                Draft202012Validator(offered[function["name"]]["parameters"]).validate(arguments)
        if task == "work_plan":
            wire_result = json.loads(result.content)
            wire_result["assignments"] = {
                sid: t["task_id"] for t in wire_result["tasks"] for sid in t["section_ids"]
            }
            for t in wire_result["tasks"]:
                t["node_refs"] = t.pop("section_ids")
            from folio.runtime.base import RuntimeResult

            result = RuntimeResult(json.dumps(wire_result))
        if research_task:
            chunks = [
                {"choices": [{"delta": {"tool_calls": [call | {"index": index} for index, call in enumerate(result.tool_calls)]}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            chunks = [
                {"choices": [{"delta": {"content": result.content}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )
    settings = ModelSettingsStore(store.root)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="http-fixture",
            no_auth=True,
            enable_thinking=False,
            structured_output="json_schema",
        )
    )
    runtime = FixtureRuntime(settings.path)
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    from folio.source_documents import source_catalog

    expected_catalog = source_catalog(
        Path(store.get(job_id)["input"]["knowledge_path"]),
        cache_root=store.root / "source-cache",
        include_content=False,
    )
    assert len(expected_catalog) == 1
    try:
        job = await Engine(store, runtime).run(job_id)
    finally:
        await runtime.aclose()
    assert job["status"] == "published"
    assert "交易撤销" in "".join(p.text for p in Document(Path(job["artifact"])).paragraphs)
    assert all(c["chat_template_kwargs"] == {"enable_thinking": False} for c in calls)
    assert any("tools" in c for c in calls)
    assert all("response_format" not in c for c in calls if "tools" in c)
    assert all(c["response_format"]["type"] == "json_schema" for c in calls if "tools" not in c)
    assert all(c["response_format"]["json_schema"]["schema"]["type"] == "object" for c in calls if "tools" not in c)
    assert research_messages
    assert any("外汇交易由交易员录入。交易撤销需要主管审批。" in text for text in research_messages)
    events = "".join([e async for e in job_events(store, job_id)])
    assert "演示：仅依据原文判断" in events  # Test endpoint's summary reaches the progress stream.
    assert "artifact.ready" in events
    with store.connect() as conn:
        records = conn.execute(
            "SELECT metadata FROM decisions WHERE job_id=?", (job_id,)
        ).fetchall()
    assert records and all(json.loads(r[0])["model"] == "http-fixture" for r in records)



def test_planning_context_sends_full_confirmed_requirements_once(tmp_path):
    from folio.document.guidance import load_guide
    from folio.document.planner import planning_context
    from folio.models import JobInput
    from folio.orchestration.engine import Engine
    from folio.requirements import RequirementEntry, RequirementSnapshot

    requirements = RequirementSnapshot.from_entries([
        RequirementEntry(source_id="message:0", text="保留订单范围。" * 2000),
        RequirementEntry(source_id="message:2", kind="revision", text="撤销必须由主管审批。"),
    ])
    engine = Engine.__new__(Engine)
    engine.request = JobInput(
        title="订单需求", requirements=requirements, knowledge_path=str(tmp_path),
        writing_guide=load_guide("srs"),
    )
    original = planning_context(requirements.text, []) | {
        "requirements_revision": requirements.revision,
    }
    context = engine.request_context("plan", original)
    assert context["requirements"] == requirements.text
    assert context["requirements"].endswith("撤销必须由主管审批。")
    assert len(context["requirements"]) > 12000
    assert context["requirements_revision"] == requirements.revision
    assert "user_request" not in context
    assert context["writing_guidance"]["id"] == "srs"
    assert engine.request_context("plan", context) == context
    assert "writing_guidance" not in original
    # Other action prompts still receive the same complete confirmed requirements.
    assert engine.request_context("next_action", {})["user_request"] == requirements.text


def test_output_schema_encodes_paragraph_and_table_rules():
    from jsonschema import ValidationError, validate

    from folio.models import SectionContent

    schema = SectionContent.model_json_schema()
    for block in [
        {"block_id": "b1", "kind": "paragraph", "text": "正文", "rows": []},
        {"block_id": "b1", "kind": "table", "rows": [["字段", "说明"], ["A", "内容"]]},
    ]:
        validate({"blocks": [block | {"origin": "evidence", "evidence_ids": ["e1"]}]}, schema)
    for block in [
        {"block_id": "b1", "kind": "paragraph", "text": "正文", "rows": [["错误混入表格"]]},
        {"block_id": "b1", "kind": "heading", "text": ""},
        {"block_id": "b1", "kind": "table", "rows": []},
    ]:
        with pytest.raises(ValidationError):
            validate({"blocks": [block | {"origin": "evidence", "evidence_ids": ["e1"]}]}, schema)
    for missing in ("origin", "evidence_ids"):
        block = {"block_id": "b1", "text": "正文", "origin": "evidence", "evidence_ids": ["e1"]}
        block.pop(missing)
        with pytest.raises(ValidationError):
            validate({"blocks": [block]}, schema)


@pytest.mark.parametrize("mode", ["recover", "exhausted", "input_full", "unknown", "other_400"])
async def test_context_rejection_reduces_only_output_with_bounded_retries(tmp_path, mode):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1", model="test", no_auth=True, max_tokens=8192
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if mode == "recover" and len(requests) == 2:
            chunk = {
                "choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 2},
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            )
        message = "This model's maximum context length is 32768 tokens. However, you requested 8192 output tokens and your prompt contains at least 24577 input tokens. private-detail"
        if mode == "input_full":
            message = message.replace("24577", "32768")
        if mode == "unknown":
            message = "This model's maximum context length is 32768 tokens. private-detail"
        if mode == "other_400":
            message = "Unsupported schema. private-detail"
        return httpx.Response(400, json={"error": {"message": message}})

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        if mode == "recover":
            result = await runtime.complete(
                "validate", {"content": "完整正文"}, {"type": "object"}, "test"
            )
            assert result.usage["context_rejections"] == 1
            assert result.usage["effective_max_tokens"] == 6144
            assert result.content == "{}"
        else:
            with pytest.raises(ModelCallFailed) as error:
                await runtime.complete(
                    "validate", {"content": "完整正文"}, {"type": "object"}, "test"
                )
            assert "private-detail" not in str(error.value)
            assert error.value.diagnostics == {"status_code": 400}
        expected = (
            [8192, 6144]
            if mode == "recover"
            else [8192, 6144, 4608]
            if mode == "exhausted"
            else [8192]
        )
        assert [r["max_tokens"] for r in requests] == expected
        assert all(
            {k: v for k, v in r.items() if k != "max_tokens"}
            == {k: v for k, v in requests[0].items() if k != "max_tokens"}
            for r in requests
        )
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "status,code",
    [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (502, "model_error")],
)
async def test_http_failure_keeps_only_numeric_status_diagnostics(tmp_path, status, code):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Inject the failure at generation, not capacity discovery.
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "private-upstream-response"})
        )
    )
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await runtime.complete("intent", {}, {"type": "object"}, "http-failure")
        assert caught.value.code == code
        assert caught.value.diagnostics == {"status_code": status}
        assert "private-upstream-response" not in str(caught.value)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("phase", ["connect", "first_content", "streaming_content"])
async def test_model_timeout_retains_stage_without_recording_partial_text(tmp_path, phase):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if phase == "streaming_content":
                yield b'data: {"choices":[{"index":0,"delta":{"content":"private partial text"}}]}\n\n'
            raise httpx.ReadTimeout("upstream private address")

    async def handler(request):
        if phase == "connect":
            raise httpx.ConnectTimeout("upstream private address")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelCallFailed) as error:
            async for _ in runtime.stream([{"role": "user", "content": "test"}]):
                pass
        assert error.value.code == "timeout"
        info = error.value.diagnostics
        assert info["phase"] == ("connect_or_response_headers" if phase == "connect" else phase)
        assert info["received_chars"] == (20 if phase == "streaming_content" else 0)
        assert "private" not in str(error.value) + json.dumps(info)
    finally:
        await runtime.aclose()


async def test_external_deadline_keeps_safe_stream_diagnostics_on_timeout_cause(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # The deadline here covers the generation stream.
    await runtime.client.aclose()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            await asyncio.Event().wait()

    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Body()
            )
        )
    )
    try:
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(
                runtime.complete("validate", {}, {"type": "object"}, "deadline"), timeout=0.05
            )
        assert isinstance(caught.value.__cause__, asyncio.CancelledError)
        info = caught.value.__cause__.diagnostics
        assert info["phase"] == "first_content"
        assert info["received_chars"] == 0
        assert info["reasoning_chars"] == len("private reasoning")
        assert info["elapsed_seconds"] >= 0
        assert "first_content_seconds" not in info
        assert "private" not in json.dumps(info)
        assert not hasattr(runtime, "last_call_diagnostics")
    finally:
        await runtime.aclose()


async def test_cancelled_concurrent_calls_keep_individual_diagnostics(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    started = [asyncio.Event(), asyncio.Event()]

    class Body(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index = index

        async def __aiter__(self):
            chunk = {
                "choices": [{"delta": {"content": "x" * (self.index + 1), "reasoning": "secret"}}]
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            started[self.index].set()
            await asyncio.Event().wait()

    def handler(request):
        index = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Body(index)
        )

    async def consume(index):
        async for _ in runtime.stream([{"role": "user", "content": index}]):
            pass

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    calls = [asyncio.create_task(consume(i)) for i in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=2)
        for call in calls:
            call.cancel()
        results = []
        for call in calls:
            with pytest.raises(asyncio.CancelledError) as caught:
                await call
            results.append(caught.value.diagnostics)
        assert [info["received_chars"] for info in results] == [1, 2]
        assert all(info["phase"] == "streaming_content" for info in results)
        assert all(info["reasoning_chars"] == len("secret") for info in results)
        assert all(info["first_content_seconds"] >= 0 for info in results)
        assert "secret" not in json.dumps(results)
    finally:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.parametrize(
    "upstream,code,expected",
    [
        (
            {
                "type": "BadRequestError",
                "message": 'Grammar error: Unimplemented keys: ["uniqueItems", "secret-property"] prompt=private-content key=private-key',
            },
            "schema_unsupported",
            "uniqueItems",
        ),
        (
            {"message": "Grammar error: private-content private-key"},
            "schema_unsupported",
            "JSON Schema",
        ),
        (
            {"type": "ValueError", "message": "private-content private-key"},
            "protocol",
            "backend_type=ValueError",
        ),
        (
            {"type": "private-type", "message": "private-content private-key"},
            "protocol",
            "Invalid model response",
        ),
        ("private-content private-key", "protocol", "Invalid model response"),
    ],
)
async def test_stream_backend_error_keeps_safe_classification_without_provider_body(
    tmp_path, upstream, code, expected
):
    from folio.runtime.errors import public_model_error

    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # Only the generation endpoint supplies this backend error.
    await runtime.client.aclose()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps({"error": upstream}) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await runtime.complete("review", {}, {"type": "object"}, "safe-error")
        failure = caught.value
        assert failure.code == code and expected in str(failure)
        public = public_model_error(failure)
        assert public["code"] == code
        diagnostic = (
            str(failure) + json.dumps(getattr(failure, "diagnostics", {})) + json.dumps(public)
        )
        assert not any(
            value in diagnostic
            for value in ["private-content", "private-key", "private-type", "secret-property"]
        )
        assert len(requests) == 1
        if expected == "uniqueItems":
            assert failure.diagnostics["unsupported_schema_keywords"] == ["uniqueItems"]
    finally:
        await runtime.aclose()


async def test_backend_schema_error_is_persisted_safely_for_review_audit(store, inputs, tmp_path):
    from test_cross_review_contract import context

    from folio.models import JobInput, Limits, Review
    from folio.orchestration.engine import Engine

    settings = ModelSettingsStore(tmp_path)
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True
    await runtime.client.aclose()
    message = 'Grammar error: Unimplemented keys: ["uniqueItems"] private-prompt private-api-key'
    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps({"error": {"message": message}}) + "\n\n",
            )
        )
    )
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="llm", limits=Limits(call_attempts=3))
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    try:
        with pytest.raises(ModelCallFailed) as caught:
            await engine._call("review", context(), Review)
        assert caught.value.code == "schema_unsupported"
        events = store.events(engine.job_id)
        failures = [e for e in events if e["kind"] == "task_failed"]
        assert len(failures) == 1
        error = failures[0]["data"]["error"]
        assert "[schema_unsupported]" in error and "uniqueItems" in error
        assert not any(e["kind"] == "task_completed" for e in events)
        assert "private-prompt" not in json.dumps(events)
        assert "private-api-key" not in json.dumps(events)
        assert store.get(engine.job_id)["calls"] == 1
    finally:
        await runtime.aclose()
