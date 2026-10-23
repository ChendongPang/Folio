import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from folio.models import Evidence
from folio.research.business_presentation import BusinessPresentation
from folio.research.context_views import context_field_text, fit_context, observation_page
from folio.research.contracts import ResearchConfig, ResearchStep
from folio.research.ledger import ResearchLedger
from folio.research.memory import ResearchMemory
from folio.research.transport import research_text
from folio.research.workflow import (
    ResearchTools,
    observation_view,
    result_progress_key,
    update_step_metadata,
)
from folio.research.workspace import WorkspaceManager


@pytest.fixture
def tools(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "rules.md").write_text("# 审批规则\n必须由主管审批。\n## 例外\n超过限额必须复核。", encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "task")
    result = ResearchTools.__new__(ResearchTools)
    result.workspace = workspace
    result.config = ResearchConfig()
    result.evidence = {}
    result.presentation = BusinessPresentation(workspace)
    result.ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    result.session = {"quota_task_id": "owner"}
    return result


def test_business_action_uses_object_and_preserves_saved_legacy_identity():
    legacy = '{ "file": "F1", "node": "N001" }'
    step = ResearchStep(tool="fs.read", arguments_json=legacy)
    assert step.arguments == {"file": "F1", "node": "N001"}
    assert step.arguments_json == legacy
    assert "arguments_json" not in ResearchStep.model_json_schema()["properties"]
    assert step.model_dump()["arguments"] == step.arguments
    assert ResearchStep(tool="fs.read", arguments={"file": "F1", "node": None}).arguments == {"file": "F1"}
    with pytest.raises(ValueError, match="disagree"):
        ResearchStep(tool="fs.read", arguments={"file": "F2"}, arguments_json=legacy)


@pytest.mark.parametrize("arguments,provided", [({}, False), ({"missing_information": []}, True)])
def test_native_metadata_omission_survives_cached_model_defaults(arguments, provided):
    original = ResearchStep(tool="fs.list", tool_call={
        "id": "call-one", "type": "function",
        "function": {"name": "fs_list", "arguments": json.dumps(arguments)},
    }, **arguments)
    restored = ResearchStep.model_validate_json(original.model_dump_json())
    assert "missing_information" in restored.model_fields_set
    assert restored.provided_metadata("missing_information") is provided


def test_omitted_or_empty_legacy_notes_preserve_checkpoint_and_explicit_gaps_can_clear():
    session = {"notes": "已核对审批规则", "missing": ["阈值待确认"]}
    omitted = ResearchStep(tool="fs.list", tool_call={
        "function": {"arguments": '{"summary":"继续查看目录"}'},
    })
    update_step_metadata(session, ResearchStep.model_validate_json(omitted.model_dump_json()))
    assert session == {"notes": "已核对审批规则", "missing": ["阈值待确认"]}
    explicit = ResearchStep(tool="finish", notes="", missing_information=[], tool_call={
        "function": {"arguments": '{"notes":"","missing_information":[]}'},
    })
    update_step_metadata(session, explicit)
    assert session == {"notes": "已核对审批规则", "missing": []}


def test_nested_note_omission_is_restored_from_native_envelope_after_cache_roundtrip():
    note = {"topic": "审批", "text": "须主管批准。", "kind": "source_statement",
            "evidence_ids": ["e1"], "request_ids": ["r1"]}
    step = ResearchStep(tool="research.save", research_notes=[note], tool_call={
        "function": {"arguments": json.dumps({"research_notes": [note]})},
    })
    cached = ResearchStep.model_validate_json(step.model_dump_json())
    assert "unresolved" in cached.research_notes[0].model_fields_set
    assert "unresolved" not in cached.note_updates()[0].model_fields_set
    note["unresolved"] = []
    explicit = ResearchStep(tool="research.save", research_notes=[note], tool_call={
        "function": {"arguments": json.dumps({"research_notes": [note]})},
    })
    assert "unresolved" in ResearchStep.model_validate_json(explicit.model_dump_json()).note_updates()[0].model_fields_set


