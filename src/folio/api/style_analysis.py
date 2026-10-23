"""Conversation-owned style analysis, independent of directory generation."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from folio.document.prepare import DirectoryRuntimeBudget
from folio.document.style_contracts import DIMENSION_LABELS, DIMENSIONS, StyleAnalysisConfig
from folio.document.style_profile import prepare_style_profile, validate_profile_sources
from folio.research.source_index import source_version
from folio.research.workflow import local
from folio.runtime.llm import LLMRuntime

_IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
_AVAILABLE = {"analyzed", "partial", "measured_only", "analysis_failed"}
_STATUS = {
    "analyzed": "可提取文本的风格分析已完成", "partial": "部分完成，仍有未确认项",
    "measured_only": "已完成程序统计，语义风格未分析", "analysis_failed": "语义分析未完成，仅保留程序统计",
}
_SCOPES = {
    "general": "一般正文", "body": "正文", "headings": "标题", "tables": "表格",
    "cover": "封面", "appendix": "附录", "technical": "技术类章节", "management": "管理类章节",
}


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"无效{label}")
    return value


def _line(value):
    return " ".join(str(value).replace("`", "").split())


def _bounded_lines(lines, max_chars, *, for_model=False):
    if max_chars is None:
        return "\n".join(lines)
    if max_chars <= 0:
        return ""
    result = []
    used = 0
    for line in lines:
        if used + len(line) + bool(result) > max_chars:
            marker = (
                "(Summary truncated; see the complete style guide.)"
                if for_model else "（摘要已限长，完整内容见风格说明。）"
            )
            if used + len(marker) + 1 <= max_chars:
                result.append(marker)
            break
        result.append(line)
        used += len(line) + (len(result) > 1)
    return "\n".join(result)


def _paragraph_parameters(style, label, *, for_model=False):
    names = {
        "east_asia_font": "中文字体", "font_name": "西文字体", "font_size_pt": "字号(pt)",
        "bold": "加粗", "italic": "斜体", "color": "文字颜色", "alignment": "对齐",
        "space_before_pt": "段前间距(pt)", "space_after_pt": "段后间距(pt)",
        "first_line_indent_pt": "首行缩进(pt)", "left_indent_pt": "左缩进(pt)", "right_indent_pt": "右缩进(pt)",
    }
    observations = []
    for key, name in names.items():
        if key not in style or style[key] is None:
            continue
        value = style[key]
        if for_model:
            name = key
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, bool) else value
        elif key in {"bold", "italic"}:
            value = "是" if value else "否"
        elif key == "alignment":
            value = {"left": "左对齐", "center": "居中", "right": "右对齐", "justify": "两端对齐"}.get(value, "待确认")
        observations.append(f"{label}{name}：{_line(value)}")
    if style.get("line_spacing") is not None:
        unit = {"multiple": "倍", "exactly": "pt（固定值）", "at_least": "pt（最小值）"}.get(style.get("line_spacing_rule", "multiple"), "（规则待确认）")
        observations.append(
            f"{label}line_spacing: {style['line_spacing']} ({style.get('line_spacing_rule', 'multiple')})"
            if for_model else f"{label}行距：{style['line_spacing']} {unit}"
        )
    return observations


def _dimension_summary(document, dimension, *, for_model=False):
    """Only derived rules and numeric/format measurements, never raw source text."""
    rules = [
        (f"Scope: {rule.get('scope', 'unresolved')}; " if for_model else f"适用范围：{_SCOPES.get(rule.get('scope'), '待确认')}；")
        + _line(rule["description"])
        + ((" (local observation; needs more samples)" if for_model else "（局部观察，待更多样本确认）")
           if rule.get("confidence") == "tentative" else "")
        for rule in document.get("rules", [])
        if rule.get("dimension") == dimension and isinstance(rule.get("description"), str)
    ] if document.get("status") in {"analyzed", "partial"} else []
    measured = document.get("measurements", {})
    layout = document.get("layout", {})
    observations = []
    if dimension in {"structure", "headings"}:
        levels = measured.get("heading_counts_by_level", {})
        if levels:
            observations.append(
                "Heading levels: " + ", ".join(f"level {level}: {count}" for level, count in levels.items())
                if for_model else "标题层级：" + "、".join(f"{level}级 {count}个" for level, count in levels.items())
            )
    elif dimension == "detail" and measured.get("paragraph_count"):
        if measured.get("paragraph_count_reliable", True):
            observations.append(
                f"{measured['paragraph_count']} body paragraphs, median {measured.get('median_paragraph_chars', 0)} characters; reference for level of detail only, not a length quota"
                if for_model else
                f"{measured['paragraph_count']}个正文段落，段落中位数{measured.get('median_paragraph_chars', 0)}字符；仅为详略参考，不是字数配额"
            )
        else:
            count = measured.get("text_block_count", measured["paragraph_count"])
            observations.append(
                f"{count} extracted text blocks, not actual body paragraphs; do not infer paragraph length from this count"
                if for_model else f"提取{count}个文本块；不等同于真实正文段落，不能据此推断段落长度"
            )
    elif dimension == "figures_tables":
        if measured.get("table_count_reliable", True):
            observations.append(
                f"Program-identified tables: {measured.get('table_count', 0)}"
                if for_model else f"程序识别{measured.get('table_count', 0)}个表格"
            )
        else:
            observations.append(
                "Text extraction provides no reliable table count; do not infer that the original has no tables"
                if for_model else "当前文本解析未提供可靠表格数量，不能视为原文无表格"
            )
        if measured.get("table_column_counts"):
            observations.append(("Table column counts: " if for_model else "表格列数：") + ", ".join(map(str, measured["table_column_counts"])))
    elif dimension == "typography":
        observations.extend(_paragraph_parameters(layout.get("body", {}), "Body " if for_model else "正文", for_model=for_model))
        for level in range(1, 7):
            style = layout.get("headings", {}).get(str(level), {})
            observations.extend(_paragraph_parameters(style, f"Heading {level} " if for_model else f"{level}级标题", for_model=for_model))
        table = layout.get("table", {})
        observations.extend(_paragraph_parameters(table.get("header", {}), "Table header " if for_model else "表头", for_model=for_model))
        observations.extend(_paragraph_parameters(table.get("body", {}), "Table body " if for_model else "表格正文", for_model=for_model))
    elif dimension == "page":
        page = layout.get("page", {})
        for key, name in (("width_mm", "纸宽"), ("height_mm", "纸高"), ("margin_top_mm", "上边距"), ("margin_bottom_mm", "下边距"), ("margin_left_mm", "左边距"), ("margin_right_mm", "右边距")):
            if key in page:
                observations.append(f"{key}: {page[key]}mm" if for_model else f"{name}{page[key]}mm")
    elif dimension == "visual":
        table = layout.get("table", {})
        for key, name in (("header_fill", "表头底色"), ("border_color", "边框颜色"), ("border_size_pt", "边框宽度(pt)")):
            if key in table:
                observations.append(f"{key}: {_line(table[key])}" if for_model else f"{name}：{_line(table[key])}")
    observations.extend(
        _line(item["text"]) for item in document.get("layout_coverage", {}).get("observations", [])
        if item.get("dimension") == dimension and item.get("text")
    )
    check = document.get("dimension_coverage", {}).get(dimension, {})
    if check.get("reason") and (not rules or check.get("status") == "unresolved"):
        observations.append(_line(check["reason"]))
    return "；".join([*observations, *rules]) or (
        "Insufficiently verified; do not derive new requirements from it"
        if for_model else "未充分确认，不据此新增要求"
    )


def profile_context(profile, *, max_chars=None, for_model=False):
    """Compact derived style context; excludes excerpts, locators, titles and audits."""
    documents = profile.get("documents", []) if isinstance(profile, dict) else []
    if not documents:
        return ""
    guidance = (
        "Use only as a reference for expression and formatting, not business facts; explicit user requirements and templates take precedence. Extractable-text and page-image coverage are recorded separately; completed text analysis does not mean all page images were viewed."
        if for_model else
        "仅参考表达形式和排版，不提供业务事实；用户明确要求和模板优先。可提取文本与页面视觉的覆盖分别记录；文字分析完成不代表已查看所有页面图片。"
    )
    lines = ["# Extracted style observations" if for_model else "# 已提取的风格说明", guidance]
    for index, document in enumerate(documents, 1):
        label = _line(document.get("reference_name") or (f"Reference {index}" if for_model else f"示例 {index}"))
        label = re.sub(r"([\\*_#\[\]<>])", r"\\\1", label)
        reference_id = document.get("reference_id")
        if reference_id:
            label += f" (attachment {_line(reference_id)})" if for_model else f"（附件 {_line(reference_id)}）"
        status = document.get("status", "unresolved") if for_model else _STATUS.get(document.get('status'), '仍有未确认项')
        if document.get('status') == 'analyzed' and not document.get('text_coverage'):
            status = "Legacy sample analysis completed" if for_model else "旧版抽样分析已完成"
        lines.append(f"## {label}：{status}")
        coverage = document.get("text_coverage")
        if coverage:
            lines.append(
                f"- Text coverage: {coverage.get('characters_read', 0)}/{coverage.get('characters_total', 0)} characters; "
                + ("All extractable text was read." if coverage.get("complete") else "Some extractable text remains unread.")
                if for_model else
                f"- 文本覆盖：{coverage.get('characters_read', 0)}/{coverage.get('characters_total', 0)}字符；"
                + ("可提取文本已全部读取。" if coverage.get("complete") else "仍有可提取文本未读取。")
            )
        else:
            lines.append(
                "- Legacy sampling records do not establish full extractable-text coverage."
                if for_model else "- 旧版记录仅为抽样分析，未证明可提取文本全部覆盖。"
            )
        # Reserve room for every dimension instead of cutting off the later
        # dimensions when the first one contains many detailed observations.
        per_dimension = (max(80, (max_chars // max(1, len(documents)) - 700) // len(DIMENSIONS))
                         if max_chars is not None else None)
        for dimension, name in DIMENSIONS.items():
            if for_model:
                name = DIMENSION_LABELS[dimension]
            summary = _dimension_summary(document, dimension, for_model=for_model)
            if per_dimension is not None and len(summary) > per_dimension:
                parts, used = [], 0
                for part in summary.split("；"):
                    if used + len(part) + 1 > per_dimension - 24:
                        break
                    parts.append(part)
                    used += len(part) + 1
                summary = "；".join(parts) + (
                    "; See complete style.md for remaining observations" if for_model else "；其余已记录特征见完整 style.md"
                )
            lines.append(f"- {name}：{summary}")
        limitations = [item for item in document.get("uncertainties", []) if isinstance(item, str)]
        lines.extend(
            ("- Recorded limitation: " if for_model else "- 已记录的限制：") + _line(item)
            for item in limitations
        )
    return _bounded_lines(lines, max_chars, for_model=for_model)


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze(folder, attachments):
    sources = []
    for item in attachments:
        path = Path(item["path"])
        version = source_version(path)  # Reject symlinked source paths as well as missing files.
        destination = folder / "sources" / (item["id"] + path.suffix.lower())
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        if source_version(destination) != version or source_version(path) != version:
            raise ValueError("风格示例在复制期间发生变化，请重试。")
        sources.append({
            "id": item["id"], "name": _line(item.get("name", path.name))[:240],
            "sha256": version, "path": destination.relative_to(folder).as_posix(),
        })
    return sources


async def _close_runtime(runtime):
    closing = asyncio.create_task(runtime.aclose())
    interrupted = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            interrupted = True
    await closing
    if interrupted:
        raise asyncio.CancelledError


class ConversationStyles:
    def __init__(self, root: Path):
        self.state_root = Path(root).absolute()
        self.root = self.state_root / "conversation-styles"

    def _folder(self, revision):
        _identifier(revision, "风格分析版本")
        folder = self.root / revision
        for path in (self.root, folder):
            if path.is_symlink():
                raise ValueError("风格分析目录不能使用符号链接")
        if not folder.resolve().is_relative_to(self.state_root.resolve()):
            raise ValueError("风格分析目录超出工作区")
        return folder

    async def analyze(self, config, conversation_id, attachments, *, on_progress=None, user_request=""):
        _identifier(conversation_id, "会话编号")
        selected = [dict(item) for item in attachments if item.get("role") == "style_reference"]
        if not selected:
            raise ValueError("请先选择用途为“风格示例”的文件，再分析风格。")
        for item in selected:
            _identifier(item.get("id"), "附件编号")
        if len({item["id"] for item in selected}) != len(selected):
            raise ValueError("风格示例文件不能重复选择")
        revision = uuid4().hex
        folder = self._folder(revision)
        folder.mkdir(parents=True, exist_ok=False)
        metadata = {"revision": revision, "conversation_id": conversation_id, "status": "preparing", "created_at": datetime.now(UTC).isoformat(), "sources": []}
        _write_json(folder / "metadata.json", metadata)
        try:
            metadata["sources"] = await local(_freeze, folder, selected)
            metadata["status"] = "analyzing"
            _write_json(folder / "metadata.json", metadata)
            runtime = LLMRuntime(config)
            settings = StyleAnalysisConfig()
            budget = DirectoryRuntimeBudget(runtime, settings.max_calls)
            try:
                profile = await prepare_style_profile(
                    [folder / source["path"] for source in metadata["sources"]],
                    folder / "style", runtime=budget, cache_root=self.state_root / "source-cache",
                    config=settings, on_progress=on_progress, user_request=user_request,
                )
            finally:
                await _close_runtime(runtime)
            validate_profile_sources(profile, [folder / source["path"] for source in metadata["sources"]])
            metadata.update(status=profile["status"], cache_hit=bool(profile.get("cache_hit")), completed_at=datetime.now(UTC).isoformat(), analysis_calls=budget.used)
            _write_json(folder / "metadata.json", metadata)
            return self._public(metadata, profile)
        except BaseException as exc:
            # Keep frozen originals and audit on cancellation/failure. No reader thread
            # can race a cleanup, and incomplete revisions never enter chat context.
            metadata.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error_type=type(exc).__name__)
            _write_json(folder / "metadata.json", metadata)
            raise

    def _public(self, metadata, profile):
        summary_profile = profile | {"documents": [
            document | {"reference_name": source["name"], "reference_id": source["id"]}
            for document, source in zip(profile["documents"], metadata["sources"], strict=True)
        ]}
        return {
            "revision": metadata["revision"], "conversation_id": metadata["conversation_id"],
            "status": metadata["status"], "document_names": [source["name"] for source in metadata["sources"]],
            "style_url": f"/conversations/{metadata['conversation_id']}/styles/{metadata['revision']}",
            "summary": profile_context(summary_profile, max_chars=None), "cache_hit": bool(profile.get("cache_hit")),
            "source_versions": [{"id": source["id"], "sha256": source["sha256"]} for source in metadata["sources"]],
        }

    def load(self, revision, conversation_id):
        _identifier(conversation_id, "会话编号")
        folder = self._folder(revision)
        for path in (folder / "metadata.json", folder / "style", folder / "style" / "profile.json", folder / "style" / "style.md"):
            if path.is_symlink():
                raise ValueError("风格分析文件不能使用符号链接")
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("conversation_id") != conversation_id or metadata.get("revision") != revision:
            raise ValueError("风格分析不属于当前会话")
        if metadata.get("status") not in _AVAILABLE:
            raise ValueError("风格分析尚未形成可读取的结果")
        profile = json.loads((folder / "style" / "profile.json").read_text(encoding="utf-8"))
        paths = []
        for source in metadata["sources"]:
            _identifier(source.get("id"), "附件编号")
            path = folder / source["path"]
            if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink():
                raise ValueError("风格示例快照路径无效")
            if source_version(path) != source.get("sha256"):
                raise ValueError("风格示例快照内容已变化")
            paths.append(path)
        validate_profile_sources(profile, paths)
        if profile.get("status") != metadata["status"]:
            raise ValueError("风格分析状态与记录不一致")
        return {"folder": folder, "profile": profile, "metadata": metadata, "sample_paths": paths}

    def context(self, reports, attachments, max_chars=None, *, conversation_id=None):
        if max_chars is not None and max_chars <= 0:
            return ""
        documents = self.documents(reports, attachments, conversation_id=conversation_id)
        return profile_context({"documents": documents}, max_chars=max_chars, for_model=True)

    def documents(self, reports, attachments, *, conversation_id=None):
        """Latest valid per-file observations for merging with other owned results."""
        current = {}
        for item in attachments:
            if item.get("role") != "style_reference":
                continue
            try:
                current[item["id"]] = source_version(Path(item["path"]))
            except (KeyError, OSError, ValueError):
                continue
        selected, seen = [], set()
        for report in reversed(reports or []):
            if current.keys() <= seen:
                break
            try:
                owner = conversation_id or report.get("conversation_id")
                saved = self.load(report["revision"], owner)
            except (KeyError, TypeError, OSError, ValueError):
                continue
            if saved["metadata"]["status"] not in _AVAILABLE:
                continue
            for index, source in enumerate(saved["metadata"]["sources"]):
                if source["id"] in seen or current.get(source["id"]) != source["sha256"]:
                    continue
                document = saved["profile"]["documents"][index]
                if document.get("status") not in _AVAILABLE:
                    continue
                seen.add(source["id"])
                selected.append(document | {"reference_name": source["name"], "reference_id": source["id"]})
        return selected
