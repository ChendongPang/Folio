import asyncio
import json
from pathlib import Path

import pytest
from docx import Document

from folio.models import JobInput, Limits
from folio.orchestration.engine import Engine
from folio.research.formats import SourceFormatError
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


class TrackingRuntime(DemoRuntime):
    def __init__(self):
        self.calls = []

    async def complete(self, task, context, schema, session_key):
        self.calls.append((task, context, session_key))
        return await super().complete(task, context, schema, session_key)


async def test_demo_generates_traceable_docx_without_publishing(store, demo_job):
    runtime = TrackingRuntime()
    job = await Engine(store, runtime).run(demo_job)
    assert job["status"] == "draft"
    assert job["completed_sections"] == 2
    doc = Document(job["artifact"])
    paragraphs = [p.text for p in doc.paragraphs]
    assert paragraphs[0] == "测试需求说明书"
    assert "草稿" in paragraphs
    assert any("外汇交易" in text for text in paragraphs)
    assert "Folio Body" in doc.styles
    assert any("演示产物" in text for text in paragraphs)
    assert not any("ev_" in text or "证据来源" in text for text in paragraphs)
    assert all(state.evidence for state in store.states(demo_job))
    assert all(c.status == "supported" for s in store.states(demo_job) for c in s.extraction.claims)
    with store.connect() as conn:
        assert conn.execute("SELECT published_version FROM documents").fetchone()[0] is None
    call_count = len(runtime.calls)
    await Engine(store, runtime).run(demo_job)
    assert len(runtime.calls) == call_count


async def test_missing_evidence_produces_complete_labeled_proposal_draft(store, inputs):
    Path(inputs["knowledge_path"], "rules.md").unlink()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, DemoRuntime()).run(job_id)
    assert job["status"] == "draft"
    assert not job["snapshot"]["blocking_issues"]
    assert all(len(w.searches) == 3 and w.proposals for w in store.work_states(job_id).values())
    assert all(not s.content.pending_items for s in store.states(job_id))
    assert all(b.origin == "model_proposal" for s in store.states(job_id) for b in s.content.blocks)
    assert not job["snapshot"]["records"]
    assert Path(job["artifact"]).is_file()


async def test_invalid_citation_never_promoted(store, inputs):
    class BadCitation(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "extract":
                data = json.loads(result.content)
                data["claims"][0]["status"] = "supported"
                data["claims"][0]["evidence_ids"] = ["invented_evidence"]
                return RuntimeResult(json.dumps(data))
            return result

    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=0)))
    job = await Engine(store, BadCitation()).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert all(c.status == "candidate" for s in store.states(job_id) for c in s.extraction.claims)
    assert not job["snapshot"]["records"]


async def test_extraction_requires_real_source_references_on_wire(store, demo_job):
    import jsonschema

    from folio.models import Extraction

    class InspectExtraction(DemoRuntime):
        inspected = 0

        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "extract" and context["evidence"]:
                self.inspected += 1
                data = Extraction.model_validate_json(result.content).model_dump()
                jsonschema.validate(data, schema)
                del data["claims"][0]["evidence_ids"]
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(data, schema)
                for invalid in ([], ["unretrieved_source"]):
                    data["claims"][0]["evidence_ids"] = invalid
                    with pytest.raises(jsonschema.ValidationError):
                        jsonschema.validate(data, schema)
            return result

    runtime = InspectExtraction()
    job = await Engine(store, runtime).run(demo_job)
    assert runtime.inspected >= 2
    assert job["snapshot"]["review_passed"]


async def test_runtime_failure_is_bounded_and_persisted(store, inputs):
    class BrokenRuntime(DemoRuntime):
        async def complete(self, *args):
            return RuntimeResult("not JSON")

    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(call_attempts=2)))
    with pytest.raises(ValueError):
        await Engine(store, BrokenRuntime()).run(job_id)
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert job["calls"] == 2
    assert job["artifact"] is None
    assert "native tool_call" in job["error"]


async def test_model_call_budget_never_becomes_review_pass(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_calls=2)))
    with pytest.raises(RuntimeError, match="budget"):
        await Engine(store, DemoRuntime()).run(job_id)
    assert store.get(job_id)["status"] == "failed"
    assert store.get(job_id)["artifact"] is None


