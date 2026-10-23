"""Exercise relevance routing at mocked provider HTTP and persisted-source boundaries."""

import json
import math
from copy import deepcopy

import httpx
import pytest
from decision_wire import payload as decision_payload
from decision_wire import transport as decision_transport

from folio.decisions import IntentDecision
from folio.model_settings import probe_fingerprint
from folio.models import JobInput
from folio.orchestration.context import ContextSelection, apply_selection, dependency_catalog
from folio.orchestration.engine import Engine
from folio.runtime.business_transport import unpack_rows
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime


def runtime_for(
    tmp_path, monkeypatch, handler, *, enabled=True, mode="json_schema", decision_model="jev"
):
    config = {
        "version": 1,
        "base_url": "http://primary.invalid/v1",
        "model": "primary-test",
        "no_auth": True,
        "max_tokens": 4096,
        "structured_output": mode,
        "provider_mode": "custom",
        "decision_enabled": enabled,
        "decision_model": "jev",
        "decision_base_url": "https://judge.test/v1", "decision_api_key": "mock-secret",
    }
    if decision_model != "jev":
        config.update(model="mimo-pro", decision_model=decision_model)
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    path = tmp_path / "selection-model.json"
    path.write_text(json.dumps(config))
    client_type = httpx.AsyncClient

    def client(*args, **kwargs):
        return client_type(*args, **kwargs, transport=decision_transport(handler))

    # Both independently created Jev clients and the runtime's primary client
    # use real request/response parsing, with no possible external HTTP request.
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return LLMRuntime(path)


