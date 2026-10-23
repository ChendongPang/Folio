import asyncio
import json

import pytest

from folio.document.research_review import review_directory_research
from folio.models import Limits, PlanItem, PlanProposal
from folio.runtime.base import RuntimeResult
from folio.runtime.errors import ModelCallFailed


def proposal(*subjects):
    return PlanProposal(sections=[PlanItem(
        node_id="n1", title="技术响应", level=1,
        instruction="具体响应：" + "；".join(subjects),
    )])


def researched(*subjects, padding=0, status="ready"):
    return {
        "status": status,
        "requirements": [{"requirement_id": f"r{i}", "text": subject,
                          "evidence_ids": [f"ev{i}"]}
                         for i, subject in enumerate(subjects)],
        "evidence": [{"evidence_id": f"ev{i}", "source_id": "tender.docx",
                      "source_version": "sha256-v1", "locator": f"第五章/段落{i}",
                      "text": "原始条款：" + subject + "。" + "原文" * padding}
                     for i, subject in enumerate(subjects)],
        # This deliberately omits the later requirements. Review must use originals.
        "summary": "文件概述和投标资格。",
        "unresolved": [],
    }


class ReviewRuntime:
    def __init__(self, mutate=None):
        self.contexts = []
        self.mutate = mutate

    def prompt_chars(self, task, context, schema):
        return len(json.dumps(context, ensure_ascii=False)) + len(
            json.dumps(schema, ensure_ascii=False)
        )

    async def complete(self, task, context, schema, session_key):
        assert task == "review_directory_research"
        self.contexts.append(context)
        scope = " ".join(item["instruction"] for item in context["directory"])
        checks = []
        for item in context["researched_requirements"]:
            originals = {e["evidence_id"]: e for e in context["evidence"]}
            assert item["text"] in originals[item["evidence_ids"][0]]["text"]
            covered = item["text"] in scope
            checks.append({
                "requirement_id": item["requirement_id"],
                "disposition": "covered" if covered else "missing",
                "node_ids": ["n1"] if covered else [],
                "evidence_ids": item["evidence_ids"],
                "reason": "已在技术响应写作范围明确承接" if covered else "未明确安排：" + item["text"],
            })
        if self.mutate:
            self.mutate(checks)
        return RuntimeResult(json.dumps({"checks": checks}, ensure_ascii=False),
                             usage={"input_tokens": 123, "output_tokens": 40})


def run(runtime, directory, research, **kwargs):
    return asyncio.run(review_directory_research(runtime, directory, research, **kwargs))


def test_late_technical_requirement_is_checked_and_repaired_without_chapter_per_item():
    subjects = ["项目目标", "返回检验", "FRTB资本计量和损益归因"]
    source = researched(*subjects, padding=3000)
    runtime = ReviewRuntime()
    result = run(runtime, proposal(*subjects[:2]), source)
    assert result["status"] == "gaps"
    assert result["checks"][-1]["disposition"] == "missing"
    assert "FRTB" in result["issues"][0]
    # Full later originals, not only the head excerpt or lossy research summary.
    actual = runtime.contexts[0]
    assert actual["evidence"] == [dict(e, retrieved_at=None, metadata={})
                                  for e in source["evidence"]]
    assert "summary" not in actual
    repaired = run(ReviewRuntime(), proposal(*subjects), source)
    assert repaired["status"] == "passed"
    assert all(check["node_ids"] == ["n1"] for check in repaired["checks"])
    assert len(repaired["calls"]) == 1
    assert repaired["calls"][0]["usage"]["input_tokens"] == 123


def test_batches_include_all_requirements_and_complete_originals():
    subjects = [f"独立功能{i}" for i in range(6)]
    source = researched(*subjects, padding=650)
    runtime = ReviewRuntime()
    result = run(runtime, proposal(*subjects), source,
                 limits=Limits(max_planning_prompt_chars=4000))
    assert result["status"] == "passed"
    assert 1 < len(runtime.contexts) <= 12
    assert [c["requirement_id"] for c in result["checks"]] == [f"r{i}" for i in range(6)]
    seen = {}
    for context in runtime.contexts:
        assert context["directory"] == proposal(*subjects).model_dump()["sections"]
        for evidence in context["evidence"]:
            seen[evidence["evidence_id"]] = evidence["text"]
    assert seen == {e["evidence_id"]: e["text"] for e in source["evidence"]}


