import pytest

from folio.models import JobInput
from folio.requirements import (
    RequirementSnapshot,
    RequirementsTooLarge,
    mark_requirement,
    pending_requirements,
    sync_requirements,
)


def test_direct_job_input_enforces_requirement_capacity_and_text_consistency(inputs):
    boundary = RequirementSnapshot.from_text("要求" * 300000)
    assert JobInput(**(inputs | {"user_request": ""}), requirements=boundary).user_request == boundary.text
    with pytest.raises(ValueError, match="600,000"):
        JobInput(**inputs, requirements=RequirementSnapshot.from_text("要求" * 300001))
    snapshot = RequirementSnapshot.from_text("编写一期功能")
    assert JobInput(**(inputs | {"user_request": ""}), requirements=snapshot).user_request == snapshot.text
    with pytest.raises(ValueError, match="must match"):
        JobInput(**(inputs | {"user_request": "编写二期功能"}), requirements=snapshot)


def test_old_constraints_survive_long_history_without_summarization_or_truncation():
    original = "只编写一期功能；不得添加二期功能。" + "明确约束。" * 3000
    state = {"messages": [{"role": "user", "content": original}]}
    for index in range(12):
        state["messages"].extend(
            [
                {"role": "assistant", "content": "讨论"},
                {"role": "user", "content": f"补充约束 {index}"},
            ]
        )
    snapshot = sync_requirements(state)
    assert snapshot.entries[0].text == original
    assert original in snapshot.text and "补充约束 11" in snapshot.text
    assert snapshot.revision == sync_requirements(state).revision
    assert RequirementSnapshot.model_validate(state["requirements"]).text == snapshot.text


def test_revision_original_is_kept_once_and_ui_commands_do_not_change_version():
    state = {"messages": [{"role": "user", "content": "只写一期"}]}
    before = sync_requirements(state)
    state["messages"].append(
        {
            "role": "user",
            "action": "revise_directory",
            "advice": "改为包含二期",
            "content": "修改建议：改为包含二期",
        }
    )
    revised = sync_requirements(state)
    assert revised.text == "只写一期\n\n改为包含二期"
    assert revised.revision != before.revision
    state["messages"].extend(
        [
            {
                "role": "user",
                "action": "regenerate_directory",
                "content": "根据最新要求重新生成目录",
            },
            {"role": "user", "action": "confirm_directory", "content": "确认目录，生成文档"},
        ]
    )
    assert sync_requirements(state).revision == revised.revision


def test_capacity_failure_keeps_full_requirements_and_explains_no_truncation():
    original = "规则" * 300_001
    state = {"messages": [{"role": "user", "content": original}]}
    snapshot = sync_requirements(state)
    with pytest.raises(RequirementsTooLarge, match="未截断"):
        snapshot.ensure_capacity()
    assert state["requirements"]["entries"][0]["text"] == original


def test_migrates_old_revision_history_and_detects_snapshot_tampering():
    state = {"messages": [{"role": "user", "content": "需求说明书"}]}
    snapshot = sync_requirements(state, revision_history=["补充例外处理"])
    assert snapshot.entries[-1].kind == "revision"
    assert sync_requirements(state, revision_history=["补充例外处理"]) == snapshot
    state["requirements"]["entries"][0]["text"] = "被修改的需求"
    with pytest.raises(ValueError, match="revision"):
        RequirementSnapshot.model_validate(state["requirements"])


@pytest.mark.parametrize(
    "text,intent",
    [
        ("为什么目录里有审批管理这一章？", "chat"),
        ("审批上限改成100万元有什么影响？", "analyze"),
        ("审批这块再考虑一下", "clarify"),
        ("分析这个风格示例的正文写法和排版", "analyze_style"),
    ],
)
def test_nonrequirements_keep_audit_without_changing_effective_version(text, intent):
    state = {"messages": [{"role": "user", "content": "只写一期"}]}
    before = sync_requirements(state)
    message = {"role": "user", "content": text, "action": "chat"}
    mark_requirement(message)
    state["messages"].append(message)
    assert sync_requirements(state) == before  # Not active while classification is pending.
    mark_requirement(message, intent=intent)
    assert sync_requirements(state) == before
    assert message["content"] == text
    assert message["requirement"]["intent"] == intent


def test_explicit_revision_with_question_mark_is_not_lost_and_retains_source_order():
    state = {"messages": [{"role": "user", "content": "只写一期，审批上限50万元"}]}
    original = sync_requirements(state)
    message = {"role": "user", "content": "请把审批上限改为100万元可以吗？"}
    mark_requirement(message, intent="requirements")
    state["messages"].append(message)
    revised = sync_requirements(state)
    assert revised.revision != original.revision
    assert revised.entries[0] == original.entries[0]
    assert revised.entries[1].source_id == "message:1"
    assert revised.entries[1].kind == "revision"
    assert revised.entries[1].text == message["content"]


def test_classification_failure_stays_pending_and_exact_resend_resolves_without_duplicate():
    original = {"role": "user", "content": "审批上限改为100万元"}
    mark_requirement(original)
    state = {"messages": [original]}
    assert not sync_requirements(state).entries
    assert pending_requirements(state) == [
        {"source_id": "message:0", "text": original["content"]}
    ]
    retry = {"role": "user", "content": original["content"]}
    mark_requirement(retry, intent="requirements")
    state["messages"].append(retry)
    snapshot = sync_requirements(state)
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].source_id == "message:1"
    assert original["requirement"]["resolved_by"] == "message:1"
    assert pending_requirements(state) == []


def test_legacy_frozen_history_is_never_reclassified_or_removed():
    state = {
        "messages": [
            {"role": "user", "content": "只写一期"},
            {"role": "user", "content": "为什么目录里有审批管理这一章？"},
        ]
    }
    original = sync_requirements(state)
    assert len(original.entries) == 2
    assert state["messages"][1]["requirement"]["source"] == "legacy"
    assert sync_requirements(state) == original
