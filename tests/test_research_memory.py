"""Research survives context eviction without upgrading summaries to evidence."""

import pytest

from folio.research.memory import ResearchMemory, ResearchNoteUpdate


def evidence(key="ev_1", *, version="v1", page=1):
    return {"evidence_id": key, "source_id": "中国AI.pdf", "source_version": version,
            "locator": f"page {page}/block 1", "text": "原文" * 50}


def observe(memory, request="read_1", items=None, **extra):
    items = items or [evidence()]
    memory.record_observation({"request_id": request, "tool": "fs.read", "arguments": {"path": "中国AI.pdf", "page": 1},
                               "result": {"evidence": items, **extra}})
    return {item["evidence_id"]: item for item in items}


def note(**changes):
    return {"topic": "芯片供给", "text": "报告讨论了供给约束；目录需明确分析口径。",
            "kind": "source_statement", "evidence_ids": ["ev_1"], **changes}


def test_note_schema_only_requires_evidence_and_ignores_legacy_receipt_ids():
    schema = ResearchNoteUpdate.model_json_schema()
    assert set(schema["required"]) == {"topic", "text", "kind", "evidence_ids"}
    assert "request_ids" not in schema["properties"]
    old = note(request_ids=["search_1", "nonexistent"])
    parsed = ResearchNoteUpdate.model_validate(old)
    assert parsed.model_dump(exclude_unset=True) == note()
    assert old["request_ids"] == ["search_1", "nonexistent"]  # No mutation of cached replies.


def test_large_note_keeps_all_delivered_evidence_and_still_rejects_unknown_refs():
    memory = ResearchMemory()
    items = [evidence(f"ev_{index}", page=index) for index in range(1, 141)]
    raw = observe(memory, items=items)
    memory.record_delivery("read_1", raw)
    update = note(evidence_ids=list(raw), text="完整研究结论。" * 400,
                  unresolved=[f"待补充材料 {index}" for index in range(30)])
    assert memory.apply([update], evidence=raw, call_id="large") > 0
    assert memory.dump()["notes"][0]["evidence_ids"] == list(raw)
    with pytest.raises(ValueError, match='actually delivered'):
        memory.apply([update | {"evidence_ids": list(raw) + ["never_read"]}], evidence=raw)
    assert len(memory.dump()["notes"]) == 1


@pytest.mark.parametrize("topic", [None, "", "   "])
def test_missing_topic_uses_existing_text_without_changing_findings(topic):
    update = note(topic=topic, text="\n已读取招标资格要求。\n企业资质需要用户补充。")
    if topic is None:
        update.pop("topic")
    original = dict(update)
    parsed = ResearchNoteUpdate.model_validate(update)
    assert parsed.topic == "已读取招标资格要求。"
    assert parsed.text == update["text"]
    assert parsed.evidence_ids == update["evidence_ids"]
    assert update == original


def test_native_save_accepts_missing_note_topics_and_preserves_provider_envelope():
    import json

    from folio.research.contracts import ResearchBatch
    from folio.research.native_tools import native_result
    from folio.runtime.base import RuntimeResult

    notes = [note(text=f"范围 {index} 已核对。", evidence_ids=[f"E{i}" for i in range(140)])
             for index in range(31)]
    for update in notes:
        update.pop("topic")
    call = {"id": "save_notes", "type": "function", "function": {
        "name": "research_save", "arguments": json.dumps({"research_notes": notes, "summary": "保存研究结果"}),
    }}
    parsed = ResearchBatch.model_validate(native_result(
        "research_step", RuntimeResult("", tool_calls=[call]), {}, {},
    ))
    step = parsed.steps[0]
    assert len(step.research_notes) == 31
    assert len(step.research_notes[0].evidence_ids) == 140
    assert step.tool_call == call
    assert step.note_updates()[0].topic == notes[0]["text"]
    assert "topic" not in json.loads(step.tool_call["function"]["arguments"])["research_notes"][0]


def test_receipts_are_inferred_from_actual_source_delivery_not_legacy_input():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_observation({"request_id": "search_1", "tool": "fs.search", "arguments": {},
                               "result": {"evidence": list(raw.values())}})
    memory.record_delivery("search_1", raw)
    legacy_note = note(request_ids=["search_1", "nonexistent"])
    with pytest.raises(ValueError, match='actually delivered'):
        memory.apply([legacy_note], evidence=raw)
    memory.record_delivery("read_1", raw)
    memory.apply([legacy_note], evidence=raw, call_id="call1")
    assert memory.dump()["notes"][0]["request_ids"] == ["read_1"]
    assert memory.apply([note()], evidence=raw, call_id="call1") == 0
    assert "read_1" not in memory.context_text()
    assert "归纳批次" not in memory.markdown()


