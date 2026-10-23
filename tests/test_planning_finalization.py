"""Failed research closes once within its existing budget and keeps verified work."""

import asyncio
import json
from copy import deepcopy

import pytest
from test_planning_research import Runtime, action

from folio.research.native_tools import tool_definitions
from folio.research.planning import PlanningResearchLimits, research_for_directory
from folio.runtime.base import RuntimeResult


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "rules.md").write_text("# 技术要求\n撤单后必须释放库存，并记录审批日志。\n", encoding="utf-8")
    return root


class FailedReading:
    def __init__(self, *, closing="valid", cancel_at=None, after_save=None):
        self.closing, self.cancel_at, self.after_save = closing, cancel_at, after_save
        self.normal_calls = 0
        self.read = None

    def __call__(self, context):
        if context.get("research_finalizing"):
            assert [item["function"]["name"] for item in tool_definitions(
                "directory_research", context, {}
            )] == ["finish"]
            if self.closing == "malformed":
                return RuntimeResult("不是有效的原生工具调用")
            if self.closing == "cancel":
                raise asyncio.CancelledError()
            evidence = [entry["evidence_id"] for entry in self.read["result"]["evidence"]]
            return action(
                "finish", summary="已读取撤单和审批日志的目录依据。",
                requirements=[{"requirement_id": "r1", "text": "安排撤单释放库存与审批日志要求",
                               "evidence_ids": ["ev_invented"] if self.closing == "invented" else evidence}],
                blocking_gaps=["缺少成本资料"] if self.closing == "source_gap" else [],
            )
        self.normal_calls += 1
        if self.normal_calls == self.cancel_at:
            raise asyncio.CancelledError()
        if self.normal_calls == 1:
            return action("fs.inspect", path="rules.md")
        if self.normal_calls == 2:
            return action("fs.read", path="rules.md", limit=20)
        if self.normal_calls == 3:
            self.read = next(item for item in context["observations"] if item["tool"] == "fs.read")
            if self.after_save:
                self.after_save()
            return action("research.save", research_notes=[{
                "topic": "撤单与审计", "text": "撤单后释放库存，审批需留日志。", "kind": "source_statement",
                "evidence_ids": [item["evidence_id"] for item in self.read["result"]["evidence"]],
            }])
        return action("fs.inspect", path="missing.md")


async def run(sources, tmp_path, policy, **limits):
    runtime = Runtime(policy)
    output = tmp_path / "research"
    report = await research_for_directory(
        runtime, sources, output, "编写技术要求目录",
        limits=PlanningResearchLimits(max_calls=12, **limits),
    )
    return runtime, report, output


async def test_tool_failure_closes_once_and_can_finish_with_real_evidence(sources, tmp_path):
    runtime, report, output = await run(sources, tmp_path, FailedReading())
    assert report["status"] == "ready"
    assert report["conclusion_submitted"] is True
    assert report["termination_reason"] == "tool_error"
    assert report["failure"]["recovered"] is True
    assert report["calls"] == len(runtime.calls) == 7
    assert sum(bool(context.get("research_finalizing")) for context in runtime.calls) == 1
    assert report["requirements"][0]["evidence_ids"]
    assert report["working_findings"][0]["text"] == "撤单后释放库存，审批需留日志。"
    saved = json.loads((output / "events/research_007-request.json").read_text())
    assert saved["phase"] == "finalization"
    assert [tool["function"]["name"] for tool in saved["tools"]] == ["finish"]
    assert json.loads((output / "report.json").read_text())["status"] == "ready"


async def test_invalid_early_finish_does_not_suppress_later_finalization(sources, tmp_path):
    class EarlyFinish(FailedReading):
        def __call__(self, context):
            if not context.get("research_finalizing") and self.normal_calls == 3:
                self.normal_calls += 1
                return action(
                    "finish", summary="提前提交，引用了尚未读取的依据。",
                    requirements=[{"requirement_id": "r1", "text": "安排撤单与审批日志",
                                   "evidence_ids": ["E999"]}],
                )
            return super().__call__(context)

    runtime, report, output = await run(sources, tmp_path, EarlyFinish())
    early = json.loads((output / "events/research_004-assessment.json").read_text())
    assert early["status"] == "partial" and early["selected_ranges"] == []
    assert any("E999" in issue for issue in early["blocking_gaps"])
    assert sum(bool(context.get("research_finalizing")) for context in runtime.calls) == 1
    assert len(runtime.calls) == 7
    assert report["status"] == "ready" and report["conclusion_validated"] is True
    assert report["selected_ranges"] and report["working_findings"]


