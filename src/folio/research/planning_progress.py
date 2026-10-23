"""Public activity descriptions derived from actual directory research actions."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{32}[-_]+", re.IGNORECASE)
_INTERNAL = re.compile(
    r"fs[._](?:list|inspect|search|read)|\b(?:arguments_json|node_id|range_id|evidence_id|tool_call_id)\b"
    r"|(?:^|\s)(?:/|[a-zA-Z]:\\)|\.\./|[{}\[\]`]|[0-9a-f]{32}",
    re.IGNORECASE,
)
_WEB_PRIVATE = re.compile(r"(?:api[_-]?key|authorization|password|secret|access[_-]?token)\s*[:=]|\bbearer\s+\S+", re.IGNORECASE)


def _web_text(value, maximum=500):
    return "" if isinstance(value, str) and _WEB_PRIVATE.search(value) else public_text(value, maximum)


def _public_url(value):
    if not isinstance(value, str) or _WEB_PRIVATE.search(value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def web_research_view(report):
    """Public navigation only; omit source bodies, provider payloads and settings."""
    if not isinstance(report, dict):
        return None
    evidence = []
    for item in report.get("evidence", []):
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        url = _public_url(item.get("url") or metadata.get("url") or item.get("source_id"))
        if url and isinstance(item.get("evidence_id"), str):
            evidence.append({
                "evidence_id": item["evidence_id"], "url": url,
                "title": _web_text(item.get("title") or metadata.get("title"), 200) or urlsplit(url).hostname,
                "retrieved_at": _web_text(item.get("retrieved_at"), 80),
            })
    known = {item["evidence_id"]: item for item in evidence}
    searches = []
    for item in report.get("searches", []):
        if not isinstance(item, dict):
            continue
        ids = [key for key in item.get("evidence_ids", []) if key in known]
        record = {
            "query": _web_text(item.get("query")), "purpose": _web_text(item.get("purpose"), 1000),
            "status": item.get("status") if item.get("status") in {"completed", "failed", "unavailable", "interrupted"} else "failed",
            "cached": item.get("cached") is True, "evidence_ids": ids,
            "source_count": len({known[key]["url"] for key in ids}),
        }
        if isinstance(item.get("error_code"), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item["error_code"]):
            record["error_code"] = item["error_code"]
        searches.append(record)
    findings = [
        {"text": _web_text(item.get("text"), 1000),
         "evidence_ids": [key for key in item.get("evidence_ids", []) if key in known]}
        for item in report.get("findings", []) if isinstance(item, dict) and _web_text(item.get("text"), 1000)
    ]
    return {"available": report.get("available") is True, "searches": searches,
            "evidence": evidence, "findings": findings}


def public_text(value, maximum=100):
    """Only brief presentation text; never render tool payloads or model notes."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or _INTERNAL.search(text):
        return ""
    return text[:maximum].rstrip("，,；;。 ") + ("…" if len(text) > maximum else "")


def source_name(path):
    name = PurePosixPath(str(path).replace("\\", "/")).name
    return _UPLOAD_PREFIX.sub("", name)


def source_label(args, files):
    path = args.get("path")
    return f"《{source_name(path)}》" if isinstance(path, str) and path in files else "所选资料"


def range_label(args, node_titles):
    path, node = args.get("path"), args.get("node_id")
    title = public_text(node_titles.get((path, node))) if isinstance(path, str) and isinstance(node, str) else ""
    if title:
        return "的正文" if title in {"全文", "Full text"} else f"的“{title}”"
    sheet = public_text(args.get("sheet"), 60)
    if sheet:
        cells = args.get("cell_range", "")
        cells = cells if isinstance(cells, str) and re.fullmatch(r"[A-Z]+\d+(?::[A-Z]+\d+)?", cells) else ""
        return f"的工作表“{sheet}”" + (f"（{cells}）" if cells else "")
    for selector, unit in (("page", "页"), ("slide", "张幻灯片")):
        value = args.get(selector)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return f"的第 {value} {unit}"
    return "的正文"