def test_cached_note_revision_and_pending_replay_preserve_existing_questions():
    memory = ResearchMemory()
    item = Evidence(evidence_id="e1", source_id="rules.md", source_version="v1", locator="p1", text="主管审批。")
    memory.record_observation({"request_id": "r1", "tool": "fs.read", "arguments": {},
                               "result": {"evidence": [item.model_dump()]}})
    memory.record_delivery("r1", ["e1"])
    note = {"topic": "审批规则", "text": "由主管审批。", "kind": "source_statement",
            "evidence_ids": ["e1"], "request_ids": ["r1"], "unresolved": ["限额尚未确认"]}
    memory.apply([note], evidence={"e1": item}, call_id="initial")
    revised = {key: value for key, value in note.items() if key != "unresolved"}
    revised.update(note_id="M1", text="该条规定审批人应为主管。")
    step = ResearchStep(tool="research.save", research_notes=[revised], tool_call={
        "id": "revise", "function": {"arguments": json.dumps({"research_notes": [revised]})},
    })
    cached = ResearchStep.model_validate_json(step.model_dump_json())
    memory.apply(cached.note_updates(), evidence={"e1": item}, call_id="revise")
    assert memory.dump()["notes"][0]["unresolved"] == ["限额尚未确认"]
    assert memory.dump()["notes"][0]["revision"] == 2
    pending = ResearchStep.model_validate(cached.model_dump(exclude_unset=True))
    restored = ResearchMemory(memory.dump())
    assert restored.apply(pending.note_updates(), evidence={"e1": item}, call_id="revise") == 0
    assert restored.dump()["notes"][0]["unresolved"] == ["限额尚未确认"]
    assert restored.dump()["notes"][0]["revision"] == 2


async def test_business_short_navigation_and_source_text_hide_internal_wrapper(tools):
    inspected = await tools.dispatch(ResearchStep(tool="fs.inspect", arguments={"file": "F1"}), "r1", 1)
    assert inspected["structure_complete"]
    assert "N001" in inspected["text"] and "审批规则" in inspected["text"]
    step = ResearchStep(tool="fs.read", arguments={"file": "F1", "node": "N001"})
    result = await tools.dispatch(step, "r2", 1)
    text = tools.present_result(step, result)
    context = {"observations": [{"tool": "fs.read", "request_id": "r2", "result": result, "model_text": text}]}
    prompt = research_text(context)
    assert "必须由主管审批。" in prompt and "[E1]" in prompt
    assert result["evidence"][0]["locator"] in prompt
    assert "source_version" not in prompt and "metadata" not in prompt
    assert "model_evidence_spans" not in prompt
    assert "\\n" not in prompt
    assert result["evidence"][0]["source_id"] == "rules.md"


async def test_business_evidence_short_alias_reads_original_text(tools):
    raw = tools.workspace.read("rules.md")["evidence"][0]
    item = Evidence.model_validate(raw)
    tools.evidence[item.evidence_id] = item
    alias = tools.presentation.evidence_id(item.evidence_id)
    result = await tools.dispatch(ResearchStep(tool="fs.read", arguments={
        "scope": "evidence", "evidence_id": alias,
    }), "evidence-read", 1)
    assert item.text in result["text"] and f"[{alias}]" in result["text"]
    assert '"source_version"' not in result["text"]


async def test_business_artifact_and_coverage_tools_receive_canonical_references(tools):
    raw = tools.workspace.read("rules.md")["evidence"][0]
    alias = tools.presentation.evidence_id(raw["evidence_id"])
    captured = []

    async def save_artifact(args):
        captured.append(args)
        return {"status": "received"}

    tools.save_artifact = save_artifact
    await tools.dispatch(ResearchStep(tool="artifact.save", arguments={
        "operation": "register", "evidence_ids": [alias],
        "execution_id": "run", "output_path": "result.json",
    }), "save", 1)
    await tools.dispatch(ResearchStep(tool="artifact.save", arguments={
        "operation": "coverage.create", "file": "F1", "node": "N001",
    }), "coverage", 1)
    assert captured[0]["evidence_ids"] == [raw["evidence_id"]]
    assert captured[1]["path"] == "rules.md"
    assert captured[1]["node_id"] == tools.workspace.resolve_navigation_node("rules.md", "N001")