async def test_failed_finalization_notes_do_not_replace_the_previous_conclusion(sources, tmp_path):
    class FailedNotes(FailedReading):
        def __call__(self, context):
            if context.get("research_finalizing"):
                result = super().__call__(context)
                result["arguments"]["summary"] = "新的收尾结论"
                result["arguments"]["research_notes"] = [{
                    "topic": "未核验笔记", "text": "这条笔记没有已读依据。",
                    "kind": "source_statement", "evidence_ids": ["E999"],
                }]
                return result
            if self.normal_calls == 3:
                self.normal_calls += 1
                return action(
                    "finish", summary="此前保存的阶段性结论",
                    requirements=[
                        {"requirement_id": "old", "text": "此前已确认的撤单要求",
                         "evidence_ids": [self.read["result"]["evidence"][0]["evidence_id"]]},
                        {"requirement_id": "pending", "text": "另一个要求仍缺依据",
                         "evidence_ids": ["E888"]},
                    ],
                )
            return super().__call__(context)

    runtime, report, output = await run(sources, tmp_path, FailedNotes())
    early = json.loads((output / "events/research_004-assessment.json").read_text())
    assert early["status"] == "partial"
    assert len(runtime.calls) == 7
    failure = report["failure"].get("finalization", report["failure"])
    assert failure["code"] == "finalization_error" and "E999" in failure["message"]
    assert report["summary"] == "此前保存的阶段性结论"
    assert [item["requirement_id"] for item in report["requirements"]] == ["old"]
    assert report["requirements"][0]["text"] == "此前已确认的撤单要求"
    assert report["conclusion_submitted"] is True and report["conclusion_validated"] is False
    assert all(note["topic"] != "未核验笔记" for note in report["research_memory"]["notes"])


@pytest.mark.parametrize("send_original", [True, False])
async def test_last_read_can_be_delivered_by_finalization_but_labels_alone_cannot_pass(
    sources, tmp_path, monkeypatch, send_original,
):
    from folio.research.planning import _DirectoryResearch

    output = tmp_path / "research"
    original_context = _DirectoryResearch.context

    def context(self, *args, **kwargs):
        value = original_context(self, *args, **kwargs)
        if kwargs.get("finalizing") and not send_original:
            value = deepcopy(value)
            for observation in value["observations"]:
                if observation["tool"] == "fs.read":
                    # Keep known labels, but simulate a window that did not carry
                    # the original text. The audit's fetched data is not delivery.
                    observation["model_text"] = "原文编号：" + "、".join(
                        item["evidence_id"] for item in observation["result"]["evidence"])
        return value

    monkeypatch.setattr(_DirectoryResearch, "context", context)
    ordinary_calls = 0

    def policy(context):
        nonlocal ordinary_calls
        if context.get("research_finalizing"):
            pending = json.loads((output / "report.json").read_text())
            assert pending["evidence"]
            assert not any(receipt["evidence_ids"] for receipt in pending["model_deliveries"])
            reading = next(item for item in context["observations"] if item["tool"] == "fs.read")
            return action(
                "finish", summary="已读取技术要求。",
                requirements=[{"requirement_id": "r1", "text": "撤单后释放库存并记录审批日志",
                               "evidence_ids": [item["evidence_id"] for item in reading["result"]["evidence"]]}],
            )
        ordinary_calls += 1
        return action("fs.inspect", path="rules.md") if ordinary_calls == 1 else action(
            "fs.read", path="rules.md", limit=20)

    runtime = Runtime(policy)
    report = await research_for_directory(
        runtime, sources, output, "编写技术目录",
        limits=PlanningResearchLimits(max_rounds=1, max_steps_per_round=2, max_calls=6),
    )
    assert len(runtime.calls) == report["calls"] == 3
    assert runtime.calls[-1]["research_finalizing"] is True
    assert report["conclusion_validated"] is send_original
    if send_original:
        assert report["status"] == "ready" and report["requirements"]
        assert report["model_deliveries"][-1]["evidence_ids"]
    else:
        assert report["status"] == "partial" and report["requirements"] == []
        assert not report["model_deliveries"][-1]["evidence_ids"]
        assert any('have not been delivered to the model' in gap for gap in report["blocking_gaps"])