def test_notes_require_actual_delivery_not_just_extraction():
    memory = ResearchMemory()
    raw = observe(memory)
    with pytest.raises(ValueError, match='actually delivered'):
        memory.apply([note()], evidence=raw, call_id="call1")
    assert not memory.can_drop("read_1")
    memory.record_delivery("read_1", raw)
    memory.apply([note(evidence_ids=["E1"])], evidence=raw, resolve_evidence=lambda key: "ev_1", call_id="call1")
    assert memory.can_drop("read_1")
    assert memory.dump()["notes"][0]["evidence_ids"] == ["ev_1"]
    assert memory.dump()["notes"][0]["request_ids"] == ["read_1"]
    assert "page 1/block 1" in memory.markdown()
    assert "原文原文" not in str(memory.dump())


def test_capacity_slice_can_be_cited_without_marking_original_read():
    memory = ResearchMemory()
    raw = observe(memory)
    part = evidence("slice")
    part.update(locator=part["locator"] + "/characters 0:10", text=part["text"][:10])
    memory.record_delivery_evidence("read_1", [part])
    memory.record_delivery("read_1", ["slice"], complete=False)
    raw["slice"] = part
    memory.apply([note(evidence_ids=["slice"])], evidence=raw)
    batch = memory.dump()["batches"]["read_1"]
    assert batch["delivered_evidence_ids"] == ["slice"]
    assert batch["delivered"] is False
    assert not memory.can_drop("read_1")
    assert batch["evidence_ids"] == ["ev_1", "slice"]
    memory.record_delivery_evidence("read_1", [part])
    assert memory.dump()["batches"]["read_1"]["evidence_ids"] == ["ev_1", "slice"]


def test_capacity_slice_cannot_claim_a_different_source_read():
    memory = ResearchMemory()
    observe(memory)
    part = evidence("slice", page=2)
    part["locator"] += "/characters 0:10"
    before = memory.dump()
    with pytest.raises(ValueError, match="outside"):
        memory.record_delivery_evidence("read_1", [part])
    assert memory.dump() == before


def test_restart_omission_and_replay_preserve_notes():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    assert memory.apply([note()], evidence=raw, call_id="call1") > 0
    saved = memory.dump()
    memory = ResearchMemory(saved)
    assert memory.apply([], evidence=raw, call_id="call2") == 0
    assert memory.apply([note()], evidence=raw, call_id="call1") == 0
    assert memory.dump() == saved
    with pytest.raises(ValueError, match='same tool call'):
        memory.apply([note(text="偷偷覆盖")], evidence=raw, call_id="call1")
    assert memory.dump() == saved


def test_updates_are_atomic_and_explicit_revision_preserves_previous_claim():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    before = memory.dump()
    with pytest.raises(ValueError, match='actually delivered'):
        memory.apply([note(), note(evidence_ids=["unseen"])], evidence=raw)
    assert memory.dump() == before
    memory.apply([note()], evidence=raw)
    memory.apply([note(note_id="M1", text="修正：这是作者预测，尚待核实。", kind="inference", unresolved=["核对预测时点"])], evidence=raw)
    item = memory.dump()["notes"][0]
    assert item["revision"] == 2 and item["kind"] == "inference"
    assert item["revisions"][0]["text"] == note()["text"]
    assert "Model inference" in memory.markdown() and "核对预测时点" in memory.markdown()
    with pytest.raises(ValueError, match='source version changed'):
        memory.apply([note()], evidence={"ev_1": evidence(version="v2")})


def test_note_revision_does_not_silently_resolve_omitted_questions():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note(unresolved=["需要核对统计年份"])], evidence=raw)
    memory.apply([ResearchNoteUpdate.model_validate(note(note_id="M1", text="补充比较范围"))], evidence=raw)
    assert memory.dump()["notes"][0]["unresolved"] == ["需要核对统计年份"]
    memory.apply([note(note_id="M1", text="已核对年份", unresolved=[])], evidence=raw)
    assert memory.dump()["notes"][0]["unresolved"] == []