async def test_interrupted_job_reuses_completed_model_and_retrieval_checkpoints(
    store, inputs, monkeypatch
):
    first_saved = asyncio.Event()
    save_section = store.save_section

    def checkpoint(job_id, state):
        save_section(job_id, state)
        first_saved.set()

    monkeypatch.setattr(store, "save_section", checkpoint)

    class Interrupted(TrackingRuntime):
        interrupted = False

        async def complete(self, task, context, schema, session_key):
            if task == "generate" and context["section"]["level"] == 2 and not self.interrupted:
                await asyncio.wait_for(first_saved.wait(), 3)
                self.interrupted = True
                raise asyncio.CancelledError()
            return await super().complete(task, context, schema, session_key)

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    runtime = Interrupted()
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, runtime).run(job_id)
    assert len(store.states(job_id)) == 1
    assert store.get(job_id)["status"] == "running"
    first_section = store.states(job_id)[0].model_dump()
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert (
        next(
            s for s in store.states(job_id) if s.section_id == first_section["section_id"]
        ).model_dump()
        == first_section
    )
    assert len([c for c in runtime.calls if c[0] == "plan"]) == 1
    assert len(
        [
            c
            for c in runtime.calls
            if c[0] == "generate" and c[1]["section"]["section_id"] == first_section["section_id"]
        ]
    ) == 1


async def test_repair_only_changes_failed_section(store, inputs):
    class RepairRuntime(TrackingRuntime):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "generate" and context["section"]["level"] == 2:
                data = json.loads(result.content)
                data["blocks"][0]["text"] = "无依据的错误业务规则。"
                return RuntimeResult(json.dumps(data))
            if task == "drill_down":
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": True,
                            "reason": "正文无依据",
                            "affected_section_ids": context["finding"]["section_ids"],
                            "repair_instruction": "恢复证据支持的描述",
                            "needs_confirmation": False,
                        }
                    )
                )
            return result

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    runtime = RepairRuntime()
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    revisions = sorted(s.revision for s in store.states(job_id))
    assert revisions == [1, 2]
    assert len([c for c in runtime.calls if c[0] == "repair"]) == 1
    assert not job["snapshot"]["blocking_issues"]


async def test_repeated_bad_repair_stops(store, inputs):
    class NeverFixed(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task in {"generate", "repair"}:
                data = json.loads(result.content)
                data["blocks"][0]["text"] = "无依据的业务规则。"
                return RuntimeResult(json.dumps(data))
            if task == "drill_down":
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": True,
                            "reason": "错误",
                            "affected_section_ids": context["finding"]["section_ids"],
                            "repair_instruction": "修复错误",
                            "needs_confirmation": False,
                        }
                    )
                )
            return result

    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=1)))
    job = await Engine(store, NeverFixed()).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert max(s.revision for s in store.states(job_id)) == 2
    assert job["snapshot"]["blocking_issues"]


async def test_deterministic_conflict_index_catches_reviewer_miss(store, inputs):
    class Conflicting(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            result = await super().complete(task, context, schema, session_key)
            if task == "extract":
                data = json.loads(result.content)
                data["claims"][0].update(
                    subject="撤销",
                    predicate="审批人",
                    value=context["section"]["title"],
                    cardinality="single",
                )
                return RuntimeResult(json.dumps(data))
            return result

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, Conflicting()).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert job["snapshot"]["disputed_keys"]
    assert any(f["kind"] == "conflict" for f in job["snapshot"]["blocking_issues"])