def completion(value):
    chunk = {
        "choices": [{"delta": {"content": json.dumps(value)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
    )


def relevance_response(payload, probability=lambda _: 0.9):
    items = payload["state"]["catalog"]
    return httpx.Response(
        200,
        json={
            "answers": {
                name: {"type": "noul", "noul": probability(items[int(name.split("_")[1])])}
                for name in payload["questions"]
            },
            "model": "jev-mock",
            "usage": {"input_tokens": 20},
        },
    )


def evidence_catalog(count, text_size=1500):
    return [
        {
            "context_ref": f"outer_{index}",
            "kind": "evidence",
            "value": {
                "evidence_id": f"evidence_{index}",
                "source_id": f"source_{index}",
                "text": "规" * text_size + f"第{index}条不得省略",
            },
        }
        for index in range(count)
    ]


async def test_jev_strict_relevance_boundary_is_audited_for_every_candidate_kind(
    tmp_path, monkeypatch
):
    probabilities = [0, 0.2, 0.5, 0.8, math.nextafter(0.8, 1), 1]
    catalog = [
        {
            "context_ref": f"{kind}_{index}",
            "kind": kind,
            "value": {"text": "PRIVATE_SOURCE 不得擅自跳过审批。"},
        }
        for kind in ("evidence", "fact", "proposal", "risk")
        for index in range(len(probabilities))
    ]
    sent = []

    def handler(request):
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        sent.append(payload)
        for question in payload["questions"].values():
            assert "retain when uncertain" not in question["instructions"]
            assert "directly relevant to the current purpose and does it contain meaningful business content" in question["instructions"]
            assert "strictly greater than 0.8" in question["instructions"]
        return relevance_response(
            payload, lambda item: probabilities[int(item["context_ref"].rsplit("_", 1)[1])]
        )

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        result = await runtime.complete(
            "select_context",
            {"purpose": "审批条件", "catalog": catalog},
            ContextSelection.model_json_schema(),
            "strict-probability",
        )
    finally:
        await runtime.aclose()
    selection = ContextSelection.model_validate_json(result.content)
    assert selection.selected_refs == [
        f"{kind}_{index}" for kind in ("evidence", "fact", "proposal") for index in (4, 5)
    ]
    assert selection.risk_refs == ["risk_4", "risk_5"]
    decisions = result.usage["decision_relevance"]
    assert len(decisions) == len(catalog)
    assert {row["page"] for row in decisions} == {str(i) for i in range(len(sent))}
    for item, row in zip(catalog, decisions, strict=True):
        probability = probabilities[int(item["context_ref"].rsplit("_", 1)[1])]
        assert row == {
            "context_ref": item["context_ref"],
            "probability": probability,
            "accepted": probability > 0.8,
            "threshold": 0.8,
            "page": row["page"],
        }
    assert [row for page in result.usage["selection_page_audit"] for row in page["relevance"]] == [
        {key: value for key, value in row.items() if key != "page"} for row in decisions
    ]
    assert "PRIVATE_SOURCE" not in json.dumps(result.usage)
    assert "mock-secret" not in json.dumps(result.usage)


@pytest.mark.parametrize("probability", [True, False, float("nan"), float("inf"), -float("inf")])
async def test_jev_invalid_probability_fails_without_llm_fallback(
    tmp_path, monkeypatch, probability
):
    calls = []

    def handler(request):
        assert request.url.host == "judge.test"
        calls.append(request)
        # Use literal response bytes to exercise even nonstandard NaN/Infinity
        # replies; the provider parser must reject them before selection.
        return httpx.Response(
            200,
            text=json.dumps({"answers": {"item_0": {"type": "noul", "noul": probability}}}),
        )

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        with pytest.raises(ModelCallFailed) as error:
            await runtime.complete(
                "select_context",
                {"purpose": "审批", "catalog": evidence_catalog(1)},
                ContextSelection.model_json_schema(),
                "invalid-probability",
            )
    finally:
        await runtime.aclose()
    assert len(calls) == 1 and error.value.code == "protocol"


async def test_selection_policy_upgrade_recomputes_old_cache_and_preserves_other_task_cache(
    tmp_path, monkeypatch, store, inputs
):
    sent = []
    probability = 0.9

    def handler(request):
        if request.url.path == "/v1/models":
            # The unrelated intent task keeps its existing capacity probe.
            return httpx.Response(404)
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        sent.append(payload)
        if "intent" in payload["questions"]:
            choices = payload["questions"]["intent"]["criteria"]
            return httpx.Response(
                200,
                json={
                    "answers": {
                        "intent": {
                            "type": "choice",
                            "choice": "chat",
                            "confidence": 1,
                            "probabilities": {key: int(key == "chat") for key in choices},
                        }
                    }
                },
            )
        return relevance_response(payload, lambda _: probability)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    context = {"purpose": "审批", "catalog": evidence_catalog(1)}
    intent_context = {"latest_user_message": "讨论一下"}
    try:
        assert runtime.material_selection_policy == 3
        runtime.material_selection_policy = 2
        old = await engine._call("select_context", context, ContextSelection)
        assert old.selected_refs == ["outer_0"]
        await engine._call("intent", intent_context, IntentDecision)
        assert len(sent) == 2
        runtime.material_selection_policy = 3
        probability = 0.8
        new = await engine._call("select_context", context, ContextSelection)
        assert new.selected_refs == [] and len(sent) == 3
        assert await engine._call("select_context", context, ContextSelection) == new
        await engine._call("intent", intent_context, IntentDecision)
        assert len(sent) == 3
        with store.connect() as conn:
            selections = conn.execute(
                "SELECT result, usage FROM tasks WHERE job_id=? AND task_key LIKE 'select_context:%'",
                (engine.job_id,),
            ).fetchall()
        assert len(selections) == 2
        assert {tuple(json.loads(row["result"])["selected_refs"]) for row in selections} == {
            (),
            ("outer_0",),
        }
        assert all(json.loads(row["usage"])["decision_relevance"] for row in selections)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("capacity_retry", [False, True])
async def test_enabled_selection_pages_only_call_jev_even_on_capacity_retry(
    tmp_path, monkeypatch, capacity_retry
):
    sent = []

    def handler(request):
        assert request.url.host == "judge.test", "Primary model/tokenizer must stay unused"
        assert request.url.path == "/v1/chat/completions"
        payload = decision_payload(request)
        assert len(payload["state"]["catalog"]) <= 24
        assert (
            len(json.dumps({k: payload[k] for k in ("state", "questions")}, ensure_ascii=False))
            <= 600000
        )
        sent.append(payload)
        return relevance_response(payload)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    context = {
        "purpose": "编写付款业务，保留相关否定约束",
        "user_request": "根据完整原始资料编制",
        "selection_capacity_retry": capacity_retry,
        "catalog": evidence_catalog(41),
    }
    original = deepcopy(context)
    try:
        measured = await runtime.measure(
            "select_context", context, ContextSelection.model_json_schema(), 48000
        )
        assert measured["fits"] and measured["selection_batches"] < 6 and not sent
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "material"
        )
    finally:
        await runtime.aclose()
    selected = ContextSelection.model_validate_json(result.content)
    assert selected.selected_refs == [item["context_ref"] for item in context["catalog"]]
    assert [item for payload in sent for item in payload["state"]["catalog"]] == context["catalog"]
    assert all(payload["state"]["selection_capacity_retry"] == capacity_retry for payload in sent)
    assert all(payload["state"]["purpose"] == context["purpose"] for payload in sent)
    assert result.usage["selection_provider"] == "decision"
    assert result.usage["decision_calls"] == len(sent) == measured["selection_batches"]
    assert any(len(payload["questions"]) > 8 for payload in sent)
    assert context == original


async def test_jev_pages_fill_actual_state_and_question_budget_without_truncation(
    tmp_path, monkeypatch
):
    sent = []

    def handler(request):
        assert request.url.host == "judge.test", "Packing must not use the primary tokenizer"
        payload = decision_payload(request)
        sent.append(payload)
        assert len(payload["questions"]) <= 24
        assert (
            len(
                json.dumps(
                    {key: payload[key] for key in ("state", "questions")}, ensure_ascii=False
                )
            )
            <= 600000
        )
        return relevance_response(payload)

    catalog = evidence_catalog(25, 3600)
    catalog[0]["value"]["text"] = "完整长规则" * 3400
    context = {
        "purpose": {"section": "付款", "instruction": "只看本节，不得截断例外"},
        "catalog": catalog,
    }
    original = deepcopy(context)
    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "packed"
        )
        for payload, following in zip(sent, sent[1:], strict=False):
            if len(payload["questions"]) < 24:
                bigger = payload["state"] | {
                    "catalog": payload["state"]["catalog"] + following["state"]["catalog"][:1]
                }
                questions, _ = runtime.material_selector.router.questions("select_context", bigger)
                assert (
                    len(json.dumps({"state": bigger, "questions": questions}, ensure_ascii=False))
                    > 600000
                )
    finally:
        await runtime.aclose()
    assert len(sent) == 2
    assert [item for payload in sent for item in payload["state"]["catalog"]] == catalog
    assert json.loads(result.content)["selected_refs"] == [item["context_ref"] for item in catalog]
    assert result.usage["decision_calls"] == 2
    assert context == original


@pytest.mark.parametrize("missing_last_answer", [False, True])
async def test_jev_sends_twenty_four_questions_per_http_request_and_requires_every_answer(
    tmp_path, monkeypatch, missing_last_answer
):
    sent = []
    catalog = evidence_catalog(48, 200)

    def handler(request):
        assert request.url.host == "judge.test", "No primary model or tokenizer may run"
        payload = decision_payload(request)
        sent.append(payload)
        assert len(payload["questions"]) == len(payload["state"]["catalog"]) == 24
        assert len(json.dumps(payload, ensure_ascii=False)) <= 600000
        response = relevance_response(
            payload,
            lambda item: 0.9 if int(item["context_ref"].split("_")[1]) % 2 == 0 else 0.8,
        )
        if missing_last_answer and len(sent) == 2:
            data = response.json()
            data["answers"].pop("item_23")
            return httpx.Response(200, json=data)
        return response

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        operation = runtime.complete(
            "select_context",
            {"purpose": "核对付款限制及例外", "catalog": catalog},
            ContextSelection.model_json_schema(),
            "full-page",
        )
        if missing_last_answer:
            with pytest.raises(ModelCallFailed) as error:
                await operation
            assert error.value.code == "protocol"
            assert [p["status"] for p in error.value.diagnostics["selection_page_audit"]] == [
                "completed",
                "protocol",
            ]
            attempts = error.value.diagnostics["decision_http_attempts"]
            assert len(attempts) == len(sent) == 2
            assert [a["outcome"] for a in attempts] == ["success", "protocol"]
            assert len({a["request_id"] for a in attempts}) == 2
        else:
            result = await operation
            assert json.loads(result.content)["selected_refs"] == [
                item["context_ref"] for item in catalog[::2]
            ]
            assert result.usage["decision_calls"] == 2
            assert len(result.usage["decision_relevance"]) == 48
    finally:
        await runtime.aclose()
    assert len(sent) == 2
    assert [item for payload in sent for item in payload["state"]["catalog"]] == catalog


async def test_arbitrary_model_uses_same_question_batch_size(
    tmp_path, monkeypatch
):
    sent = []
    catalog = evidence_catalog(9, 200)

    def handler(request):
        assert request.url.path == "/v1/chat/completions", "Only the typed decision endpoint may be called"
        payload = decision_payload(request)
        assert payload["model"] == "luna-decisions"
        assert len(payload["questions"]) == len(payload["state"]["catalog"])
        assert len(payload["questions"]) <= 24
        assert len(json.dumps(payload, ensure_ascii=False)) <= 600000
        sent.append(payload)
        return relevance_response(payload)

    runtime = runtime_for(tmp_path, monkeypatch, handler, decision_model="luna-decisions")
    try:
        context = {"purpose": "检查付款", "catalog": catalog}
        measured = await runtime.measure(
            "select_context", context, ContextSelection.model_json_schema(), 48000
        )
        assert measured["selection_batches"] == 1 and not sent
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "luna-pages"
        )
    finally:
        await runtime.aclose()
    assert [len(payload["questions"]) for payload in sent] == [9]
    assert [item for payload in sent for item in payload["state"]["catalog"]] == catalog
    assert json.loads(result.content)["selected_refs"] == [item["context_ref"] for item in catalog]
    assert result.usage["decision_calls"] == 1