def test_partial_delivery_and_history_read_never_mark_unseen_pages_reviewed():
    memory = ResearchMemory()
    raw = observe(memory, items=[evidence(), evidence("ev_2", page=2)])
    memory.record_delivery("read_1", ["ev_1"], complete=False)
    memory.apply([note()], evidence=raw)
    assert memory.pending_requests() == ["read_1"]
    observe(memory, "history_1", [raw["ev_2"]], original_request_id="read_1")
    memory.record_delivery("history_1", ["ev_2"])
    assert memory.dump()["batches"]["read_1"]["delivered"]
    assert not memory.can_drop("read_1")
    memory.apply([note()], evidence=raw)
    assert not memory.can_drop("read_1")  # Repeating an old note does not summarize the new page.
    memory.apply([note(topic="第二页", evidence_ids=["ev_2"])], evidence=raw)
    assert memory.can_drop("read_1") and memory.can_drop("history_1")


def test_one_cited_excerpt_does_not_mark_the_whole_delivered_batch_reviewed():
    memory = ResearchMemory()
    raw = observe(memory, items=[evidence(), evidence("ev_2", page=2)])
    memory.record_delivery("read_1", raw)
    memory.apply([note()], evidence=raw)
    batch = memory.dump()["batches"]["read_1"]
    assert batch["delivered"] and not batch["reviewed"]
    assert batch["reviewed_evidence_ids"] == ["ev_1"]
    assert memory.pending_requests() == ["read_1"]
    memory.apply([note(topic="第二页", evidence_ids=["ev_2"])], evidence=raw)
    assert memory.can_drop("read_1")


def test_direct_continuation_can_complete_previous_batch_delivery():
    memory = ResearchMemory()
    raw = observe(memory, items=[evidence(), evidence("ev_2", page=2)])
    memory.record_delivery("read_1", ["ev_1"], complete=False)
    observe(memory, "read_2", [raw["ev_2"]])
    memory.record_delivery("read_2", ["ev_2"])
    assert memory.dump()["batches"]["read_1"]["delivered"]
    assert not memory.can_drop("read_1")  # Delivery alone is still not a research note.


def test_new_batch_inherits_delivery_and_review_for_identical_evidence():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note()], evidence=raw)
    observe(memory, "reread", [raw["ev_1"]])
    batch = memory.dump()["batches"]["reread"]
    assert batch["delivered_evidence_ids"] == batch["reviewed_evidence_ids"] == ["ev_1"]
    assert batch["delivered"] and batch["reviewed"]
    assert memory.can_drop("reread")
    assert memory.pending_requests() == []


def test_reread_with_new_page_only_keeps_new_evidence_pending():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note()], evidence=raw)
    raw = observe(memory, "reread", [evidence(), evidence("ev_2", page=2)])
    batch = memory.dump()["batches"]["reread"]
    assert batch["delivered_evidence_ids"] == batch["reviewed_evidence_ids"] == ["ev_1"]
    assert not batch["delivered"] and not batch["reviewed"]
    assert memory.pending_requests() == ["reread"]
    memory.record_delivery("reread", ["ev_2"])
    memory.apply([note()], evidence=raw)
    assert memory.pending_requests() == ["reread"]
    memory.apply([note(evidence_ids=["ev_2"])], evidence=raw)
    assert memory.pending_requests() == []


@pytest.mark.parametrize("change", [{"source_id": "另一份.pdf"}, {"source_version": "v2"}, {"locator": "page 2/block 1"}])
def test_changed_reference_never_inherits_delivery_or_review_even_with_same_evidence_id(change):
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note()], evidence=raw)
    changed = evidence() | change
    observe(memory, "changed", [changed], original_request_id="read_1")
    batch = memory.dump()["batches"]["changed"]
    assert batch["delivered_evidence_ids"] == batch["reviewed_evidence_ids"] == []
    with pytest.raises(ValueError, match='source version changed or its location does not match'):
        memory.apply([note()], evidence={"ev_1": changed})
    memory.record_delivery("changed", ["ev_1"])
    assert memory.dump()["batches"]["changed"]["reviewed_evidence_ids"] == []
    memory = ResearchMemory(memory.dump())
    assert not memory.can_drop("changed")
    memory.apply([note()], evidence={"ev_1": changed})
    assert memory.dump()["notes"][-1]["request_ids"] == ["changed"]
    assert memory.can_drop("changed")


