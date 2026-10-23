"""Mandatory original evidence is read intact without redundant relevance calls."""

import json
from copy import deepcopy

import pytest
from decision_wire import payload as decision_payload
from test_material_selection import relevance_response, runtime_for
from test_request_context import engine_for, evidence

from folio.models import Finding
from folio.orchestration.context import ContextSelection, gap_material_catalog, project
from folio.orchestration.focused_review import material_parts


def issue_source(citation="block"):
    original = evidence(1, "正向规则。" * 6500 + "例外：虚拟交易不得触发清算核算。")
    finding = Finding(
        kind="conflict",
        description="原文e1的结算规则存在冲突" if citation == "description" else "结算规则存在冲突",
        section_ids=["section"],
        block_refs=[{"section_id": "section", "block_id": "b1"}],
        evidence_ids=["e1"] if citation == "finding" else [],
        key="settlement",
    )
    return {
        "finding": finding.model_dump(),
        "sections": [
            {
                "section_id": "section",
                "revision": 1,
                "content": {
                    "blocks": [
                        {
                            "block_id": "b1",
                            "text": "虚拟交易不触发清算核算。",
                            "evidence_ids": ["e1"] if citation == "block" else [],
                        }
                    ]
                },
                "evidence": [
                    original,
                    evidence(2, "反例：实际成交交易必须触发清算。"),
                    evidence(3, "相关性不足的其他材料。"),
                ],
            }
        ],
        "definitions": [{"section_id": "section", "title": "结算规则"}],
        "business_contexts": [],
    }


def original_evidence(view):
    values, fragments = {}, {}
    for item in view["original_materials"]:
        if item["kind"] != "evidence":
            continue
        value = item["value"]
        if "serialized_fragment" in value:
            fragments.setdefault(value["parent_ref"], []).append(value)
        else:
            values[value["evidence_id"]] = value
    for parts in fragments.values():
        parts.sort(key=lambda part: part["start"])
        assert parts[0]["start"] == 0
        assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:], strict=False))
        assert parts[-1]["end"] == parts[-1]["total_characters"]
        value = json.loads("".join(part["serialized_fragment"] for part in parts))
        values[value["evidence_id"]] = value
    return values


@pytest.mark.parametrize("citation", ["block", "finding", "description"])
async def test_strict_jev_only_scores_optional_sources_and_keeps_cited_original_tail(
    store, inputs, tmp_path, monkeypatch, citation
):
    requests = []

    def handler(request):
        # No main-model generation, tokenizer or fallback request is permitted.
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        requests.append(payload)
        assert (
            len(json.dumps({k: payload[k] for k in ("state", "questions")}, ensure_ascii=False))
            <= 32000
        )
        assert all(item["value"]["evidence_id"] != "e1" for item in payload["state"]["catalog"])
        return relevance_response(
            payload, lambda item: 0.9 if item["value"]["evidence_id"] == "e2" else 0.8
        )

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    # Exercise page packing with an explicitly small decision-service budget;
    # the application default now admits this complete catalog in one request.
    monkeypatch.setattr("folio.decisions.selection.DECISION_CONTEXT_CHARS", 32000)
    engine = engine_for(store, inputs, runtime)
    source = issue_source(citation)
    before = deepcopy(source)
    try:
        view = await engine.prepare_context("drill_down", source)
        assert len(requests) == 1
        values = original_evidence(view)
        assert set(values) == {"e1", "e2"}
        assert values["e1"]["text"] == source["sections"][0]["evidence"][0]["text"]
        assert values["e1"]["text"].endswith("例外：虚拟交易不得触发清算核算。")
        assert "实际成交交易必须触发清算" in values["e2"]["text"]
        assert view["sections"][0]["content"] == source["sections"][0]["content"]
        assert source == before

        # Replay the previous candidate set through the REAL page planner only.
        # Including the guaranteed source would have spent additional 32k pages.
        projected = project("drill_down", source)
        full_catalog = [
            part
            for item in gap_material_catalog({"evidence": projected["sections"][0]["evidence"]})
            for part in material_parts(item)
        ]
        prior_context = requests[0]["state"] | {"catalog": full_catalog}
        pages = await runtime.material_selector.prepare(
            prior_context, ContextSelection.model_json_schema()
        )
        assert len(pages) > len(requests)
        assert all(measured["characters"] <= 32000 for _, _, measured in pages)
        # Existing exact request caching still applies on a subsequent issue read.
        assert await engine.prepare_context("drill_down", source) == view
        assert len(requests) == 1
    finally:
        await runtime.aclose()