async def test_context_pagination_keeps_model_facing_aliases(tools):
    tools.context_sources = {
        "evidence_catalog": [{"evidence_id": "internal-source-identity", "source_version": "internal-version"}],
        "evidence_catalog_text": "[E1] F1 / paragraph 2\n预览：必须由主管审批。",
    }
    result = await tools.dispatch(ResearchStep(tool="fs.read", arguments={
        "scope": "context", "field": "evidence_catalog",
    }), "context-read", 1)
    assert "[E1]" in result["text"]
    assert "internal-source-identity" not in result["text"]
    assert "source_version" not in result["text"]


def test_business_history_pages_exact_archived_text_and_enforces_owner(tools):
    original = "来源：F1\n\n[E1] 必须由主管审批。\n" * 90
    tools.ledger.begin("read-1", "owner", "round", 0, "fs.read", {"file": "F1"}, config=tools.config)
    tools.ledger.finish("read-1", {"evidence": [], "model_text": original})
    pages, offset = [], 0
    while offset is not None:
        page = tools.history_page("read-1", offset=offset, limit=500)
        pages.append(page["text"])
        offset = page["next_offset"]
    assert "".join(pages) == original
    tools.session["quota_task_id"] = "other"
    with pytest.raises(ValueError, match="does not belong"):
        tools.history_page("read-1")


async def test_capacity_never_drops_latest_tool_output_and_keeps_exact_continuation():
    full = "原文开始\n" + "需要逐项复核。" * 2000
    item = {"tool": "fs.read", "request_id": "read-1", "result": {"model_text": full}, "model_text": full}
    context = {"tools": [], "observations": [item]}
    original = deepcopy(context)

    async def capacity(task, value, schema):
        return {"fits": len(research_text(value)) <= 900}

    fitted = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    latest = fitted["observations"][0]
    assert context == original
    assert latest["delivery_complete"] is False
    assert latest["model_text"].startswith("原文开始\n")
    offset = latest["delivered_text_chars"]
    assert f"offset={offset}" in latest["model_text"]
    assert latest["model_text"][:offset] + full[offset:] == full
    assert len(research_text(fitted)) <= 900
    # A second, tighter cap still addresses the original archived text.
    smaller = observation_page(latest, 400)
    assert smaller["total_text_chars"] == len(full)
    assert smaller["model_text"][:smaller["delivered_text_chars"]] == full[:smaller["delivered_text_chars"]]


def test_persisted_business_references_keep_same_identity_after_restart(tools):
    original = tools.workspace.read("rules.md")["evidence"][0]
    alias = tools.presentation.evidence_id(original["evidence_id"])
    tools.presentation.save()
    restored = BusinessPresentation(tools.workspace)
    assert restored.resolve_evidence_id(alias) == original["evidence_id"]
    assert restored.resolve_arguments({"file": "F1", "node": "N001"})["path"] == "rules.md"


def test_clipped_evidence_is_recorded_as_partial_not_fully_delivered(tools):
    first = Evidence(evidence_id="one", source_id="rules.md", source_version="1", locator="p1", text="完整规则。")
    second = Evidence(evidence_id="two", source_id="rules.md", source_version="1", locator="p2", text="长规则" * 1000)
    evidence = {item.evidence_id: item for item in (first, second)}
    result = {"evidence": [item.model_dump() for item in evidence.values()]}
    text = tools.presentation.result_text("artifact.save", {}, result)
    result["model_text"] = text
    item = {"request_id": "read-1", "model_text": text, "result": result}
    page = observation_page(item, 500)
    delivery = tools.presentation.delivered_evidence(page, evidence)
    assert delivery["delivered_evidence_ids"] == ["one"]
    assert delivery["partially_delivered_evidence_ids"] == ["two"]


