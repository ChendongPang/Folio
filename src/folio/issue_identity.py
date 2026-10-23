"""Frozen aliases for deterministic findings persisted before English messages.

These strings are identity data, not display translations. Do not load them from
the editable locale catalogs or translate user/model descriptions generally.
"""

import re

_EXACT = {
    ("coverage", "Content sections cannot be empty"): "内容章节不能为空",
    ("coverage", "The required table and column headings are missing from this section"):
        "缺少本节要求的表格及列名",
    ("coverage", "Body subheadings cannot skip levels"): "正文子标题不能跳级",
    ("coverage", "Body subheadings cannot exceed the current section hierarchy"):
        "正文子标题不能越过当前章节层级",
    ("unsupported", "The body cites evidence that was not retrieved"): "正文引用了未检索到的证据",
    ("extraction", "A fact points to a nonexistent body block"): "事实指向不存在的正文块",
    ("unsupported", "A fact lacks valid evidence citations"): "事实缺少有效证据引用",
}

_DERIVED_EXACT = {
    "Derived evidence lacks a trusted research record": "派生证据缺少可信研究记录",
    "Derived-evidence content does not match the research record": "派生证据内容与研究记录不一致",
    "The derived result has not passed calculation-method and source verification": "派生结果尚未通过计算口径和来源核验",
    "The derived-evidence research record cannot be verified": "派生证据研究记录无法核验",
    "Invalid derived-evidence record location": "派生证据记录位置无效",
    "The derived-evidence record is missing or exceeds the size limit": "派生证据记录缺失或超过大小限制",
    "Invalid derived-evidence record signature": "派生证据记录签名无效",
    "Invalid derived-evidence record version or identity": "派生证据记录版本或身份无效",
    "The derived-evidence record does not belong to the current task": "派生证据记录不属于当前任务",
    "The derived-evidence record identity does not match": "派生证据记录身份不匹配",
    "Invalid derived-evidence record format": "派生证据记录格式无效",
    "The research execution record changed": "研究执行记录已变化",
}
_DERIVED_PREFIXES = {
    "The calculation input source version changed: ": "计算输入资料版本已变化：",
    "The research script or result file changed: ": "研究脚本或结果文件已变化：",
    "A preceding calculation step, result, or execution record changed: ": "前序计算步骤、结果或执行记录已变化：",
    "The source version used by a preceding calculation changed: ": "前序计算使用的资料版本已变化：",
}


def _canonical_derived_error(error):
    if error in _DERIVED_EXACT:
        return _DERIVED_EXACT[error]
    for english, original in _DERIVED_PREFIXES.items():
        if error.startswith(english):
            return original + error[len(english):]
    # Unknown/external errors remain exact, even when this requires a fresh
    # review; do not infer equivalence of unrecognized failure descriptions.
    return error


def canonical_issue_description(description, *, kind, key, basis, rule):
    """Keep legacy fingerprints for known rules, preserving all dynamic text."""
    if basis != "content" or rule != "other":
        return description
    if original := _EXACT.get((kind, description)):
        return original
    if kind == "coverage":
        prefix = "Project source not covered: "
        if description.startswith(prefix):
            return "未覆盖项目材料：" + description[len(prefix):]
        if key.startswith("required-table:"):
            match = re.fullmatch(
                r"Required standalone template table (.+?) is missing\. It must have columns "
                r"(.+) and at least (\d+) data rows", description, re.S,
            )
            if match and key == "required-table:" + match[1]:
                return (
                    f"缺少模板要求的独立表格 {match[1]}，列名须为 {match[2]}，"
                    f"且至少有 {match[3]} 行数据"
                )
    prefixes = (
        ("unsupported", "derived-evidence:", "Derived research evidence is unavailable: ", "派生研究证据不可用："),
        ("pending", "record-only:", "Metadata to fill in: ", "待填写元数据："),
    )
    for expected_kind, key_prefix, english, original in prefixes:
        if kind == expected_kind and key.startswith(key_prefix) and description.startswith(english):
            suffix = description[len(english):]
            if key_prefix == "derived-evidence:":
                suffix = _canonical_derived_error(suffix)
            return original + suffix
    return description
