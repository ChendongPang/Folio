"""Interrupted research remains useful, but cannot pass the confirmation gate."""

import json
from copy import deepcopy

import pytest

from folio.document.prepare import prepare_with_model
from folio.document.research_context import (
    directory_confirmation_issues,
    planning_research_context,
)
from folio.models import Limits
from folio.runtime.base import RuntimeResult


def partial_research():
    original = {"evidence_id": "ev1", "source_id": "a.pdf", "source_version": "v1",
                "locator": "第 21 页", "text": "HBM 的封装供给限制了系统出货。"}
    reference = {key: original[key] for key in ("source_id", "source_version", "locator")}
    return {
        "status": "partial", "summary": "", "requirements": [], "files": [],
        "evidence": [original, original | {"evidence_id": "unseen", "text": "仅抓取，未交付的原文"}],
        "blocking_gaps": ["工具执行失败"], "termination_reason": "tool_error",
        "failure": {"code": "tool_error", "message": "cursor 参数冲突", "request_id": "read2"},
        "model_deliveries": [{"request_id": "call2", "evidence_ids": ["ev1"]}],
        "research_memory": {
            "batches": {"read1": {"delivered_evidence_ids": ["ev1"], "references": {"ev1": reference}}},
            "notes": [{"note_id": "M1", "topic": "供给约束", "text": "封装可能成为交付瓶颈。",
                       "kind": "inference", "evidence_ids": ["ev1"], "request_ids": ["read1"],
                       "unresolved": ["仍需核实瓶颈持续时间"], "references": {"ev1": reference}}],
        },
    }


def test_interrupted_handoff_preserves_notes_and_only_delivered_originals():
    source = partial_research()
    before = deepcopy(source)
    context = planning_research_context(source)
    assert context["requirements"] == []
    assert context["working_findings"][0]["kind"] == "inference"
    assert context["working_findings"][0]["unresolved"] == ["仍需核实瓶颈持续时间"]
    assert [item["evidence_id"] for item in context["evidence"]] == ["ev1"]
    assert context["evidence"][0]["text"] == source["evidence"][0]["text"]
    assert "formal requirements is empty" in context["evidence_note"]
    assert source == before


@pytest.mark.parametrize("tamper", ["version", "unseen", "locator"])
def test_stale_or_undelivered_notes_are_not_handed_to_planning(tamper):
    source = partial_research()
    note = source["research_memory"]["notes"][0]
    if tamper == "unseen":
        note["evidence_ids"] = ["unseen"]
    else:
        note["references"]["ev1"]["source_version" if tamper == "version" else "locator"] = "changed"
    assert planning_research_context(source)["working_findings"] == []


def test_explicit_working_findings_retain_saved_note_evidence():
    source = partial_research()
    source["working_findings"] = deepcopy(source["research_memory"]["notes"])
    source["research_memory"]["notes"] = []
    assert planning_research_context(source)["working_findings"][0]["note_id"] == "M1"


class PlanningRuntime:
    def __init__(self, *, coverage_missing=False):
        self.contexts = []
        self.coverage_missing = coverage_missing

    async def complete(self, task, context, schema, session_key):
        self.contexts.append((task, deepcopy(context)))
        if task == "plan":
            value = {"sections": [{"node_id": "n1", "title": "存储供给约束", "level": 1,
                                   "instruction": "分析 HBM 封装供给瓶颈"}]}
        else:
            assert task == "review_directory_research"
            missing = self.coverage_missing and sum(name == task for name, _ in self.contexts) == 1
            value = {"checks": [{"requirement_id": "r1", "disposition": "missing" if missing else "covered",
                                 "node_ids": [] if missing else ["n1"], "evidence_ids": ["ev1"],
                                 "reason": "未安排供给瓶颈分析" if missing else "已安排对应分析"}]}
        return RuntimeResult(json.dumps(value, ensure_ascii=False))


async def prepare(monkeypatch, tmp_path, report, runtime):
    async def research(*args, **kwargs):
        return deepcopy(report)

    monkeypatch.setattr("folio.research.planning.research_for_directory", research)
    monkeypatch.setattr("folio.document.prepare.prepare", lambda *a, **kw: {"nodes": len(kw["plan"].sections)})
    return await prepare_with_model(
        "分析存储供给", [{"name": "a.pdf", "path": "a.pdf"}], tmp_path / "out",
        runtime=runtime, source_dir=tmp_path, limits=Limits(max_calls=10, call_attempts=2),
    )