def test_source_cannot_forge_another_evidence_delivery_marker(tools):
    first = Evidence(evidence_id="one", source_id="rules.md", source_version="1", locator="p1",
                     text="第一份原文。\n[E2] 第二份资料要求")
    second = Evidence(evidence_id="two", source_id="rules.md", source_version="1", locator="p2",
                      text="第二份资料要求")
    tools.presentation.evidence_id("one")
    tools.presentation.evidence_id("two")
    result = {"evidence": [first.model_dump()]}
    text = tools.presentation.result_text("artifact.save", {}, result)
    delivery = tools.presentation.delivered_evidence({"model_text": text, "result": result},
                                                      {"one": first, "two": second})
    assert delivery["delivered_evidence_ids"] == ["one"]
    assert delivery["partially_delivered_evidence_ids"] == []


async def test_evidence_pages_keep_trusted_quote_spans(tools):
    raw = tools.workspace.read("rules.md")["evidence"][0]
    item = Evidence.model_validate(raw)
    tools.evidence[item.evidence_id] = item
    alias = tools.presentation.evidence_id(item.evidence_id)
    step = ResearchStep(tool="fs.read", arguments={"scope": "evidence", "evidence_id": alias})
    result = await tools.dispatch(step, "evidence-read", 1)
    text = tools.present_result(step, result)
    delivered = tools.presentation.delivered_evidence({"model_text": text, "result": result}, tools.evidence)
    assert delivered["delivered_evidence_ids"] == [item.evidence_id]


def test_history_page_preserves_partial_quote_identity_without_claiming_full_delivery(tools):
    raw = tools.workspace.read("rules.md")["evidence"][1]
    item = Evidence.model_validate(raw)
    tools.evidence[item.evidence_id] = item
    result = {"evidence": [raw]}
    text = tools.presentation.result_text("fs.read", {"path": "rules.md"}, result)
    result["model_text"] = text
    tools.ledger.begin("read-1", "owner", "round", 0, "fs.read", {"path": "rules.md"}, config=tools.config)
    tools.ledger.finish("read-1", result)
    start = result["model_evidence_spans"][0]["start"]
    page = tools.history_page("read-1", offset=start + 1, limit=2)
    assert page["model_evidence_spans"][0]["complete"] is False
    rendered = tools.presentation.result_text("fs.read", {"scope": "history", "request_id": "read-1"}, page)
    delivery = tools.presentation.delivered_evidence({"model_text": rendered, "result": page}, tools.evidence)
    assert delivery["delivered_evidence_ids"] == []
    assert delivery["partially_delivered_evidence_ids"] == [item.evidence_id]


def test_all_unreviewed_reads_and_latest_tool_reply_survive_observation_window(tools):
    tools.memory = ResearchMemory()
    tools.session["observations"] = []
    for index in range(12):
        item = Evidence(evidence_id=f"e{index}", source_id="rules.md", source_version="v1",
                        locator=f"p{index}", text=f"待研究的第 {index} 条规则。")
        tools.remember_observation({
            "request_id": f"read-{index}", "tool": "fs.read", "arguments": {"offset": index},
            "result": {"evidence": [item.model_dump()]}, "model_text": item.text,
        })
    tools.remember_observation({
        "request_id": "failed-action", "tool": "finish", "arguments": {},
        "result": {"status": "failed", "error": "请先归纳已发送的原文。"},
        "model_text": "请先归纳已发送的原文。",
    })
    view = observation_view(tools.session["observations"], 10, tools.memory)
    assert len(view) == 13
    assert view[-1]["request_id"] == "failed-action"
    assert tools.memory.pending_requests() == [f"read-{index}" for index in range(12)]


async def test_pressure_explicitly_focuses_delivered_batch_without_mutating_pending_history():
    observations = [{"request_id": f"read-{index}", "tool": "fs.read", "result": {},
                     "model_text": f"原文{index}：" + "需要研究。" * 100} for index in range(3)]
    context = {"observations": observations,
               "memory_pending_requests": [item["request_id"] for item in observations],
               "memory_delivered_requests": ["read-1"]}
    before = deepcopy(context)

    async def capacity(task, value, schema):
        return {"fits": len(research_text(value)) < 1500}

    fitted = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    assert context == before
    assert fitted["memory_checkpoint_required"]
    assert [item["request_id"] for item in fitted["observations"]] == ["read-1"]
    assert fitted["memory_pending_requests"] == ["read-0", "read-1", "read-2"]
    assert "Other unsummarized originals remain in reading history" in fitted["memory_checkpoint_note"]