@pytest.mark.parametrize("mode", ["prompt", "json_schema"])
@pytest.mark.parametrize("token_limited", [False, True])
async def test_primary_selection_obeys_full_prompt_chars_and_actual_token_window(
    tmp_path, monkeypatch, mode, token_limited
):
    counted, sent = [], []
    # Leave room for the full English policy and output reservation while still
    # forcing the original catalog into multiple token-measured requests.
    window = 9000 if token_limited else 100000

    def tokens(messages):
        return sum(len(message["content"]) for message in messages) // 3 if token_limited else 1

    def handler(request):
        assert request.url.host == "primary.invalid", "Disabled Jev must stay unused"
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "primary-test", "max_model_len": window}]}
            )
        payload = decision_payload(request)
        if request.url.path == "/tokenize":
            counted.append(payload["messages"])
            return httpx.Response(200, json={"count": tokens(payload["messages"])})
        assert request.url.path == "/v1/chat/completions"
        sent.append(payload)
        messages = payload["messages"]
        assert sum(len(message["content"]) for message in messages) <= 600000
        assert tokens(messages) + payload["max_tokens"] + 512 <= window
        assert messages in counted, "The exact sent messages must have been token measured"
        assert "JSON Schema:\n" in messages[0]["content"]
        page = unpack_rows(json.loads(messages[1]["content"]))
        return completion({"selected_refs": [item["context_ref"] for item in page["catalog"]]})

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False, mode=mode)
    context = {"purpose": "仅选择付款业务的资料", "catalog": evidence_catalog(24, 2100)}
    try:
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "primary"
        )
    finally:
        await runtime.aclose()
    assert (len(sent) > 1 if token_limited else len(sent) == 1)
    assert result.usage["selection_provider"] == "llm"
    assert ContextSelection.model_validate_json(result.content).selected_refs == [
        item["context_ref"] for item in context["catalog"]
    ]
    delivered = [
        item for payload in sent
        for item in unpack_rows(json.loads(payload["messages"][1]["content"]))["catalog"]
    ]
    # A real small context window may split one source, but no source text is lost.
    for original in context["catalog"]:
        items = [item for item in delivered if item["context_ref"] == original["context_ref"]]
        assert items
        if items[0].get("fragment"):
            assert json.loads("".join(item["value"] for item in items)) == original["value"]
        else:
            assert items == [original]
    assert result.usage["selection_max_input_characters"] <= 600000


