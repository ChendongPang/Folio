"""Inner source IDs can only recover an unambiguous outer reference on the same page."""

import json

import pytest

from folio.models import JobInput, Limits
from folio.orchestration.context import ContextSelection
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.errors import TaskRetriesExhausted


def engine_for(store, inputs, runtime):
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, limits=Limits(call_attempts=2))
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    return engine


def item(ref, kind, **value):
    return {"context_ref": ref, "kind": kind, "value": value}


def test_unique_inner_ids_recover_without_merging_sources_or_risk_kinds(store, inputs):
    engine = engine_for(store, inputs, object())
    catalog = [
        item("outer_e", "evidence", evidence_id="inner_e"),
        item("outer_f", "fact", fact_id="inner_f"),
        item("outer_p", "proposal", gap_id="inner_p"),
        item("outer_r", "risk", evidence_id="inner_r"),
    ]
    selected = ContextSelection(
        selected_refs=["inner_e", "outer_e", "inner_f", "inner_p", "inner_f"],
        risk_refs=["outer_r"],
        risk_summary="保留原有风险",
    )
    engine.check_result(selected, {"catalog": catalog})
    assert selected.selected_refs == ["outer_e", "outer_f", "outer_p"]
    assert selected.risk_refs == ["outer_r"] and selected.risk_summary == "保留原有风险"
    for fields in (
        {"selected_refs": ["inner_r"]},
        {"selected_refs": ["outer_r"]},
        {"risk_refs": ["inner_e"], "risk_summary": "不能把证据升为风险"},
        {"selected_refs": ["unknown"]},
    ):
        with pytest.raises(ValueError, match="declared kind"):
            engine.check_result(ContextSelection(**fields), {"catalog": catalog})


@pytest.mark.parametrize("case", ["duplicate_source", "cross_kind_collision", "outer_wrong_kind"])
def test_ambiguous_ids_and_known_wrong_kind_outer_refs_remain_invalid(store, inputs, case):
    engine = engine_for(store, inputs, object())
    catalog = [item("outer_e", "evidence", evidence_id="shared")]
    if case == "duplicate_source":
        catalog.append(item("outer_other", "evidence", evidence_id="shared"))
    elif case == "cross_kind_collision":
        catalog.append(item("outer_f", "fact", fact_id="shared"))
    else:
        catalog.append(item("shared", "risk", text="已知风险编号不能改映射为证据"))
    with pytest.raises(ValueError, match="declared kind"):
        engine.check_result(ContextSelection(selected_refs=["shared"]), {"catalog": catalog})


class PageRuntime:
    def __init__(self):
        self.calls = []
        self.correct = False
        self.corrected_failure = False

    async def complete(self, task, context, schema, session):
        self.calls.append(context)
        first = context["catalog"][0]
        if first["context_ref"].startswith("bad") and (not self.correct or self.corrected_failure):
            refs = ["unknown"]
        else:
            refs = [first["value"]["evidence_id"]]
        return RuntimeResult(
            json.dumps({"selected_refs": refs, "risk_refs": [], "risk_summary": ""})
        )


async def test_only_exhausted_page_gets_one_corrected_identity_and_success_cache_is_reused(
    store, inputs
):
    runtime = PageRuntime()
    engine = engine_for(store, inputs, runtime)
    good = [item(f"good{i}", "evidence", evidence_id=f"e{i}") for i in range(12)]
    bad = [item(f"bad{i}", "evidence", evidence_id=f"b{i}") for i in range(13)]
    purpose = {"action": "read"}
    await engine._call("select_context", {"catalog": good, "purpose": purpose}, ContextSelection)
    with pytest.raises(ValueError, match="declared kind"):
        await engine._call("select_context", {"catalog": bad, "purpose": purpose}, ContextSelection)
    with store.connect() as conn:
        before = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tasks WHERE job_id=? ORDER BY task_key", (engine.job_id,)
            )
        ]
    assert len(runtime.calls) == 3 and len(before) == 2
    old_failure = next(row for row in before if row["error"])
    runtime.correct = True
    selections = await engine.select_reads(purpose, good + bad)
    assert [s.selected_refs for s in selections] == [["good0"], ["bad0"]]
    assert len(runtime.calls) == 4
    assert runtime.calls[-1]["selection_reference_retry"]
    with store.connect() as conn:
        after = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tasks WHERE job_id=? ORDER BY task_key", (engine.job_id,)
            )
        ]
    assert len(after) == 3 and all(row in after for row in before)
    assert store.task(engine.job_id, old_failure["task_key"]) == old_failure
    assert store.get(engine.job_id)["calls"] == 4
    await engine.select_reads(purpose, good + bad)
    assert len(runtime.calls) == 4  # Both the old success and corrected page are cached.