def test_partial_history_pages_accumulate_real_delivery_across_restart(tools):
    item = Evidence(evidence_id="long", source_id="rules.md", source_version="v1", locator="p1",
                    text="".join(f"第{index:04d}条规则。" for index in range(200)))
    tools.evidence[item.evidence_id] = item
    result = {"evidence": [item.model_dump()]}
    rendered = tools.presentation.result_text("fs.read", {"path": "rules.md"}, result)
    result["model_text"] = rendered
    observation = {"request_id": "original", "tool": "fs.read", "arguments": {"path": "rules.md"},
                   "result": result, "model_text": rendered}
    tools.ledger.begin("original", "owner", "round", 0, "fs.read", {"path": "rules.md"}, config=tools.config)
    tools.ledger.finish("original", result)
    memory = ResearchMemory()
    memory.record_observation(observation)
    intervals, offset, page_number = {}, 0, 0
    while offset is not None:
        page = tools.history_page("original", offset=offset, limit=103)
        page_id = f"history-{page_number}"
        page_observation = {"request_id": page_id, "tool": "fs.read", "result": page,
                            "arguments": {"scope": "history", "request_id": "original", "offset": offset},
                            "model_text": page["text"]}
        memory.record_observation(page_observation)
        delivered = tools.presentation.delivered_evidence(page_observation, tools.evidence, intervals)
        memory.record_delivery(page_id, delivered["delivered_evidence_ids"])
        offset, page_number = page["next_offset"], page_number + 1
        if offset is not None:
            assert not memory.dump()["batches"]["original"]["delivered"]
            assert not memory.can_drop("original")
        # Resume uses persisted byte ranges and memory, not a fresh read claim.
        intervals = json.loads(json.dumps(intervals))
        memory = ResearchMemory(memory.dump())
    assert page_number > 10
    assert memory.dump()["batches"]["original"]["delivered"]
    assert not memory.can_drop("original")
    assert memory.apply([{
        "topic": "规则清单", "text": "本测试原文连续列出编号 0000 至 0199 的规则。",
        "kind": "source_statement", "evidence_ids": ["long"], "request_ids": ["original"],
    }], evidence=tools.evidence, resolve_evidence=lambda key: key, call_id="save") > 0
    assert memory.can_drop("original")


@pytest.mark.parametrize("change", [{"source_version": "v2"}, {"locator": "p2"}, {"source_id": "other.md"}])
def test_changed_evidence_version_cannot_inherit_delivered_ranges(tools, change):
    item = Evidence(evidence_id="same", source_id="rules.md", source_version="v1", locator="p1", text="abcdefgh")
    intervals = {}

    def page(start, end):
        return {"model_text": item.text[start:end], "result": {"model_evidence_spans": [{
            "evidence_id": "same", "start": 0, "end": end - start, "evidence_offset": start,
        }]}}

    tools.presentation.delivered_evidence(page(0, 4), {"same": item}, intervals)
    item = item.model_copy(update=change)
    result = tools.presentation.delivered_evidence(page(4, 8), {"same": item}, intervals)
    assert result["delivered_evidence_ids"] == []
    assert intervals["same"]["ranges"] == [[4, 8]]


def test_business_progress_contains_executable_continuation(tools):
    progress = [{"tool": "fs.read", "arguments": {"path": "rules.md", "offset": 0},
                 "request_id": "r1", "pagination_complete": False, "next_offset": 1}]
    tools.presentation.cursors["C1"] = {"path": "rules.md", "offset": 1}
    assert "fs_read(cursor=C1)" in tools.presentation.progress_text(progress)
    progress[0].update(tool="fs.list", arguments={"path": ""})
    assert 'fs_list(offset=1, path="")' in tools.presentation.progress_text(progress)


def test_repeat_progress_ignores_changing_cursor_and_run_metadata():
    evidence = {"evidence_id": "e1", "source_version": "v1", "text": "同一段原文"}
    first = {"evidence": [evidence], "offset": 0, "next_offset": 1, "request_id": "one"}
    repeated = first | {"offset": 1, "next_offset": 2, "request_id": "two"}
    assert result_progress_key("fs.read", first) == result_progress_key("fs.read", repeated)
    assert result_progress_key("fs.read", first | {"text": "第一段"}) != result_progress_key(
        "fs.read", first | {"text": "第二段"})