@pytest.mark.parametrize(
    "status,code",
    [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (500, "upstream")],
)
async def test_jev_failure_is_reported_without_primary_fallback(
    tmp_path, monkeypatch, status, code
):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "judge.test", (
            "Jev errors must not route to the primary model"
        )
        return httpx.Response(status, text="private-source-and-key-must-not-be-exposed")

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        with pytest.raises(ModelCallFailed) as error:
            await runtime.complete(
                "select_context",
                {"purpose": "付款", "catalog": evidence_catalog(1)},
                ContextSelection.model_json_schema(),
                "failure",
            )
    finally:
        await runtime.aclose()
    assert requests and error.value.code == code
    assert "private-source" not in str(error.value)
    assert "mock-secret" not in str(error.value)
    assert error.value.diagnostics["selection_page_audit"][0]["status"] == code


async def test_long_retry_after_code_preserved_without_early_retry_or_permanent_latch(
    tmp_path, monkeypatch
):
    requests = []

    def handler(request):
        assert request.url.host == "judge.test", "No primary fallback or tokenizer"
        requests.append(request)
        return httpx.Response(429, text="PRIVATE_SOURCE mock-secret", headers={"Retry-After": "60"})

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    codes = []
    try:
        for index in range(3):
            with pytest.raises(ModelCallFailed) as error:
                await runtime.complete(
                    "select_context",
                    {"purpose": "PRIVATE_SOURCE", "catalog": evidence_catalog(1)},
                    ContextSelection.model_json_schema(),
                    f"rate-limit-{index}",
                )
            codes.append(error.value.code)
            public = json.dumps(error.value.diagnostics) + str(error.value)
            assert "PRIVATE_SOURCE" not in public and "mock-secret" not in public
        assert codes == ["rate_limit", "rate_limit", "rate_limit"]
        assert len(requests) == 1
        assert runtime.material_selector.router.failures == 1
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("tail_probability", [0.8, math.nextafter(0.8, 1)])
async def test_long_evidence_fragments_select_full_original_in_downstream_workflow(
    tmp_path, monkeypatch, store, inputs, tail_probability
):
    sent = []

    def handler(request):
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        sent.append(payload)
        return relevance_response(
            payload, lambda item: tail_probability if "TAIL_RELEVANT" in item["value"] else 0.01
        )

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    evidence = {
        "evidence_id": "full_original",
        "source_id": "source_policy",
        "text": '完整原文\\路径\n"约束"' * 60000 + "TAIL_RELEVANT 未批准不得付款。",
    }
    context = {"section": {"section_id": "s1", "title": "付款"}, "evidence": [evidence]}
    original = deepcopy(context)
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs)
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    try:
        selected = await engine.select_evidence(context)
    finally:
        await runtime.aclose()
    fragments = [item for payload in sent for item in payload["state"]["catalog"]]
    assert len(fragments) > 1 and all(item["fragment"]["partial"] for item in fragments)
    assert all(item["context_ref"] == evidence["evidence_id"] for item in fragments)
    assert json.loads("".join(item["value"] for item in fragments)) == evidence
    assert selected["evidence"] == ([evidence] if tail_probability > 0.8 else [])
    assert all("fragment" not in value for value in selected["evidence"])
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT usage FROM tasks WHERE job_id=? AND task_key LIKE 'select_context:%'",
            (engine.job_id,),
        ).fetchall()
    audit = [item for row in rows for item in json.loads(row["usage"])["decision_relevance"]]
    assert len(audit) == len(fragments)
    assert {item["context_ref"] for item in audit} == {evidence["evidence_id"]}
    assert sum(item["accepted"] for item in audit) == int(tail_probability > 0.8)
    assert context == original


