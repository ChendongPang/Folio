"""Transport failures must not be retried as directory validation corrections."""

import asyncio
import json

import pytest

from folio.document.prepare import prepare_with_model
from folio.models import Limits
from folio.research.planning import _limits
from folio.runtime.base import RuntimeResult
from folio.runtime.errors import ModelCallBudgetExceeded, ModelCallFailed


async def prepare(inputs, tmp_path, runtime, *, timeout=1, progress=None):
    return await prepare_with_model(
        inputs["user_request"],
        [],
        tmp_path / "result",
        runtime=runtime,
        limits=Limits(call_timeout_seconds=timeout, call_attempts=3),
        on_progress=progress.append if progress is not None else None,
    )


def attempts(tmp_path):
    return json.loads((tmp_path / "result/planning-attempts.json").read_text())


def test_directory_research_default_budget_is_thirty_minutes_and_eighty_calls():
    limits = _limits(Limits())
    assert limits.max_seconds == 30 * 60
    assert limits.max_calls == 80
    assert limits.call_timeout_seconds == 900
    assert limits.max_tool_calls == 120


@pytest.mark.parametrize(
    "error",
    [
        ModelCallFailed("No first content", "timeout"),
        ModelCallFailed("Access denied", "authentication"),
        ModelCallFailed("Rate limited", "rate_limit"),
        ModelCallFailed("Invalid upstream protocol", "protocol"),
        RuntimeError("Connection failed"),
        ValueError("Malformed runtime envelope, no plan received"),
    ],
)
async def test_runtime_failure_is_not_validation_retry(inputs, tmp_path, error):
    error.diagnostics = {"phase": "first_content", "received_chars": 0, "elapsed_seconds": 1.2}

    class Runtime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            raise error

    runtime, progress = Runtime(), []
    with pytest.raises(type(error)) as caught:
        await prepare(inputs, tmp_path, runtime, progress=progress)
    assert caught.value is error
    assert runtime.calls == 1 and len(attempts(tmp_path)) == 1
    assert attempts(tmp_path)[0]["diagnostics"] == error.diagnostics
    assert not any("第 2 轮" in text or "未通过检查" in text for text in progress)
    assert not (tmp_path / "result/planning-proposal.json").exists()


async def test_outer_deadline_preserves_cancelled_runtime_diagnostics(inputs, tmp_path):
    class Runtime:
        calls = 0
        cancelled = False

        async def complete(self, *args):
            self.calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError as exc:
                self.cancelled = True
                exc.diagnostics = {
                    "phase": "streaming_content",
                    "received_chars": 240,
                    "reasoning_chars": 9,
                }
                raise

    runtime, progress = Runtime(), []
    with pytest.raises(ModelCallFailed) as caught:
        await prepare(inputs, tmp_path, runtime, timeout=0.01, progress=progress)
    assert caught.value.code == "timeout" and runtime.cancelled
    assert runtime.calls == 1 and len(attempts(tmp_path)) == 1
    record = attempts(tmp_path)[0]
    assert record["error_code"] == "timeout"
    assert record["diagnostics"]["phase"] == "streaming_content"
    assert record["diagnostics"]["received_chars"] == 240
    assert record["diagnostics"]["reasoning_chars"] == 9
    assert record["diagnostics"]["timeout_seconds"] == 0.01
    assert record["diagnostics"]["deadline_source"] == "directory_call"
    assert not any("第 2 轮" in text or "未通过检查" in text for text in progress)


async def test_raw_timeout_has_nonempty_typed_diagnostics(inputs, tmp_path):
    class Runtime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            raise TimeoutError()

    runtime = Runtime()
    with pytest.raises(ModelCallFailed) as caught:
        await prepare(inputs, tmp_path, runtime)
    assert caught.value.code == "timeout" and runtime.calls == 1
    record = attempts(tmp_path)[0]
    assert record["error"] == "ModelCallFailed: Directory planning call timed out"
    assert record["diagnostics"]["elapsed_seconds"] >= 0


async def test_user_cancellation_is_not_converted_to_timeout_or_retried(inputs, tmp_path):
    class Runtime:
        started = asyncio.Event()
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            self.started.set()
            await asyncio.sleep(10)

    runtime = Runtime()
    task = asyncio.create_task(prepare(inputs, tmp_path, runtime))
    await runtime.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.calls == 1 and len(attempts(tmp_path)) == 1
    assert "error_code" not in attempts(tmp_path)[0]


async def test_incomplete_response_is_protocol_failure_not_empty_validation(inputs, tmp_path):
    class Runtime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            return RuntimeResult("", stop_reason="failed")

    runtime = Runtime()
    with pytest.raises(ModelCallFailed) as caught:
        await prepare(inputs, tmp_path, runtime)
    assert runtime.calls == 1 and caught.value.code == "protocol"
    assert attempts(tmp_path)[0]["error_code"] == "protocol"


@pytest.mark.parametrize("maximum", [1, 3])
async def test_research_and_planning_share_total_call_allowance(tmp_path, maximum):
    from folio.runtime.demo import DemoRuntime

    class Runtime(DemoRuntime):
        tasks = []

        async def complete(self, task, *args, **kwargs):
            self.tasks.append(task)
            return await super().complete(task, *args, **kwargs)

    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "rules.md").write_text("# 审批\n交易撤销必须由主管审批。", encoding="utf-8")
    runtime = Runtime()
    with pytest.raises(ModelCallBudgetExceeded):
        await prepare_with_model(
            "编制交易管理需求说明书", [], tmp_path / "result", runtime=runtime,
            source_dir=sources, limits=Limits(max_calls=maximum),
        )
    assert len(runtime.tasks) == maximum
    assert runtime.tasks == ["directory_research"] * maximum
    report = json.loads((tmp_path / "result/research/report.json").read_text())
    assert report["calls"] == maximum
    assert not (tmp_path / "result/document-plan.json").exists()
