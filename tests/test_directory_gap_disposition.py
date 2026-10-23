"""Directory finish separates correctable reading work from unavailable input."""

import json

import pytest
from test_planning_research import Runtime, action

from folio.research.planning import PlanningResearchLimits, research_for_directory
from folio.runtime.base import RuntimeResult
from folio.runtime.errors import protocol_failure


@pytest.mark.parametrize("remaining", [None, [], ["继续读取评分章节"]])
async def test_partial_finish_stops_only_with_explicit_no_local_actions(tmp_path, remaining):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "bid.md").write_text("# 采购要求\n必须支持审批流程。\n", encoding="utf-8")
    followups = []

    def policy(context):
        if not context["observations"]:
            return action("fs.read", path="bid.md")
        if context["observations"][-1]["tool"] == "finish":
            followups.append(context)
            return action("fs.inspect", path="bid.md")
        evidence = context["observations"][0]["result"]["evidence"]
        args = {
            "summary": "审批要求已核实，合同标包仍需用户选定。",
            "requirements": [{"requirement_id": "r1", "text": "覆盖审批要求",
                              "evidence_ids": [item["evidence_id"] for item in evidence]}],
            "blocking_gaps": ["合同标包尚未确定，会影响章节范围"],
            "unresolved": ["投标人报价尚未提供"],
        }
        if remaining is not None:
            args["local_research_actions"] = remaining
        return action("finish", **args)

    runtime = Runtime(policy)
    report = await research_for_directory(
        runtime, sources, tmp_path / "research", "编写投标书目录",
        limits=PlanningResearchLimits(max_calls=3),
    )
    assert report["status"] == "partial"
    assert report["requirements"] and report["unresolved"] == ["投标人报价尚未提供"]
    assert "合同标包尚未确定，会影响章节范围" in report["blocking_gaps"]
    assert report["calls"] == (2 if remaining == [] else 3)
    assert bool(followups) is (remaining != [])


@pytest.mark.parametrize("stream_error", [False, True])
async def test_plain_text_reply_gets_bounded_native_finish_correction(tmp_path, stream_error):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "bid.md").write_text("# 采购要求\n必须支持审批流程。\n", encoding="utf-8")
    calls = 0

    def policy(context):
        nonlocal calls
        calls += 1
        if calls == 1:
            return action("fs.read", path="bid.md")
        if calls == 2:
            if stream_error:
                raise protocol_failure("tool_finish_invalid", finish_reason="stop", tool_calls_received=0)
            return RuntimeResult("研究已完成，剩余投标人信息需用户提供。")
        assert "No tool was executed" in context["format_feedback"]
        if not stream_error:
            assert context["native_retry_response"] == "研究已完成，剩余投标人信息需用户提供。"
        evidence = context["observations"][0]["result"]["evidence"]
        return action(
            "finish", summary="现有原文足够规划目录",
            requirements=[{"requirement_id": "r1", "text": "覆盖审批流程",
                           "evidence_ids": [item["evidence_id"] for item in evidence]}],
            unresolved=["投标人信息待提供"], local_research_actions=[],
        )

    output = tmp_path / "research"
    report = await research_for_directory(
        Runtime(policy), sources, output, "编写目录",
        limits=PlanningResearchLimits(max_calls=4),
    )
    assert report["status"] == "ready" and report["calls"] == 3
    saved = json.loads((output / "events" / "research_003-request.json").read_text())
    assert "No tool was executed" in saved["context"]["format_feedback"]


async def test_repeated_plain_text_protocol_errors_remain_bounded(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "bid.md").write_text("投标条款。", encoding="utf-8")
    runtime = Runtime(lambda _: RuntimeResult("研究已结束，请补充投标人信息。"))
    report = await research_for_directory(
        runtime, sources, tmp_path / "research", "编写目录",
        limits=PlanningResearchLimits(max_calls=3),
    )
    assert report["status"] == "partial"
    assert len(runtime.calls) == report["calls"] == 3
    assert not report["requirements"] and not report["conclusion_submitted"]