async def test_failed_table_repair_keeps_original_and_delivers_unapproved_draft(store, inputs):
    from folio.models import Finding

    class TableEngine(Engine):
        async def validate_section(self, section, content, evidence, revision, feedback=""):
            state = await super().validate_section(section, content, evidence, revision, feedback)
            if section.level == 1:
                state.local_findings.append(
                    Finding(
                        kind="coverage",
                        key="required-table",
                        description="缺少模板要求的表格及列名",
                        section_ids=[section.section_id],
                    )
                )
            return state

    class UnsuccessfulRepair(TrackingRuntime):
        async def complete(self, task, context, schema, session_key):
            if task == "drill_down":
                return RuntimeResult(
                    json.dumps(
                        {
                            "confirmed": True,
                            "needs_confirmation": False,
                            "reason": "缺少表头",
                            "repair_instruction": "补充表头",
                            "affected_section_ids": context["finding"]["section_ids"],
                        }
                    )
                )
            return await super().complete(task, context, schema, session_key)

    runtime = UnsuccessfulRepair()
    job_id = store.create(JobInput(**inputs, runtime="demo", limits=Limits(max_repairs=3)))
    job = await TableEngine(store, runtime).run(job_id)
    assert job["status"] == "needs_confirmation"
    assert job["snapshot"]["review_passed"] is False
    assert all(s.revision == 1 for s in store.states(job_id))
    assert len([t for t, _, _ in runtime.calls if t == "repair"]) == 1
    assert any(
        f["key"].startswith("repair-no-progress:") for f in job["snapshot"]["blocking_issues"]
    )
    assert Path(job["artifact"]).is_file()
    assert list((store.root / "jobs" / job_id / "contexts" / "rejected_repairs").glob("*.json"))


async def test_complete_outline_uses_one_planning_request_and_bounded_business_tasks(store, inputs):
    inputs["user_request"] = "\n".join(f"# 外汇交易需求{i}" for i in range(80))
    job_id = store.create(
        # Leave room for the full English policy while exercising the same
        # 80-section task grouping and call-count bound.
        JobInput(**inputs, runtime="demo", limits=Limits(max_calls=900, max_prompt_chars=64000))
    )
    runtime = TrackingRuntime()
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert job["completed_sections"] == 80
    assert job["calls"] < 900
    plans = [call for call in runtime.calls if call[0] == "plan"]
    assert len(plans) == 1 and plans[0][1]["requirements"] == inputs["user_request"]
    assert all(len(call[1]["sections"]) <= 6 for call in runtime.calls if call[0] == "review")


async def test_table_header_repair_uses_saved_materials_without_research(store, inputs):
    from unittest.mock import AsyncMock

    from folio.models import Block, Finding, SectionContent

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    engine = Engine(store, DemoRuntime())
    await engine.run(job_id)
    section = engine.plan.sections[0].model_copy(
        update={"expected_table_headers": ["名称", "说明"]}
    )
    previous = next(s for s in store.states(job_id) if s.section_id == section.section_id)
    finding = Finding(
        kind="coverage",
        key="required-table",
        description="缺少模板表头",
        section_ids=[section.section_id],
    )
    previous.local_findings = [finding]
    candidate = SectionContent(
        blocks=[
            Block(block_id="table", kind="table", rows=[["名称", "说明"], ["交易", "已有说明"]])
        ]
    )
    engine.research_work = AsyncMock(side_effect=AssertionError("Table repair needs no research"))
    engine.call = AsyncMock(return_value=candidate)
    engine.validate_section = AsyncMock(return_value=previous)
    await engine.generate(section, previous, finding, "另外添加尚未证实的参考资料")
    engine.research_work.assert_not_awaited()
    context = engine.call.call_args.args[1]
    assert context["current"] == previous.content.model_dump()
    assert '"名称", "说明"' in context["repair_instruction"]
    assert "尚未证实" not in context["repair_instruction"]
    assert engine.call.call_args.args[0] == "repair"
    assert engine.validate_section.call_args.args[1] == candidate


async def test_run_timeout_is_persisted(store, inputs):
    class Slow(DemoRuntime):
        async def complete(self, *args):
            await asyncio.sleep(10)

    job_id = store.create(
        JobInput(**inputs, runtime="demo", limits=Limits(run_timeout_seconds=0.02))
    )
    with pytest.raises(TimeoutError):
        await Engine(store, Slow()).run(job_id)
    assert store.get(job_id)["status"] == "failed"
    assert store.get(job_id)["artifact"] is None


async def test_invalid_knowledge_file_marks_job_failed(store, inputs):
    Path(inputs["knowledge_path"], "broken.docx").write_bytes(b"invalid package")
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(SourceFormatError, match="not a valid ZIP document"):
        await Engine(store, DemoRuntime()).run(job_id)
    assert store.get(job_id)["status"] == "failed"
    assert job_id not in store.candidates()
