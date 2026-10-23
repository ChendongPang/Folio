"""Navigation instructions must refer to actual callable API function names."""

import json
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest

from folio.research.context_views import fit_context, observation_page
from folio.research.contracts import ResearchStep
from folio.research.native_tools import native_result
from folio.research.presentation import SourcePresentation
from folio.research.transport import research_messages, research_prompt_chars
from folio.research.workspace import WorkspaceError, WorkspaceManager
from folio.runtime.base import RuntimeResult


@pytest.fixture
def presentation(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for index in range(2):
        (sources / f"rules{index}.md").write_text("# 规则\n交易需要审批。\n", encoding="utf-8")
    return SourcePresentation(WorkspaceManager(sources, tmp_path / "work"))


def test_catalog_continuation_names_native_list_function(presentation):
    result = presentation.workspace.list(limit=1)
    text = presentation.catalog_text(result)
    assert "fs_list(offset=1)" in text
    assert "fs.list(" not in text


@pytest.fixture
def uploaded_presentation(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    names = ["a" * 32 + "__中国 AI 基础设施.md", "b" * 32 + "__HBM 供给.md"]
    for name in names:
        (sources / name).write_text("# 分析\n资料正文。\n\n## 后续\n更多资料。", encoding="utf-8")
    return SourcePresentation(WorkspaceManager(sources, tmp_path / "work"))


def test_unique_display_filename_resolves_to_snapshot_source(uploaded_presentation):
    presentation = uploaded_presentation
    path = presentation.resolve_file("F1")
    assert presentation.resolve_file("中国 AI 基础设施.md") == path
    assert presentation.resolve_arguments({"file": "中国 AI 基础设施.md", "path": "F1"}) == {"path": path}
    assert presentation.resolve_file("../中国 AI 基础设施.md") == "../中国 AI 基础设施.md"
    assert presentation.resolve_file("中国 AI") == "中国 AI"


def test_duplicate_display_names_require_explicit_file_id(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for prefix in ("a", "b"):
        (sources / (prefix * 32 + "__报告.md")).write_text("# 报告\n正文。", encoding="utf-8")
    presentation = SourcePresentation(WorkspaceManager(sources, tmp_path / "work"))
    with pytest.raises(WorkspaceError, match='multiple sources.*F1、F2'):
        presentation.resolve_file("报告.md")
    assert presentation.resolve_file("F1") != presentation.resolve_file("F2")
    assert presentation.resolve_file("a" * 32 + "__报告.md") == "a" * 32 + "__报告.md"


def test_cursor_accepts_same_file_display_name_and_redundant_position(uploaded_presentation):
    presentation = uploaded_presentation
    path = presentation.resolve_file("F1")
    node = presentation.workspace.resolve_navigation_node(path, "N001")
    presentation.cursors["C4"] = {"path": path, "node_id": node, "offset": 1}
    for file in (path, "F1", "中国 AI 基础设施.md"):
        resolved = presentation.resolve_arguments({"cursor": "C4", "file": file, "max_chars": 16000})
        assert resolved == presentation.cursors["C4"] | {"max_chars": 16000}
    assert presentation.resolve_arguments({"cursor": "C4", "node": "N001", "offset": 1}) == presentation.cursors["C4"]
    # The returned location executes against the real source snapshot.
    result = presentation.workspace.read(**presentation.resolve_arguments({"cursor": "C4", "file": "中国 AI 基础设施.md"}))
    assert result["offset"] == 1


@pytest.mark.parametrize("extra", [{"file": "F2"}, {"offset": 0}, {"page": 1}, {"scope": "history"}])
def test_cursor_rejects_conflicting_file_or_position(uploaded_presentation, extra):
    presentation = uploaded_presentation
    presentation.cursors["C4"] = {"path": presentation.resolve_file("F1"), "offset": 1}
    with pytest.raises(WorkspaceError, match="identify different"):
        presentation.resolve_arguments({"cursor": "C4", **extra})
    with pytest.raises(WorkspaceError, match="R IDs identify read ranges only"):
        presentation.resolve_arguments({"cursor": "R3"})


def test_progress_keeps_continuation_after_original_result_is_archived(presentation):
    selector = {"path": "rules0.md"}
    presentation.cursors["C7"] = selector | {"offset": 1}
    progress = [{"range_id": "r1", "selector": selector, "total": 3, "complete": False,
                 "pages": [(0, 1)], "delivered_evidence_ids": ["e1"]}]
    text = presentation.progress_text(progress)
    assert "fs_read(cursor=C7)" in text
    assert presentation.resolve_arguments({"cursor": "C7"}) == selector | {"offset": 1}
    # Discontiguous reads must continue at the first missing part, not skip it.
    progress[0]["pages"] += [(2, 3)]
    assert "fs_read(cursor=C7)" in presentation.progress_text(progress)


def test_long_paragraph_cursor_restores_exact_character_after_restart(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    original = "abcdefghijklmnopqrstuvwxyz" * 200
    (sources / "long.txt").write_text(original, encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "work")
    presentation = SourcePresentation(workspace)
    args = {"path": "long.txt"}
    first = workspace.read(**args, max_chars=3500)
    first["pagination_reason"] = "context_capacity"
    text = presentation.result_text("fs.read", args, first)
    assert "source block 1 through 1, starting at character 3500" in text
    assert "available request capacity" in text
    cursor = re.search(r"fs_read\(cursor=(C\d+)\)", text)[1]
    presentation.save()

    restored = SourcePresentation(workspace)
    resumed = restored.resolve_arguments({"cursor": cursor})
    assert resumed == args | {"offset": 0, "character_offset": 3500}
    last = workspace.read(**resumed)
    assert "".join(item["text"] for page in (first, last) for item in page["evidence"]) == original
    assert last["complete"]
    assert "End of the selected range." in restored.result_text("fs.read", resumed, last)
    with pytest.raises(WorkspaceError, match="identify different"):
        restored.resolve_arguments({"cursor": cursor, "character_offset": 0})


def test_progress_preserves_partial_block_position(presentation):
    selector = {"path": "rules0.md"}
    presentation.cursors["C8"] = selector | {"offset": 1, "character_offset": 3}
    progress = [{"selector": selector, "total": 3, "complete": False,
                 "next_offset": 1, "next_character_offset": 3,
                 "pages": [(0, 1)], "delivered_evidence_ids": ["e1"]}]
    assert "fs_read(cursor=C8)" in presentation.progress_text(progress)
    # Starting a later block must not carry the prior block's character offset.
    assert "character_offset" not in presentation.continuation_command(
        "fs.read", selector | {"character_offset": 3}, 2)


def test_legacy_fixed_chunk_cursor_migrates_without_skipping_source(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    original = "abcdefg" * 800
    (sources / "long.txt").write_text(original + "\nLast paragraph", encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "work")
    presentation = SourcePresentation(workspace)
    # Prior versions' offset=1 meant the second 2,000-character piece.
    workspace._atomic_json(presentation.path, {
        "snapshot_id": workspace.source_snapshot_id,
        "cursors": {"C1": {"path": "long.txt", "offset": 1}},
    })
    restored = SourcePresentation(workspace)
    args = restored.resolve_arguments({"cursor": "C1"}, tool="fs.read")
    assert args == {"path": "long.txt", "offset": 0, "character_offset": 2000}
    result = workspace.read(**args)
    assert result["evidence"][0]["text"] == original[2000:]
    assert result["evidence"][1]["text"] == "Last paragraph"
    restored.save()
    assert SourcePresentation(workspace).resolve_arguments({"cursor": "C1"}, tool="fs.read") == args


@pytest.mark.parametrize("tool", ["fs.read", "fs.search"])
def test_continuation_is_callable_and_restores_exact_range_after_restart(presentation, tool):
    args = {"path": "rules0.md", "offset": 0, "limit": 1}
    if tool == "fs.search":
        args["query"] = "审批"
    text = presentation._continuation(tool, args, {"next_offset": 1})
    function, cursor = re.search(r"(fs_\w+)\(cursor=(C\d+)\)", text).groups()
    assert function == tool.replace(".", "_")
    presentation.save()
    restored = SourcePresentation(presentation.workspace)
    call = {"id": "actual_call", "type": "function", "function": {
        "name": function,
        "arguments": json.dumps({"cursor": cursor, "summary": "继续阅读相关原文"}, ensure_ascii=False),
    }}
    step = native_result("directory_research", RuntimeResult("", tool_calls=[call]), {}, {})["steps"][0]
    assert step["tool_call"] == call
    assert restored.resolve_arguments(step["arguments"]) == {
        key: value for key, value in args.items() if key != "limit"
    } | {"offset": 1}


async def test_bounded_business_history_preserves_original_call_after_resume():
    raw_call = {"id": "provider_call_17", "type": "function", "function": {
        "name": "fs_read", "arguments": '{ "file": "F1", "node": "N001", "summary": "核对规则" }',
    }}
    step = ResearchStep.model_validate(native_result(
        "research_step", RuntimeResult("", tool_calls=[raw_call]), {}, ResearchStep.model_json_schema())["steps"][0])
    restored = ResearchStep.model_validate_json(step.model_dump_json())
    assert restored.tool_call == raw_call
    full_text = "原始资料正文。" * 3000
    observation = {"tool": restored.tool, "tool_call": restored.tool_call, "request_id": "ledger-read-17",
                   "result": {"model_text": full_text}, "model_text": full_text}
    first_page = observation_page(observation, 4000)
    context = {"observations": [first_page]}
    before = deepcopy(context)
    schema = ResearchStep.model_json_schema()
    floor = research_prompt_chars({"observations": [observation_page(observation, 300)]},
                                  schema, task="research_step")

    async def capacity(task, value, _model):
        return {"fits": research_prompt_chars(value, schema, task=task) <= floor + 400}

    fitted = await fit_context(SimpleNamespace(request_capacity=capacity), context, ResearchStep)
    assert context == before
    shown = fitted["observations"][0]
    assert shown["tool_call"] == raw_call
    assert shown["delivery_complete"] is False
    assert shown["model_text"][:shown["delivered_text_chars"]] == full_text[:shown["delivered_text_chars"]]
    messages = research_messages(fitted, schema, task="research_step")
    assert messages[-2]["tool_calls"] == [raw_call]
    assert messages[-1]["tool_call_id"] == raw_call["id"]
    assert "ledger-read-17" in messages[-1]["content"]