async def test_all_cited_sources_need_no_selector_and_fact_selected_sources_still_close_over_evidence(
    store, inputs, tmp_path, monkeypatch
):
    requests = []

    def handler(request):
        assert request.url.host == "judge.test"
        payload = decision_payload(request)
        requests.append(payload)
        assert all(item["value"].get("evidence_id") != "e1" for item in payload["state"]["catalog"])
        return relevance_response(payload, lambda item: 0.9 if item["kind"] == "fact" else 0.0)

    runtime = runtime_for(tmp_path, monkeypatch, handler)
    engine = engine_for(store, inputs, runtime)
    source = issue_source()
    source["sections"][0]["evidence"] = source["sections"][0]["evidence"][:1]
    try:
        view = await engine.prepare_context("drill_down", source)
        assert not requests
        assert set(original_evidence(view)) == {"e1"}
        source["sections"][0]["evidence"].append(
            evidence(2, "事实所引用的原文：不能省略否定约束。")
        )
        source["business_contexts"] = [
            {
                "business_record": {
                    "facts": [{"fact_id": "f2", "statement": "不得清算", "evidence_ids": ["e2"]}]
                }
            }
        ]
        view = await engine.prepare_context("drill_down", source)
        assert len(requests) == 1
        assert set(original_evidence(view)) == {"e1", "e2"}
        assert any(item["kind"] == "fact" for item in view["original_materials"])
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("optional_count", [1, 8])
@pytest.mark.parametrize("prompt_limit", [None, 45000])
async def test_optional_catalog_selection_depends_on_measured_capacity(
    store, inputs, optional_count, prompt_limit
):
    from folio.models import DrillDown
    from folio.runtime.base import RuntimeResult
    from folio.runtime.demo import DemoRuntime

    class WideWindow(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def measure(self, task, context, schema, max_chars):
            size = len(json.dumps(context, ensure_ascii=False))
            return {"characters": size, "fits": task != "drill_down"
                    or prompt_limit is None or size <= prompt_limit}

        async def complete(self, task, context, schema, session):
            self.calls.append((task, context))
            if task == "select_context":
                assert not any(i["value"].get("evidence_id") == "e1" for i in context["catalog"])
                return RuntimeResult(
                    json.dumps(
                        {
                            "selected_refs": [
                                i["context_ref"]
                                for i in context["catalog"]
                                if i["value"].get("evidence_id") == "e2"
                            ],
                            "risk_refs": [],
                            "risk_summary": "",
                        }
                    )
                )
            assert task == "drill_down"
            return RuntimeResult(
                json.dumps({"confirmed": False, "reason": "核对了原文例外和反例。"})
            )

    runtime = WideWindow()
    engine = engine_for(store, inputs, runtime)
    source = issue_source()
    original = source["sections"][0]["evidence"][0]
    source["sections"][0]["evidence"] = [original] + [
        evidence(i, ("有关业务反例。" if i == 2 else "其他领域资料。") * 660)
        for i in range(2, optional_count + 2)
    ]
    before = deepcopy(source)
    await engine.call("drill_down", source, DrillDown)
    selections = [c for task, c in runtime.calls if task == "select_context"]
    selected = prompt_limit is not None and optional_count > 1
    assert bool(selections) == selected
    final = [c for task, c in runtime.calls if task == "drill_down"][-1]
    originals = original_evidence(final)
    assert set(originals) == ({"e1", "e2"} if selected else
                             {f"e{i}" for i in range(1, optional_count + 2)})
    assert originals["e1"]["text"] == original["text"]
    assert originals["e1"]["text"].endswith("例外：虚拟交易不得触发清算核算。")
    assert source == before