@pytest.mark.parametrize("mutation,message", [
    (lambda checks: checks.pop(), "exactly once"),
    (lambda checks: checks.append(dict(checks[0])), "exactly once"),
    (lambda checks: checks[0].update(requirement_id="fake"), "exactly once"),
    (lambda checks: checks[0].update(node_ids=["fake"]), "nonexistent"),
    (lambda checks: checks[0].update(evidence_ids=["fake"]), "evidence that does not belong"),
    (lambda checks: checks[0].update(evidence_ids=["ev1"]), "evidence that does not belong"),
    (lambda checks: checks[0].update(node_ids=[]), "contain body text"),
    (lambda checks: checks[0].update(node_ids=["n1", "n1"]), "duplicate"),
    (lambda checks: checks[0].update(reason="   "), "substantive reason"),
])
def test_reference_cheating_or_missing_results_never_pass(mutation, message):
    with pytest.raises(ValueError, match=message):
        run(ReviewRuntime(mutation), proposal("要求A", "要求B"), researched("要求A", "要求B"))


def test_container_is_not_a_writing_destination():
    directory = PlanProposal(sections=[
        PlanItem(node_id="group", title="技术", level=1, content_mode="container"),
        PlanItem(node_id="n1", title="响应", level=2, instruction="要求A"),
    ])
    with pytest.raises(ValueError, match="contain body text"):
        run(ReviewRuntime(lambda checks: checks[0].update(node_ids=["group"])),
            directory, researched("要求A"))