def _scope_label(args):
    scope = args.get("scope", "sources")
    if scope == "context":
        return {
            "requirements": "完整编制要求", "template": "模板的完整约束",
            "research_notes": "已保存的研究笔记", "research_history": "资料阅读历史索引",
        }.get(args.get("field"), "任务上下文")
    if scope == "history":
        return "此前的资料阅读记录"
    return "已经读取的原文依据"


def action_message(tool, args, files, node_titles, summary=""):
    if tool == "search.web":
        query, purpose = _web_text(args.get("query"), 120), _web_text(args.get("purpose"), 200)
        message = "正在联网查找" + (f"“{query}”" if query else "与目录有关的公开资料")
        if purpose:
            message += f"。目录用途：{purpose}"
        return message + "。"
    file = source_label(args, files)
    more = "继续" if any(type(args.get(key)) is int and args[key] > 0
                       for key in ("offset", "character_offset")) else ""
    if tool == "research.save":
        message = "正在归纳已读原文，保存有出处的研究笔记"
    elif tool == "fs.list":
        message = f"正在{more}浏览资料文件清单，查找与本次文档有关的文件"
    elif tool == "fs.inspect":
        message = f"正在{more}查看{file}的内部结构，定位需要阅读的章节或页面"
    elif tool == "fs.search":
        query = public_text(args.get("query"), 60)
        target = file if isinstance(args.get("path"), str) and args["path"] in files else "上传资料"
        message = f"正在{target}中查找" + (f"“{query}”的相关内容" if query else "相关要求的位置")
    elif args.get("scope", "sources") != "sources":
        message = f"正在{more}查看{_scope_label(args)}"
    else:
        message = f"正在{more}读取{file}{range_label(args, node_titles)}，核对目录需要覆盖的要求"
    # summary is an optional public purpose, never a substitute for known action.
    purpose = public_text(summary, 100) if "/" not in summary and "\\" not in summary else ""
    if purpose and purpose not in message:
        message += f"。本步目的：{purpose}"
    return message + "。"


def result_message(tool, args, result, files, node_titles):
    if tool == "search.web":
        query = _web_text(args.get("query") or result.get("query"), 120)
        target = f"“{query}”" if query else "本次查询"
        status = result.get("status")
        if status == "unavailable":
            return f"联网搜索暂不可用，未取得{target}的公开资料；已保留记录。"
        if status == "interrupted":
            return f"对{target}的联网搜索已中断，已保留记录。"
        if status != "completed":
            return f"对{target}的联网搜索未成功，已保留记录，继续核对现有目录依据。"
        evidence = result.get("evidence", [])
        sources = {item.get("url") or item.get("metadata", {}).get("url") or item.get("source_id")
                   or item.get("evidence_id") for item in evidence if isinstance(item, dict)}
        sources.discard(None)
        count = result.get("source_count")
        if type(count) is not int or count < 0:
            count = len(sources)
        prefix = "已复用缓存中的搜索结果" if result.get("cached") else "联网搜索完成"
        continuation = "；部分摘录已返回，其余可继续读取" if result.get("web_result_page") and not result.get("complete") else ""
        return f"{prefix}：{target}，取得 {count} 个公开来源{continuation}，正在核对其与目录的关系。"
    file = source_label(args, files)
    continuation = "；还有后续内容可继续读取" if result.get("next_offset") is not None else ""
    if continuation and result.get("pagination_reason") == "context_capacity":
        continuation = "；本次可用上下文容量不足，剩余正文已保留续读位置"
    if tool == "research.save":
        return "本批研究笔记已保存，正在安排后续资料阅读。"
    if tool == "fs.list":
        count = len(result.get("files", []))
        return f"已取得本页 {count} 份文件信息{continuation}，正在选择需要查看内部结构的资料。"
    if tool == "fs.inspect":
        nodes = result.get("structure", [])
        kinds = {node.get("kind") for node in nodes}
        unit = "个章节标题" if kinds and kinds <= {"heading", "section", "bookmark"} else "条内部结构信息"
        return f"已获取{file}的 {len(nodes)} {unit}{continuation}，正在判断需要阅读的内容。"
    if tool == "fs.search":
        count = len(result.get("evidence", []))
        return f"已找到 {count} 条相关片段的位置，正在选择需要读取原文上下文的范围。"
    if args.get("scope", "sources") != "sources":
        return f"已取得{_scope_label(args)}的 {len(result.get('text', ''))} 字内容{continuation}，正在核对后续阅读安排。"
    evidence = result.get("evidence", [])
    characters = sum(len(item.get("text", "")) for item in evidence)
    details = []
    pages = sorted({item.get("metadata", {}).get("page") for item in evidence
                    if type(item.get("metadata", {}).get("page")) is int
                    and item["metadata"]["page"] > 0})
    if pages:
        if len(pages) == 1:
            details.append(f"第 {pages[0]} 页")
        elif pages == list(range(pages[0], pages[-1] + 1)):
            details.append(f"第 {pages[0]}—{pages[-1]} 页")
        elif len(pages) <= 4:
            details.append("第 " + "、".join(map(str, pages)) + " 页")
        else:
            details.append(f"涉及 {len(pages)} 页")
    offset, total = result.get("offset"), result.get("total")
    if type(offset) is int and type(total) is int and total > 0 and evidence:
        end = offset + len(evidence)
        interval = str(end) if end == offset + 1 else f"{offset + 1}—{end}"
        details.append(f"第 {interval} 段 / 共 {total} 段")
    batch = "（" + "；".join(details) + "）" if details else ""
    return (f"已取得{file}{range_label(args, node_titles)}原文，本批 {characters} 字符{batch}{continuation}，"
            "正在归纳本批原文，提取目录需要覆盖的要求。")