async def test_long_selected_risk_keeps_original_without_llm_summary(tmp_path, monkeypatch):
    sent = []

    def handler(request):
        assert request.url.host == "judge.test", (
            "Risk preservation must not request an LLM summary"
        )
        payload = decision_payload(request)
        sent.append(payload)
        return relevance_response(payload)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    risk = {
        "kind": "conflict",
        "description": "总行审批规则与分行规则冲突，尚未确认，不得默认由分行审批。" * 2200,
        "section_ids": ["s1"],
    }
    catalog, objects, views = dependency_catalog(
        [{"task_id": "t1", "title": "付款审批", "findings": [risk], "sections": []}]
    )
    try:
        result = await runtime.complete(
            "select_context",
            {"purpose": "审批规则", "catalog": catalog},
            ContextSelection.model_json_schema(),
            "risk",
        )
    finally:
        await runtime.aclose()
    selection = ContextSelection.model_validate_json(result.content)
    apply_selection(views, objects, selection)
    assert len(sent) >= 1 and selection.risk_refs == [catalog[0]["context_ref"]]
    assert len(selection.risk_summary) <= 1200
    brief = views[0]["risk_briefs"][0]
    assert brief["source_refs"] == selection.risk_refs
    assert brief["originals"] == [{"source_ref": catalog[0]["context_ref"], "value": risk}]