def test_review_does_not_propagate_to_different_locator_already_delivered():
    memory = ResearchMemory()
    first = observe(memory)
    second = observe(memory, "different", [evidence(page=2)])
    memory.record_delivery("read_1", first)
    assert memory.dump()["batches"]["different"]["delivered_evidence_ids"] == []
    memory.record_delivery("different", second)
    memory.apply([note()], evidence=first)
    assert not memory.can_drop("different")
    memory.apply([note(request_ids=["different"])], evidence=first)
    assert memory.dump()["notes"][0]["request_ids"] == ["read_1"]
    assert not memory.can_drop("different")
    memory.apply([note()], evidence=second)
    assert memory.can_drop("different")


def test_each_receipt_only_reviews_matching_versions_of_its_cited_evidence():
    memory = ResearchMemory()
    first = observe(memory, items=[evidence(), evidence("ev_2", page=2)])
    second = observe(memory, "changed", [evidence("ev_2", version="v2", page=2)])
    memory.record_delivery("read_1", first)
    memory.record_delivery("changed", second)
    memory.apply([note(evidence_ids=["ev_1", "ev_2"])], evidence=first | second)
    batches = memory.dump()["batches"]
    assert batches["read_1"]["reviewed_evidence_ids"] == ["ev_1"]
    assert not batches["read_1"]["reviewed"]
    assert batches["changed"]["reviewed_evidence_ids"] == ["ev_2"]
    assert batches["changed"]["reviewed"]
    assert memory.dump()["notes"][0]["request_ids"] == ["read_1", "changed"]


def test_restart_repairs_old_reviewed_but_undelivered_reread_checkpoint():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note()], evidence=raw)
    observe(memory, "reread", [raw["ev_1"]])
    saved = memory.dump()
    saved["batches"]["reread"].update(delivered=False, delivered_evidence_ids=[])
    restored = ResearchMemory(saved)
    assert restored.can_drop("reread")
    assert restored.pending_requests() == []


def test_partial_history_delivery_cannot_mark_changed_original_version_read():
    memory = ResearchMemory()
    observe(memory)
    observe(memory, "history", [evidence(version="v2")], original_request_id="read_1")
    memory.record_delivery("history", ["ev_1"])
    assert memory.dump()["batches"]["read_1"]["delivered_evidence_ids"] == []


def test_readable_index_bounded_notes_and_repeat_reason():
    memory = ResearchMemory()
    raw = observe(memory)
    memory.record_delivery("read_1", raw)
    memory.apply([note(text="长笔记" * 600)], evidence=raw)
    view = memory.context_text(max_chars=400, evidence_label=lambda key: "E1")
    assert len(view) <= 400 and "research_notes" in view and "M1" in view
    history = memory.history_text(evidence_label=lambda key: "E1")
    assert all(text in history for text in ["中国AI.pdf", "page 1/block 1", "E1", "Summarized"])
    args = {"path": "中国AI.pdf", "page": 1}
    assert "read_1" in memory.check_repeat("fs.read", args)
    assert memory.check_repeat("fs.read", args, "核对预测年份") is None
    assert memory.check_repeat("fs.read", args | {"page": 2}) is None
    assert memory.record_attempt("a", False) == 1
    assert memory.record_attempt("a", False) == 1
    assert memory.record_attempt("b", False) == 2
    assert memory.record_attempt("c", True) == 0


def test_native_note_fields_are_metadata_and_checkpoint_only_offers_save():
    import json

    from folio.research.native_tools import native_result, tool_definitions
    from folio.runtime.base import RuntimeResult

    args = {"summary": "记录芯片供给约束", "research_notes": [note()], "review_reason": "核对预测年份", "file": "F1"}
    call = {"id": "call1", "type": "function", "function": {"name": "fs_read", "arguments": json.dumps(args)}}
    result = native_result("directory_research", RuntimeResult("", tool_calls=[call]), {}, {})["steps"][0]
    assert result["arguments"] == {"file": "F1"}
    assert result["research_notes"] == args["research_notes"]
    assert result["review_reason"] == args["review_reason"]
    tools = tool_definitions("research_step", {"memory_checkpoint_required": True}, {})
    assert [item["function"]["name"] for item in tools] == ["research_save"]
    with pytest.raises(ValueError, match="not available"):
        native_result("directory_research", RuntimeResult("", tool_calls=[call]), {"memory_checkpoint_required": True}, {})
