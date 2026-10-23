import importlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from folio.models import DocumentPlan, JobInput, Section
from folio.store import Store


@pytest.mark.parametrize("intent", ["generate", "chat"])
async def test_replay_reserves_once_and_runs_real_order_without_inventing_success(
    tmp_path, inputs, monkeypatch, intent
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    runner = importlib.import_module("replay_audited")
    root = tmp_path / "source"
    source_store = Store(root)
    plan = DocumentPlan(
        sections=[
            Section(
                section_id="one",
                node_id="n",
                title="交易",
                level=1,
                display_number="1",
                instruction="交易规则",
            )
        ]
    )
    source_job = source_store.create(
        JobInput(**(inputs | {"user_request": "根据已上传资料生成文档。"}), runtime="llm"),
        plan=plan,
    )
    config = root / "dummy.json"
    config.write_text("{}")
    monkeypatch.setattr(runner, "resolve_model_config", lambda *_: config)
    output = tmp_path / "replay"
    output.mkdir()
    (output / "ledger.json").write_text(
        json.dumps(
            {
                "source_job": source_job,
                "max_full_runs": 1,
                "max_optimization_rounds": 1,
                "optimization_rounds": [{"round": 1}],
                "full_runs": [],
            }
        )
    )
    steps = []

    async def detect(*args):
        steps.append("intent")
        return SimpleNamespace(intent=intent, model_dump=lambda: {"intent": intent})

    class Drafts:
        def __init__(self, *args):
            pass

        async def create(self, *args, **kwargs):
            steps.append("directory")
            assert kwargs["title"] == "口行需求说明书"
            assert kwargs["source_catalog"][0]["name"] == "rules.md"
            return {"revision": "r", "sections": [s.model_dump() for s in plan.sections]}

        def load(self, *args):
            return {"plan": plan, "folder": tmp_path}

    async def execute(store, job_id, config):
        steps.append("body")
        assert store.get(job_id)["plan"] == plan.model_dump()
        store.update(job_id, status="failed", stage="review", error="test endpoint unavailable")

    monkeypatch.setattr(runner, "detect_intent", detect)
    monkeypatch.setattr(runner, "DirectoryDrafts", Drafts)
    monkeypatch.setattr(runner, "execute", execute)
    args = Namespace(
        root=root,
        source_job=source_job,
        from_uploads=True,
        title="口行需求说明书",
        generation_timeout=900,
        parallel=16,
        output_root=output,
        timeout=1200,
    )
    result = await runner.run(args)
    assert steps == (["intent", "directory", "body"] if intent == "generate" else ["intent"])
    assert not result["passed"]
    assert result["code_unchanged"] and result["source_configuration_unchanged"]
    assert not result.get("valid_docx")
    with pytest.raises(ValueError, match="full-run limit"):
        await runner.run(args)
    assert len(json.loads((output / "ledger.json").read_text())["full_runs"]) == 1


@pytest.fixture
def replay_case(tmp_path, inputs, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    runner = importlib.import_module("replay_audited")
    root = tmp_path / "source"
    source = Store(root)
    plan = DocumentPlan(
        sections=[
            Section(
                section_id="one",
                node_id="n",
                title="交易",
                level=1,
                display_number="1",
                instruction="交易规则",
            )
        ]
    )
    job_id = source.create(JobInput(**inputs, runtime="llm"), plan=plan)
    config = root / "dummy.json"
    config.write_text("{}")
    monkeypatch.setattr(runner, "resolve_model_config", lambda *_: config)
    monkeypatch.setattr(runner, "code_digest", lambda: "stable-code")
    output = tmp_path / "replay"
    output.mkdir()
    (output / "ledger.json").write_text(
        json.dumps(
            {
                "source_job": job_id,
                "max_full_runs": 1,
                "max_optimization_rounds": 1,
                "optimization_rounds": [{"round": 1}],
                "full_runs": [],
            }
        )
    )
    steps = []

    async def execute(store, current_job, config):
        steps.append("execute")
        store.update(current_job, status="draft", stage="complete")

    monkeypatch.setattr(runner, "execute", execute)
    # This fixture isolates harness finalization, not document quality validation.
    monkeypatch.setattr(runner, "inspect_artifact", lambda *_, **__: {"complete_delivery": True})
    args = Namespace(
        root=root,
        source_job=job_id,
        from_uploads=False,
        title="口行需求说明书",
        generation_timeout=900,
        parallel=16,
        output_root=output,
        timeout=1200,
    )
    return runner, args, steps


@pytest.mark.parametrize(
    "failure_point", ["initial_digest", "initial_report", "store_setup", "job_create"]
)
async def test_reserved_replay_setup_failures_finalize_as_consumed_failed_attempts(
    replay_case, monkeypatch, failure_point
):
    runner, args, steps = replay_case

    def fail(*args, **kwargs):
        raise OSError("PRIVATE_ERROR_DETAIL")

    if failure_point == "initial_digest":
        monkeypatch.setattr(runner, "code_digest", fail)
    elif failure_point == "store_setup":
        monkeypatch.setattr(runner, "Store", fail)
    elif failure_point == "job_create":
        monkeypatch.setattr(runner.Store, "create", fail)
    else:
        original = runner.write_json
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                fail()
            return original(*args, **kwargs)

        monkeypatch.setattr(runner, "write_json", fail_once)
    report = await runner.run(args)
    assert not steps and report["exception_type"] == "OSError"
    assert report["status"] == "failed" and not report["passed"]
    written = json.loads((args.output_root / "run-01" / "result.json").read_text())
    ledger = json.loads((args.output_root / "ledger.json").read_text())
    assert written == report
    assert len(ledger["full_runs"]) == 1 and ledger["full_runs"][0]["status"] == "failed"
    assert "PRIVATE_ERROR_DETAIL" not in json.dumps(report)
    with pytest.raises(ValueError, match="full-run limit"):
        await runner.run(args)


@pytest.mark.parametrize(
    "failure_point",
    ["verify_source", "inspect_artifact", "summarize_events", "write_result", "finish_ledger"],
)
async def test_final_audit_or_persistence_failure_cannot_become_a_success(
    replay_case, monkeypatch, failure_point
):
    runner, args, steps = replay_case
    calls = 0

    def fail(*args, **kwargs):
        raise OSError("PRIVATE_ERROR_DETAIL")

    if failure_point == "verify_source":
        original = runner.source_signature

        def recheck(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs) if calls == 1 else fail()

        monkeypatch.setattr(runner, "source_signature", recheck)
    elif failure_point in {"inspect_artifact", "summarize_events"}:
        monkeypatch.setattr(runner, failure_point, fail)
    elif failure_point == "write_result":
        original = runner.write_json

        def fail_final_once(path, value):
            nonlocal calls
            if path.name == "result.json":
                calls += 1
                if calls == 4:
                    fail()
            return original(path, value)

        monkeypatch.setattr(runner, "write_json", fail_final_once)
    else:
        original = runner.finish_ledger

        def fail_final_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            return fail() if calls == 1 else original(*args, **kwargs)

        monkeypatch.setattr(runner, "finish_ledger", fail_final_once)
    report = await runner.run(args)
    assert steps == ["execute"]
    assert report["status"] == "failed" and not report["passed"] and not report["audit_complete"]
    assert any(row["stage"] == failure_point for row in report["audit_errors"])
    written = json.loads((args.output_root / "run-01" / "result.json").read_text())
    ledger = json.loads((args.output_root / "ledger.json").read_text())
    assert written == report
    assert len(ledger["full_runs"]) == 1 and ledger["full_runs"][0]["status"] == "failed"
    assert "PRIVATE_ERROR_DETAIL" not in json.dumps(report)
    if failure_point == "verify_source":
        monkeypatch.setattr(runner, "source_signature", original)
    with pytest.raises(ValueError, match="full-run limit"):
        await runner.run(args)
    assert steps == ["execute"]


async def test_original_execution_exception_survives_secondary_finalization_errors(
    replay_case, monkeypatch
):
    from folio.runtime.errors import ModelCallFailed

    runner, args, steps = replay_case

    async def failed_execution(*args):
        steps.append("execute")
        raise ModelCallFailed("PRIVATE_PROVIDER_BODY", "timeout")

    monkeypatch.setattr(runner, "execute", failed_execution)
    original = runner.source_signature
    calls = 0

    def failed_recheck(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise FileNotFoundError("PRIVATE_SOURCE_PATH")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "source_signature", failed_recheck)
    report = await runner.run(args)
    assert report["exception_type"] == "ModelCallFailed" and report["exception_code"] == "timeout"
    assert report["audit_errors"][0] == {
        "stage": "verify_source",
        "exception_type": "FileNotFoundError",
    }
    assert report["status"] == "failed" and not report["passed"]
    assert "PRIVATE" not in json.dumps(report)