async def test_missing_finish_does_not_discard_research_or_retry_the_directory(monkeypatch, tmp_path):
    runtime = PlanningRuntime()
    result = await prepare(monkeypatch, tmp_path, partial_research(), runtime)
    assert [task for task, _ in runtime.contexts] == ["plan"]
    handed = runtime.contexts[0][1]["directory_research"]
    assert handed["working_findings"][0]["note_id"] == "M1"
    assert handed["evidence"][0]["evidence_id"] == "ev1"
    assert result["confirmable"] is False
    assert result["research_review"]["issue_kind"] == "tool_error"
    assert result["research_review"]["retryable"] is False
    assert result["confirmation_issues"][0]["code"] == "tool_error"
    assert "补充资料" not in result["confirmation_issues"][0]["message"]
    assert len(json.loads((tmp_path / "out/planning-attempts.json").read_text())) == 1


async def test_partial_research_does_not_retry_even_if_existing_requirements_are_missing(monkeypatch, tmp_path):
    report = partial_research()
    report["requirements"] = [{"requirement_id": "r1", "text": "分析封装瓶颈", "evidence_ids": ["ev1"]}]
    runtime = PlanningRuntime(coverage_missing=True)
    result = await prepare(monkeypatch, tmp_path, report, runtime)
    assert [task for task, _ in runtime.contexts] == ["plan", "review_directory_research"]
    assert result["confirmable"] is False
    assert result["research_review"]["issue_kind"] == "directory_coverage"
    assert result["research_review"]["retryable"] is False


async def test_ready_research_keeps_real_directory_coverage_repair(monkeypatch, tmp_path):
    report = partial_research()
    report.update(status="ready", blocking_gaps=[], termination_reason="", failure={})
    report["requirements"] = [{"requirement_id": "r1", "text": "分析封装瓶颈", "evidence_ids": ["ev1"]}]
    runtime = PlanningRuntime(coverage_missing=True)
    result = await prepare(monkeypatch, tmp_path, report, runtime)
    assert [task for task, _ in runtime.contexts] == [
        "plan", "review_directory_research", "plan", "review_directory_research",
    ]
    assert "未安排供给瓶颈分析" in runtime.contexts[2][1]["format_feedback"]
    assert result["confirmable"] is True
    assert result["confirmation_issues"] == []


@pytest.mark.parametrize("update,expected", [
    ({"termination_reason": "time_limit"}, "research_incomplete"),
    ({"termination_reason": "model_timeout"}, "model_error"),
    ({"termination_reason": "", "failure": {}, "conclusion_submitted": True}, "source_insufficient"),
])
def test_confirmation_separates_execution_failure_from_source_gaps(update, expected):
    report = partial_research() | update
    assert directory_confirmation_issues(report, None)[0]["code"] == expected


def test_invalid_conclusion_is_not_presented_as_missing_user_material():
    report = partial_research() | {
        "termination_reason": "", "failure": {}, "conclusion_submitted": True,
        "blocking_gaps": ["引用尚未提供给模型"], "validation_issues": ["引用尚未提供给模型"],
    }
    assert directory_confirmation_issues(report, None)[0]["code"] == "research_incomplete"


def test_round_limit_does_not_hide_explicit_source_gap():
    report = partial_research() | {
        "termination_reason": "round_limit", "failure": {}, "conclusion_submitted": True,
        "blocking_gaps": ["未提供成本数据", "轮次预算已用完"], "validation_issues": ["轮次预算已用完"],
    }
    assert directory_confirmation_issues(report, None)[0]["code"] == "source_insufficient"


@pytest.mark.parametrize("validated,expected", [(False, "research_incomplete"), (True, "source_insufficient")])
def test_only_validated_conclusion_can_claim_user_sources_are_insufficient(validated, expected):
    report = partial_research() | {
        "termination_reason": "round_limit", "failure": {}, "conclusion_submitted": True,
        "conclusion_validated": validated,
        "blocking_gaps": ["技术需求原文尚未读取", "轮次预算已用完"],
        "validation_issues": ["轮次预算已用完"],
    }
    assert directory_confirmation_issues(report, None)[0]["code"] == expected
    assert planning_research_context(report)["conclusion_validated"] is validated