async def test_corrected_page_failure_stops_without_another_retry_identity(store, inputs):
    runtime = PageRuntime()
    engine = engine_for(store, inputs, runtime)
    catalog = [item("bad0", "evidence", evidence_id="inner")]
    context = {"purpose": {"action": "read"}, "catalog": catalog}
    with pytest.raises(ValueError, match="declared kind"):
        await engine._call("select_context", context, ContextSelection)
    runtime.corrected_failure = True
    with pytest.raises(ValueError, match="declared kind"):
        await engine.select_reads(context["purpose"], catalog)
    with pytest.raises(TaskRetriesExhausted) as caught:
        await engine.select_reads(context["purpose"], catalog)
    assert caught.value.last_error.startswith("ValueError: Context selection")
    assert len(runtime.calls) == store.get(engine.job_id)["calls"] == 4


async def test_other_exhausted_errors_do_not_get_selection_reference_retry(store, inputs):
    class Unavailable:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            raise RuntimeError("provider unavailable")

    runtime = Unavailable()
    engine = engine_for(store, inputs, runtime)
    catalog = [item("outer", "evidence", evidence_id="inner")]
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await engine.select_reads({}, catalog)
    with pytest.raises(TaskRetriesExhausted) as caught:
        await engine.select_reads({}, catalog)
    assert caught.value.last_error == "RuntimeError: provider unavailable"
    assert caught.value.task_key.startswith("select_context:")
    assert len([e for e in store.events(engine.job_id) if e["kind"] == "task_started"]) == 2
    assert runtime.calls == 2


async def test_actual_selection_pages_have_independent_archives_and_reusable_results(
    store, inputs, tmp_path, monkeypatch
):
    from folio.model_settings import probe_fingerprint
    from folio.orchestration.context import apply_selection, dependency_catalog
    from folio.runtime.llm import LLMRuntime

    config = {
        "base_url": "http://unused.invalid/v1",
        "model": "unused",
        "no_auth": True,
        "decision_enabled": True,
        "decision_base_url": "https://judge.test/v1", "decision_api_key": "mock-secret",
    }
    config["verified_capabilities"] = {"decision": probe_fingerprint(config, "decision")}
    config_path = tmp_path / "model.json"
    config_path.write_text(json.dumps(config))
    calls = []

    # Explicitly use a small decision-service budget to exercise real paging;
    # production now allows 600K characters before this compatibility fallback.
    monkeypatch.setattr("folio.decisions.selection.DECISION_CONTEXT_CHARS", 32000)
    monkeypatch.setattr("folio.decisions.routing.DECISION_CONTEXT_CHARS", 32000)

    async def evaluate(config, state, questions):
        assert (
            len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False)) <= 32000
        )
        calls.append(state)
        return {"answers": {name: {"noul": 0.9} for name in questions}, "model": "jev-test"}

    async def no_primary(*args, **kwargs):
        pytest.fail("Material pages must not contact the main model")

    monkeypatch.setattr("folio.decision_client.evaluate", evaluate)
    runtime = LLMRuntime(config_path)
    runtime.client.request = no_primary
    engine = engine_for(store, inputs, runtime)
    risk = {"kind": "conflict", "description": "必须保留的否定约束。" * 9000, "section_ids": ["s"]}
    catalog, objects, views = dependency_catalog(
        [{"task_id": "t", "title": "审批", "findings": [risk], "sections": []}]
    )
    try:
        selections = await engine.select_reads({"question": "审批限制是什么？"}, catalog)
        assert len(calls) > 1
        assert len(selections) == 1
        apply_selection(views, objects, selections[0])
        assert len(views[0]["risk_briefs"]) == 1
        assert views[0]["risk_briefs"][0]["originals"] == [
            {"source_ref": catalog[0]["context_ref"], "value": risk}
        ]
        request_dir = store.root / "jobs" / engine.job_id / "contexts" / "requests"
        archives = [json.loads(path.read_text()) for path in request_dir.glob("*.json")]
        assert len(archives) == len(calls)
        assert all(len(a["context"]["catalog"]) == 1 for a in archives)
        assert all(a["capacity"]["selection_batches"] == 1 for a in archives)
        with store.connect() as conn:
            tasks = conn.execute(
                "SELECT result FROM tasks WHERE job_id=?", (engine.job_id,)
            ).fetchall()
        assert len(tasks) == len(calls) and all(row["result"] for row in tasks)
        previous = len(calls)
        await engine.select_reads({"question": "审批限制是什么？"}, catalog)
        assert len(calls) == previous
    finally:
        await runtime.aclose()