async def test_page_cannot_select_a_reference_from_another_page(tmp_path, monkeypatch):
    catalog = evidence_catalog(30)
    completions = []

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        payload = decision_payload(request)
        page = unpack_rows(json.loads(payload["messages"][1]["content"]))
        completions.append(page)
        assert "outer_29" not in {item["context_ref"] for item in page["catalog"]}
        return completion({"selected_refs": ["outer_29"]})

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False)
    runtime.config["max_input_tokens"] = 4000
    try:
        with pytest.raises(ValueError, match="declared kind"):
            await runtime.complete(
                "select_context",
                {"purpose": "付款", "catalog": catalog},
                ContextSelection.model_json_schema(),
                "wrong-page",
            )
    finally:
        await runtime.aclose()
    assert len(completions) == 1


async def test_intent_keeps_existing_uncertain_decision_fallback(tmp_path, monkeypatch):
    routes = []

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        routes.append(request.url.host)
        if request.url.host == "judge.test":
            payload = decision_payload(request)
            choices = payload["questions"]["intent"]["criteria"]
            probability = 1 / len(choices)
            return httpx.Response(
                200,
                json={
                    "answers": {
                        "intent": {
                            "type": "choice",
                            "choice": "chat",
                            "confidence": probability,
                            "probabilities": {choice: probability for choice in choices},
                        }
                    }
                },
            )
        assert request.url.path == "/v1/chat/completions"
        return completion({"intent": "chat", "summary": "继续讨论，不开始生成。"})

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    try:
        result = await runtime.complete(
            "intent",
            {"latest_user_message": "先讨论一下"},
            IntentDecision.model_json_schema(),
            "intent",
        )
    finally:
        await runtime.aclose()
    assert routes == ["judge.test", "primary.invalid"]
    assert IntentDecision.model_validate_json(result.content).intent == "chat"
    assert result.usage["decision_fallback"] == "ValueError"


def capacity_rejection(code):
    if code == "context_limit":
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "maximum context length is 6000 tokens; prompt contains 4500 input tokens",
                }
            },
        )
    chunk = {
        "choices": [
            {
                "delta": {"content": '{"selected_refs":["SHOULD_NOT_SURVIVE"]'},
                "finish_reason": "length",
            }
        ]
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize("code", ["context_limit", "length_limit"])
async def test_actual_service_limits_split_same_backend_without_repeating_input(
    tmp_path, monkeypatch, code
):
    sent = []

    def handler(request):
        assert request.url.host == "primary.invalid"
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        payload = decision_payload(request)
        assert request.url.path == "/v1/chat/completions"
        assert sum(len(message["content"]) for message in payload["messages"]) <= 600000
        assert payload["messages"] not in [previous["messages"] for previous in sent]
        sent.append(payload)
        page = unpack_rows(json.loads(payload["messages"][1]["content"]))
        if len(page["catalog"]) > 2:
            return capacity_rejection(code)
        return completion({"selected_refs": [item["context_ref"] for item in page["catalog"]]})

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False)
    context = {"purpose": "付款", "catalog": evidence_catalog(8, 200)}
    try:
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "recover"
        )
    finally:
        await runtime.aclose()
    assert len(sent) == 7
    assert ContextSelection.model_validate_json(result.content).selected_refs == [
        item["context_ref"] for item in context["catalog"]
    ]
    assert result.usage["selection_batches"] == 4 and result.usage["selection_attempts"] == 7
    audit = result.usage["selection_page_audit"]
    assert [entry["status"] for entry in audit].count(code) == 3
    by_path = {entry["page"]: entry for entry in audit}
    for entry in audit:
        assert entry["depth"] <= 3 and entry["characters"] <= 600000
        if "." in entry["page"]:
            assert entry["characters"] < by_path[entry["page"].rsplit(".", 1)[0]]["characters"]
    assert "规" * 30 not in json.dumps(audit, ensure_ascii=False)


