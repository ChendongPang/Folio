"""Research tools use English instructions while product progress is bilingual."""

import hashlib
import json
import re
from pathlib import Path
from string import Formatter

import pytest

from folio.i18n import translate_text
from folio.research.artifacts import _evidence
from folio.research.planning import _has_unread_content
from folio.research.planning_progress import (
    action_message,
    error_message,
    gap_message,
    result_message,
)
from folio.research.source_index import SourceIndex
from folio.research.workspace import WorkspaceError, WorkspaceManager, _pending_ocr_warning


@pytest.mark.parametrize("titles,suffix,expected", [
    (["2.5.1 Event Calendar", "2.5.2 Job Board"], "等 6 节", '“2.5.1 Event Calendar”, “2.5.2 Job Board” and others (6 sections total)'),
    (["正文", "标题"], "", '“正文”, “标题”'),
    (["原始中文标题"], "", '“原始中文标题”'),
])
def test_complete_review_progress_translates_without_changing_titles(titles, suffix, expected):
    from folio.i18n import localize_payload

    source = "跨章节检查第 16/47 组：" + "、".join(f"《{title}》" for title in titles) + suffix + "。"
    english = f"Cross-section review, group 16/47: {expected}."
    assert translate_text(source, "en") == english
    result = localize_payload({"text": source}, "en", event="work.update")
    assert result["text"] == english
    assert result["_translations"][source] == english
    assert translate_text(english, "zh-CN") == source


async def test_engine_without_explicit_language_uses_user_instruction_for_word_labels(store, inputs):
    from docx import Document

    from folio.models import JobInput
    from folio.orchestration.engine import Engine
    from folio.runtime.demo import DemoRuntime

    request = JobInput(**(inputs | {
        "title": "Trading requirements",
        "user_request": "# Foreign exchange\nDescribe trading requirements and approval rules.",
    }), runtime="demo", ui_language="zh-CN")
    assert request.language is None
    job_id = store.create(request)
    engine = Engine(store, DemoRuntime())
    job = await engine.run(job_id)
    assert engine.request.language == "en"
    text = "\n".join(paragraph.text for paragraph in Document(job["artifact"]).paragraphs)
    assert "Contents" in text and "Demo output:" in text
    assert "演示产物：" not in text and "目录" not in text


@pytest.mark.parametrize("kind,key,original,current", [
    ("coverage", "e1", "未覆盖项目材料：附件二", "Project source not covered: 附件二"),
    ("coverage", "required-table:t1", "缺少模板要求的独立表格 t1，列名须为 Name / 说明，且至少有 2 行数据",
     "Required standalone template table t1 is missing. It must have columns Name / 说明 and at least 2 data rows"),
    ("pending", "record-only:r1", "待填写元数据：报价。用户原文", "Metadata to fill in: 报价。用户原文"),
    ("unsupported", "derived-evidence:e1", "派生研究证据不可用：用户原因", "Derived research evidence is unavailable: 用户原因"),
    ("unsupported", "derived-evidence:e1", "派生研究证据不可用：派生证据记录签名无效", "Derived research evidence is unavailable: Invalid derived-evidence record signature"),
    ("unsupported", "derived-evidence:e1", "派生研究证据不可用：研究脚本或结果文件已变化：artifacts/原文.json", "Derived research evidence is unavailable: The research script or result file changed: artifacts/原文.json"),
])
def test_known_rule_identity_preserves_legacy_hash_without_collapsing_user_text(kind, key, original, current):
    from folio.models import Finding

    legacy = Finding(kind=kind, key=key, description=original, section_ids=["s1"])
    translated = legacy.model_copy(update={"description": current})
    assert translated.fingerprint == legacy.fingerprint
    changed = translated.model_copy(update={"description": current + " another source"})
    assert changed.fingerprint != translated.fingerprint
    assert translated.description == current


def test_freeform_model_findings_are_not_translated_for_identity():
    from folio.models import Finding

    original = Finding(kind="pending", key="model-1", description="资料不足", section_ids=["s1"])
    translated = original.model_copy(update={"description": "Insufficient source material"})
    assert original.fingerprint != translated.fingerprint


def test_research_catalog_preserves_every_interpolated_argument():
    path = Path(__file__).parents[1] / "src/folio/locales/research.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    formatter = Formatter()
    for chinese, english in catalog.items():
        assert english.strip(), chinese
        original = {field for _, field, _, _ in formatter.parse(chinese) if field is not None}
        translated = {field for _, field, _, _ in formatter.parse(english) if field is not None}
        assert translated == original, chinese
        assert not re.search(r"[\u3400-\u9fff]", english), chinese


@pytest.mark.parametrize("warning", [
    "2 页未完整读取：未提取到文字。",
    "2 pages were not fully read: no text was extracted.",
    "Excel contains unread images, charts, or drawings.",
    "The image has not undergone OCR.",
])
def test_unread_source_detection_accepts_current_and_persisted_warnings(warning):
    assert _has_unread_content({"warnings": [warning]})