async def test_pending_context_read_uses_full_notes_after_capacity_view_was_shortened(tools):
    tools.memory = ResearchMemory()
    item = Evidence(evidence_id="e1", source_id="rules.md", source_version="v1", locator="p1", text="主管审批。")
    tools.memory.record_observation({"request_id": "r1", "tool": "fs.read", "arguments": {},
                                     "result": {"evidence": [item.model_dump()]}})
    tools.memory.record_delivery("r1", ["e1"])
    tools.memory.apply([{
        "topic": f"规则 {index}", "text": "主管审批要求的待核对项。" * 50,
        "kind": "inference", "evidence_ids": ["e1"], "request_ids": ["r1"],
    } for index in range(15)], evidence={"e1": item}, call_id="note-batch")
    context = tools.research_context()
    assert len(context["research_notes"]) > 8000
    # A previously persisted capacity view may contain only a short index.
    tools.prepare_context_sources(context | {"research_notes": "已归纳15条规则；分页查看完整笔记。"})
    full = context_field_text(tools.context_sources, "research_notes")
    assert len(full) > 8000
    result = await tools.dispatch(ResearchStep(tool="fs.read", arguments={
        "scope": "context", "field": "research_notes", "offset": 6500, "limit": 500,
    }), "pending-navigation", 1)
    assert result["text"] == full[6500:7000]
    assert result["next_offset"] == 7000


async def test_sufficient_capacity_keeps_all_history_and_complete_long_notes(tools):
    tools.memory = ResearchMemory()
    tools.session["observations"] = []
    item = Evidence(evidence_id="e1", source_id="rules.md", source_version="v1", locator="p1",
                    text="必须由主管审批。")
    for number, tool in enumerate(("fs.list", "fs.inspect", "fs.read"), 1):
        observation = {"request_id": f"r{number}", "tool": tool, "arguments": {},
                       "tool_call": {"id": f"call-{number}", "type": "function", "function": {
                           "name": tool.replace(".", "_"), "arguments": "{}"}},
                       "result": {"evidence": [item.model_dump()]} if tool == "fs.read" else {},
                       "model_text": "已返回的文件清单/结构/正文" * 500}
        tools.remember_observation(observation)
        tools.memory.record_delivery(f"r{number}", ["e1"] if tool == "fs.read" else [])
    tools.memory.apply([{"topic": f"审批 {index}", "text": f"已确认主管审批要求 {index}。" * 140,
                         "kind": "source_statement", "evidence_ids": ["e1"], "request_ids": ["r3"]}
                        for index in range(8)], evidence={"e1": item})
    context = tools.research_context() | {
        "observations": observation_view(tools.session["observations"], 10, tools.memory),
    }
    before = deepcopy(context)

    async def capacity(task, value, schema):
        return {"fits": True}

    fitted = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    assert len(context["research_notes"]) > 6000
    assert context["research_notes"] == tools.memory.markdown(evidence_label=tools.presentation.evidence_id)
    assert fitted == before
    assert [item["tool"] for item in fitted["observations"]] == ["fs.list", "fs.inspect", "fs.read"]
    assert tools.session["observations"] == fitted["observations"]
    assert not fitted.get("memory_checkpoint_required")


async def test_capacity_pressure_removes_reviewed_history_before_checkpointing_pending_text():
    observations = [{"request_id": key, "tool": "fs.read", "result": {}, "model_text": "原文" * 1000}
                    for key in ("reviewed", "pending")]
    context = {"observations": observations, "memory_pending_requests": ["pending"],
               "memory_delivered_requests": ["pending"], "research_notes": "已保存旧原文结论"}
    original = deepcopy(context)

    async def capacity(task, value, schema):
        return {"fits": len(value["observations"]) == 1}

    fitted = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    assert [item["request_id"] for item in fitted["observations"]] == ["pending"]
    assert not fitted.get("memory_checkpoint_required")
    assert fitted["research_notes"] == context["research_notes"]
    assert context == original