async def test_rejected_long_fragments_can_split_again_without_losing_original_text(
    tmp_path, monkeypatch
):
    successful, requests = [], []

    def handler(request):
        assert request.url.host == "primary.invalid"
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        payload = decision_payload(request)
        assert sum(len(message["content"]) for message in payload["messages"]) <= 600000
        assert payload["messages"] not in requests
        requests.append(payload["messages"])
        page = unpack_rows(json.loads(payload["messages"][1]["content"]))
        item = page["catalog"][0]
        if not item.get("fragment") or len(item["value"]) > 7000:
            return capacity_rejection("context_limit")
        successful.append(item)
        return completion(
            {"selected_refs": [item["context_ref"]] if "RELEVANT_END" in item["value"] else []}
        )

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False)
    value = {"evidence_id": "e1", "source_id": "s1", "text": "约束条款。" * 10000 + "RELEVANT_END"}
    context = {
        "purpose": "付款约束",
        "catalog": [{"context_ref": "outer", "kind": "evidence", "value": value}],
    }
    try:
        result = await runtime.complete(
            "select_context", context, ContextSelection.model_json_schema(), "fragment-retry"
        )
    finally:
        await runtime.aclose()
    assert len(successful) > 2
    assert any(item["fragment"].get("parent") for item in successful)
    assert all(item["context_ref"] == "outer" for item in successful)
    assert json.loads("".join(item["value"] for item in successful)) == value
    assert ContextSelection.model_validate_json(result.content).selected_refs == ["outer"]


async def test_failed_remaining_child_never_returns_partial_success_and_stops_at_depth_limit(
    tmp_path, monkeypatch
):
    completed, rejected = [], []

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        payload = decision_payload(request)
        page = unpack_rows(json.loads(payload["messages"][1]["content"]))
        refs = [item["context_ref"] for item in page["catalog"]]
        if refs == ["good"]:
            completed.append(payload["messages"])
            return completion({"selected_refs": refs})
        assert payload["messages"] not in rejected
        rejected.append(payload["messages"])
        return capacity_rejection("length_limit")

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False)
    context = {
        "purpose": "付款",
        "catalog": [
            {"context_ref": "good", "kind": "evidence", "value": "可确认的相关材料"},
            {"context_ref": "bad", "kind": "evidence", "value": "尚不能完成的材料" * 2500},
        ],
    }
    try:
        with pytest.raises(ModelCallFailed) as error:
            await runtime.complete(
                "select_context", context, ContextSelection.model_json_schema(), "exhausted"
            )
    finally:
        await runtime.aclose()
    assert completed and error.value.code == "length_limit"
    assert len(rejected) == 4
    audit = error.value.diagnostics["selection_page_audit"]
    assert max(entry["depth"] for entry in audit) == 3
    assert any(entry["status"] == "completed" for entry in audit)


async def test_other_decision_failures_do_not_disable_strict_material_selector(
    tmp_path, monkeypatch
):
    sent = []

    def handler(request):
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        sent.append(payload)
        return relevance_response(payload)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    runtime.decision_router.failures = 2
    try:
        result = await runtime.complete(
            "select_context",
            {"purpose": "付款", "catalog": evidence_catalog(1)},
            ContextSelection.model_json_schema(),
            "isolated",
        )
    finally:
        await runtime.aclose()
    assert len(sent) == 1 and json.loads(result.content)["selected_refs"] == ["outer_0"]
    assert runtime.decision_router.failures == 2


async def test_primary_selector_keeps_large_complete_catalog_within_token_budget(tmp_path, monkeypatch):
    sent = []
    catalog = evidence_catalog(30, 1)
    for item in catalog:
        item["value"]["text"] = "The source explains an applicable business requirement. " * 500

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        payload = decision_payload(request)
        page = unpack_rows(json.loads(payload["messages"][1]["content"]))
        sent.append(page)
        assert page["catalog"] == catalog
        assert sum(len(message["content"]) for message in payload["messages"]) > 600000
        return completion({"selected_refs": [item["context_ref"] for item in catalog]})

    runtime = runtime_for(tmp_path, monkeypatch, handler, enabled=False)
    try:
        result = await runtime.complete("select_context", {"purpose": "完整核对", "catalog": catalog},
                                        ContextSelection.model_json_schema(), "large-context")
    finally:
        await runtime.aclose()
    assert len(sent) == 1
    assert result.usage["selection_batches"] == 1
    assert "selection_character_limit" not in result.usage