def test_navigation_only_warning_does_not_imply_unread_business_content():
    assert not _has_unread_content({"warnings": [
        "The PDF has no usable bookmark outline. Navigation uses actual page numbers."
    ]})


@pytest.mark.parametrize("warning", [
    "图片尚未进行 OCR", "The image has not undergone OCR.", "2 页未提取到文字",
])
def test_ocr_completion_clears_obsolete_warnings_in_both_languages(warning):
    assert _pending_ocr_warning(warning)
    assert not _pending_ocr_warning("OCR text is machine-recognized. Verify key values.")


def test_model_tool_errors_are_english_and_can_be_displayed_in_chinese(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "source.md").write_text("# 标题\n原始业务资料", encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "work")
    with pytest.raises(WorkspaceError, match="snapshot root") as failure:
        workspace.read(path="../secret.md")
    assert not re.search(r"[\u3400-\u9fff]", str(failure.value))
    assert translate_text(str(failure.value), "zh-CN") == "资料路径不能越过快照根目录"
    assert workspace.read(path="source.md")["evidence"][1]["text"] == "原始业务资料"


def test_progress_localizes_composed_actions_without_translating_source_titles():
    files = {"投标资料.pdf": {}}
    args = {"path": "投标资料.pdf", "node_id": "root"}
    titles = {("投标资料.pdf", "root"): "Full text"}
    message = translate_text(action_message("fs.read", args, files, titles), "en")
    assert "Reading" in message and "body text" in message
    assert "投标资料.pdf" in message
    assert "正在" not in message
    result = translate_text(result_message("fs.read", args, {"evidence": [{"text": "source"}]}, files, titles), "en")
    assert "body text" in result and "的正文" not in result
    assert "6 characters" in result and "投标资料.pdf" in result


def test_structure_counts_and_paginated_read_progress_have_no_untranslated_fragments():
    files = {"source.pdf": {}}
    structure = translate_text(result_message(
        "fs.inspect", {"path": "source.pdf"}, {"structure": [{"kind": "page"}]}, files, {},
    ), "en")
    assert "1 internal structure entries from" in structure
    for continuation in ({}, {"next_offset": 2}, {"context_limited": True}):
        result = {
            "evidence": [{"text": "source", "metadata": {"page": 2}}],
            "offset": 0, "total": 5,
        } | continuation
        message = translate_text(result_message(
            "fs.read", {"path": "source.pdf", "page": 2}, result, files, {},
        ), "en")
        assert "6 characters" in message and "segments 1 of 5" in message
        assert not re.search(r"[\u3400-\u9fff]", message), message


def test_error_progress_classifies_english_and_legacy_note_failures_identically():
    old = error_message("fs.read", {}, {}, "笔记引用必须来自实际发送给模型的原文")
    new = error_message("fs.read", {}, {}, "Note citations must refer to original text actually delivered to the model")
    assert new == old
    text = gap_message({"validation_issues": [
        "The file has not been selected or given an exclusion reason: F1",
        "Unread content affects the outline scope: F1",
        "Citations for requirement R1 have not been delivered to the model",
    ]}, 1, 3)
    assert all(marker in text for marker in ("资料筛选", "关键原文", "目录要求"))


def test_existing_signed_artifact_text_is_not_rewritten_by_locale_changes(tmp_path):
    record = {
        "artifact_id": "artifact-1", "task_root": str(tmp_path),
        "registration": {
            "description": "Business calculation", "result_text": "42",
            "execution_id": "run-1", "environment": {},
            "output": {"path": "result.txt"},
        },
        "validation": {"status": "verified"},
    }
    original = _evidence(record, tmp_path / "manifest.json")
    current = _evidence(record | {"evidence_text_format": 2}, tmp_path / "manifest.json")
    assert original.text == "Business calculation\n计算结果：\n42"
    assert current.text == "Business calculation\nCalculation result:\n42"
    assert original.evidence_id == current.evidence_id


def test_cached_navigation_labels_upgrade_without_reparsing_or_changing_source_ids(tmp_path, monkeypatch):
    from folio.research import source_index

    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "requirements.md").write_text("# 原始标题\n正文保留", encoding="utf-8")
    index = SourceIndex(sources)
    index.prepare()
    original = index.structure("requirements.md")
    snapshot = index.source_snapshot_id
    target = index.navigation_path("requirements.md")
    legacy = index.navigation("requirements.md").replace("Full text", "全文")
    target.write_text(legacy, encoding="utf-8")
    manifest = index.manifest
    manifest["files"]["requirements.md"].update(
        navigation_version=1,
        navigation_hash=hashlib.sha256(legacy.encode()).hexdigest(),
    )
    (index.index_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def reject_reparse(*args, **kwargs):
        pytest.fail("Changing a generated label must not reparse source data")

    monkeypatch.setattr(source_index, "read_source", reject_reparse)
    restored = SourceIndex(sources)
    restored.prepare()
    assert "Full text" in restored.navigation("requirements.md")
    assert "原始标题" in restored.navigation("requirements.md")
    assert restored.source_snapshot_id == snapshot
    assert restored.structure("requirements.md") == original