def error_message(tool, args, files, error):
    if tool == "search.web":
        query = _web_text(args.get("query"), 120)
        target = f"“{query}”" if query else "本次查询"
        return f"对{target}的联网搜索未成功，已保留记录，正在调整后续研究安排。"
    if tool is None:
        return "AI 返回的操作格式不完整，正在重新请求下一步资料阅读安排。"
    if tool == "finish":
        return "资料研究结论格式不完整，正在重新整理目录要求和原文引用。"
    error_text = str(error).lower()
    if tool == "research.save" or any(marker in error_text for marker in (
        "研究笔记", "笔记引用", "research note", "note citation",
    )):
        return "研究笔记尚未通过原文引用核对，正在修正归纳内容。"
    if "该范围已发送给模型" in error_text or "range was already delivered" in error_text:
        return "这部分原文已经提供过，正在整理已有笔记；需要复核时会说明具体疑问。"
    if "预算" in error_text or "budget" in error_text:
        return "本轮原文读取额度已用完，正在整理已读内容和仍需补查的问题。"
    if tool == "fs.list":
        return "资料清单暂未读取成功，正在调整读取方式。"
    return f"本次未能完成对{source_label(args, files)}的读取，正在调整阅读方式。"


def gap_message(report, round_number, max_rounds):
    # Validation details contain paths and internal IDs. Describe categories from
    # program checks rather than echoing model text or serialized diagnostics.
    issues = "\n".join(report.get("validation_issues", [])).lower()
    reasons = []
    if any(marker in issues for marker in (
        "内部结构", "manifest", "文件尚未选用", "internal structure", "file has not been selected",
    )):
        reasons.append("资料筛选或内部结构尚未检查完整")
    if any(marker in issues for marker in (
        "关键原文", "关键范围", "未读内容", "读取完整性", "key source text", "key range",
        "unread content", "unread-content", "reading completeness",
    )):
        reasons.append("关键原文仍需补查")
    if any(marker in issues for marker in (
        "引用", "要求", "requirement_id", "citation", "cites", "requirement",
    )):
        reasons.append("目录要求与原文依据尚需核对")
    detail = "、".join(reasons) or "还有影响目录范围的资料缺口"
    next_step = "正在继续补查" if round_number < max_rounds else "将保留缺口，目录依据尚未确认完整"
    return f"第 {round_number} 轮资料研究检查发现：{detail}；{next_step}。"