@pytest.mark.parametrize("closing", ["malformed", "invented", "source_gap"])
async def test_failed_or_incomplete_close_preserves_notes_without_fabricating_ready(sources, tmp_path, closing):
    runtime, report, output = await run(sources, tmp_path, FailedReading(closing=closing))
    assert report["status"] == "partial"
    assert report["blocking_gaps"]
    assert report["calls"] == len(runtime.calls) == 7
    assert sum(bool(context.get("research_finalizing")) for context in runtime.calls) == 1
    assert report["working_findings"][0]["text"] == "撤单后释放库存，审批需留日志。"
    assert report["evidence"]
    assert "撤单后释放库存" in (output / "research.md").read_text()
    saved = json.loads((output / "report.json").read_text())
    assert saved["research_memory"]["notes"] == report["research_memory"]["notes"]
    if closing in {"malformed", "invented"}:
        assert report["requirements"] == []
    if closing == "malformed":
        assert report["failure"]["finalization"]["code"] == "finalization_error"


@pytest.mark.parametrize("maximum,expected_finalizations", [(3, 0), (6, 1)])
async def test_finalization_does_not_add_calls_beyond_existing_budget(sources, tmp_path, maximum, expected_finalizations):
    runtime = Runtime(FailedReading())
    report = await research_for_directory(
        runtime, sources, tmp_path / "research", "编写目录",
        limits=PlanningResearchLimits(max_calls=maximum),
    )
    assert report["calls"] == len(runtime.calls) == maximum
    assert sum(bool(context.get("research_finalizing")) for context in runtime.calls) == expected_finalizations
    assert report["working_findings"]
    if maximum == 3:
        assert report["status"] == "partial"


async def test_tool_budget_exhaustion_never_requests_extra_finish(sources, tmp_path):
    runtime, report, _ = await run(sources, tmp_path, FailedReading(), max_tool_calls=3)
    assert len(runtime.calls) == report["calls"] == report["tool_calls"] == 3
    assert not any(context.get("research_finalizing") for context in runtime.calls)
    assert report["status"] == "partial"
    assert report["working_findings"]


async def test_expired_time_budget_does_not_request_finalization(sources, tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("folio.research.planning.monotonic", lambda: clock[0])
    policy = FailedReading(after_save=lambda: clock.__setitem__(0, 12.0))
    runtime, report, _ = await run(sources, tmp_path, policy, max_seconds=1)
    assert report["termination_reason"] == "time_limit"
    assert len(runtime.calls) == 3
    assert not any(context.get("research_finalizing") for context in runtime.calls)
    assert report["working_findings"]


async def test_finalization_rechecks_deadline_after_async_progress(sources, tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("folio.research.planning.monotonic", lambda: clock[0])

    async def progress(message):
        if message.startswith("正在整理已读资料和研究笔记"):
            clock[0] = 30.0

    runtime = Runtime(FailedReading())
    report = await research_for_directory(
        runtime, sources, tmp_path / "research", "编写目录", on_progress=progress,
        limits=PlanningResearchLimits(max_calls=12, max_seconds=10),
    )
    assert len(runtime.calls) == report["calls"] == 6
    assert not any(context.get("research_finalizing") for context in runtime.calls)
    assert report["status"] == "partial" and report["working_findings"]


@pytest.mark.parametrize("cancel_finalization", [False, True])
async def test_cancellation_preserves_work_and_never_starts_another_close(sources, tmp_path, cancel_finalization):
    policy = FailedReading(closing="cancel" if cancel_finalization else "valid",
                           cancel_at=None if cancel_finalization else 4)
    runtime = Runtime(policy)
    output = tmp_path / "research"
    with pytest.raises(asyncio.CancelledError):
        await research_for_directory(runtime, sources, output, "编写目录")
    saved = json.loads((output / "report.json").read_text())
    assert saved["status"] == "partial"
    assert saved["termination_reason"] == "cancelled"
    assert saved["working_findings"] and saved["evidence"]
    assert sum(bool(context.get("research_finalizing")) for context in runtime.calls) == int(cancel_finalization)