def test_container_is_ignored_when_generate_destination_is_also_present():
    directory = PlanProposal(sections=[
        PlanItem(node_id="group", title="技术", level=1, content_mode="container"),
        PlanItem(node_id="n1", title="响应", level=2, instruction="要求A"),
    ])
    result = run(
        ReviewRuntime(lambda checks: checks[0].update(node_ids=["group", "n1"])),
        directory, researched("要求A"),
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["node_ids"] == ["n1"]


def test_check_keeps_only_evidence_supplied_for_its_requirement():
    def add_adjacent_evidence(checks):
        checks[0]["evidence_ids"] = [
            checks[0]["evidence_ids"][0],
            checks[1]["evidence_ids"][0],
        ]

    result = run(
        ReviewRuntime(add_adjacent_evidence),
        proposal("要求A", "要求B"),
        researched("要求A", "要求B"),
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["evidence_ids"] == ["ev0"]


def test_duplicate_requirement_evidence_is_deduplicated():
    def duplicate(checks):
        checks[0]["evidence_ids"] = ["ev0", "ev0"]

    result = run(
        ReviewRuntime(duplicate),
        proposal("要求A", "要求B"),
        researched("要求A", "要求B"),
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["evidence_ids"] == ["ev0"]


def test_requirement_id_alias_from_model_is_normalized():
    def typo(checks):
        checks[0]["require_id"] = checks[0].pop("requirement_id")
        checks[0]["id"] = checks[0]["require_id"]

    result = run(
        ReviewRuntime(typo),
        proposal("要求A", "要求B"),
        researched("要求A", "要求B"),
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["requirement_id"] == "r0"


def test_not_applicable_has_explicit_scope_basis_and_no_fake_destination():
    def exclude(checks):
        checks[1].update(disposition="not_applicable", node_ids=[],
                         reason="用户明确只投包件一，原文第二项明确限定包件二")

    result = run(ReviewRuntime(exclude), proposal("包件一功能"),
                 researched("包件一功能", "包件二功能"), requirements="只投包件一")
    assert result["status"] == "passed"
    assert result["checks"][1]["disposition"] == "not_applicable"


@pytest.mark.parametrize("source", [{}, {"status": "ready", "requirements": [], "evidence": []},
                                      {"status": "partial", "requirements": [], "evidence": []}])
def test_empty_research_with_sources_is_a_gap(source):
    runtime = ReviewRuntime()
    assert run(runtime, proposal("目标"), source)["status"] == "gaps"
    assert runtime.contexts == []


def test_no_sources_only_skips_when_there_is_no_source_evidence():
    runtime = ReviewRuntime()
    result = run(runtime, proposal("目标"), {"status": "no_sources"})
    assert result == {"status": "passed", "checks": [], "issues": [], "calls": []}
    assert run(runtime, proposal("目标"), researched("目标", status="no_sources"))["status"] == "gaps"
    assert runtime.contexts == []


def test_partial_research_readiness_is_a_separate_gate():
    source = researched("目标", status="partial")
    source["unresolved"] = ["尚未确定投标包件"]
    result = run(ReviewRuntime(), proposal("目标"), source)
    assert result["status"] == "passed"
    assert source["status"] == "partial"
    assert source["unresolved"]  # The caller must still forbid confirmation.


@pytest.mark.parametrize("mutation", [
    lambda source: source["requirements"][0].update(evidence_ids=["invented"]),
    lambda source: source["requirements"].append(dict(source["requirements"][0])),
    lambda source: source["evidence"].append(dict(source["evidence"][0])),
])
def test_invalid_research_receipts_do_not_reach_model(mutation):
    source = researched("目标")
    mutation(source)
    runtime = ReviewRuntime()
    with pytest.raises(ValueError):
        run(runtime, proposal("目标"), source)
    assert runtime.contexts == []


def test_oversized_single_requirement_is_not_truncated():
    runtime = ReviewRuntime()
    result = run(runtime, proposal("目标"), researched("目标", padding=10000),
                 limits=Limits(max_planning_prompt_chars=4000))
    assert result["status"] == "gaps"
    assert "未截断" in result["issues"][0]
    assert not runtime.contexts


def test_review_batch_budget_does_not_accept_partial_prefix():
    runtime = ReviewRuntime()
    subjects = [f"功能{i}" for i in range(14)]
    result = run(runtime, proposal(*subjects), researched(*subjects, padding=650),
                 limits=Limits(max_planning_prompt_chars=4000))
    assert result["status"] == "gaps"
    assert "12 批预算" in result["issues"][0]
    assert not runtime.contexts


def test_incomplete_model_output_does_not_pass():
    class Truncated(ReviewRuntime):
        async def complete(self, *args):
            return RuntimeResult('{"checks":[]}', stop_reason="length")

    with pytest.raises(ModelCallFailed, match="incomplete"):
        run(Truncated(), proposal("目标"), researched("目标"))


def test_model_timeout_is_not_success():
    class Slow(ReviewRuntime):
        async def complete(self, *args):
            await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        run(Slow(), proposal("目标"), researched("目标"),
            limits=Limits(call_timeout_seconds=0.01))


def test_failed_second_batch_keeps_first_batch_and_received_invalid_response(tmp_path):
    class SecondInvalid(ReviewRuntime):
        async def complete(self, *args):
            result = await super().complete(*args)
            return RuntimeResult('{"checks": []}', usage={"input_tokens": 50}) \
                if len(self.contexts) == 2 else result

    output = tmp_path / "research-review-attempt-1.json"
    subjects = [f"功能{i}" for i in range(4)]
    with pytest.raises(ValueError, match="exactly once"):
        run(SecondInvalid(), proposal(*subjects), researched(*subjects, padding=650),
            limits=Limits(max_planning_prompt_chars=4000), output=output)
    audit = json.loads(output.read_text())
    assert audit["status"] == "failed"
    assert len(audit["calls"]) == 2
    assert audit["calls"][0]["status"] == "completed"
    assert audit["calls"][1]["status"] == "failed"
    assert audit["calls"][1]["response"] == '{"checks": []}'
    assert audit["checks"]  # Completed checks survived the later failure.
    assert not output.with_name(output.name + ".tmp").exists()


def test_cancellation_keeps_pending_batch_input_and_status(tmp_path):
    output = tmp_path / "research-review-attempt-1.json"

    class Waiting(ReviewRuntime):
        async def complete(self, *args):
            pending = json.loads(output.read_text())
            assert pending["status"] == "running"
            assert pending["calls"][0]["input"]["evidence"]
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(Waiting(), proposal("目标"), researched("目标"), output=output)
    audit = json.loads(output.read_text())
    assert audit["status"] == "cancelled"
    assert audit["calls"][0]["error"] == "CancelledError"
