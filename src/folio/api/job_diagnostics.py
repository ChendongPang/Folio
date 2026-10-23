"""Safe, replayable document-job failures; source bodies remain server-side."""

import json
import math
import re
from datetime import datetime

from folio.runtime.diagnostics import FINISH_REASONS, PROTOCOL_MESSAGES

STAGES = {
    "queued": "等待执行",
    "planning": "目录规划",
    "work_planning": "任务分组",
    "generation": "正文生成与章节核验",
    "review": "文档审阅",
    "illustrating": "生成配图",
    "formatting": "排版优化",
    "rendering": "Word 导出",
}
ACTION_LABELS = {
    "plan": "目录规划",
    "work_plan": "任务分组",
    "select_context": "筛选相关资料",
    "understand_work": "理解业务资料",
    "query": "生成检索问题",
    "web_query": "生成联网检索问题",
    "next_action": "判断下一步动作",
    "generate": "撰写正文",
    "repair": "修正正文",
    "complete_gaps": "补写资料缺口",
    "read_gap_materials": "阅读补写资料",
    "integrate_supplement": "整合补充内容",
    "check_supplement_conflicts": "核对补充内容冲突",
    "repair_supplement_conflicts": "修正补充内容冲突",
    "extract": "提取事实要点",
    "validate": "核验章节事实",
    "review_work": "审阅业务任务",
    "review": "跨章节一致性检查",
    "review_focus": "确定审阅范围",
    "plan_review": "安排审阅分组",
    "read_review_materials": "阅读审阅资料",
    "drill_down": "问题精查",
    "design_ui": "生成界面原型",
    "design_flowchart": "生成流程图",
    "validate_visual": "核验配图",
    "format_document": "优化文档排版",
}
REASONS = {
    "stalled_output": "模型没有返回完整的结构化结果，并持续输出空白；这份不完整结果未被采用。",
    "timeout": "本次模型请求达到等待时限，未得到完整结果。",
    "context_limit": "本次请求超过模型可处理的上下文容量。",
    "length_limit": "模型达到输出长度上限，结果仍不完整。",
    "repetition": "模型反复输出相同内容，程序已停止这次重复输出。",
    "authentication": "模型服务认证未通过，请检查接口密钥及权限。",
    "billing": "模型服务报告额度不足，本次调用未完成。",
    "rate_limit": "模型服务限制了当前请求频率。",
    "schema_unsupported": "模型接口不支持本次结构化输出约束。",
    "configuration": "模型服务配置不可用。",
    "protocol": "模型服务没有返回符合接口协议的完整结果。",
    "table_shape": "模型返回的表格各行列数不一致。",
    "internal_reference": "模型把内部资料编号写入了正文或表格。",
    "visual_reference": "模型返回的配图引用未对应到有效正文或表格。",
    "pending_metadata": "模型标记的待补元数据未对应到有效的空白表格单元格。",
    "edit_target": "模型提出的修改位置不存在或相互重叠。",
    "provenance": "模型修改未正确保留已有内容的来源或补写标记。",
    "invalid_action": "模型选择的下一步动作不符合当前任务状态。",
    "work_plan_coverage": "任务规划遗漏了部分内容节点，尚未开始检索和正文生成。",
    "work_plan_size": "任务分组仍超过单任务节点上限，尚未开始检索和正文生成。",
    "internal_validation": "程序处理内部数据时校验失败，已有内容与进度已保留；请通过定位码检查后台记录。",
    "invalid_output": "模型返回的内容未通过必要的数据结构或内容校验。",
    "model_error": "当前处理步骤未完成，具体技术原因可通过定位码查询后台记录。",
}


def _code(error, explicit=None, *, model_output=False):
    if explicit in REASONS and explicit != "model_error":
        return explicit
    error = error or ""
    match = re.search(r"\[([a-z_]+)\]", error)
    if match and match[1] in REASONS:
        return match[1]
    for marker, code in (
        ("Structured response stalled", "stalled_output"),
        ("Timeout", "timeout"),
        ("ModelOutputRepetition", "repetition"),
        ("Model context limit", "context_limit"),
        ("context exceeds budget", "context_limit"),
        ("exceeds capacity", "context_limit"),
        ("exceeds measured capacity", "context_limit"),
        ("finish_reason='length'", "length_limit"),
        ("Table rows must have the same width", "table_shape"),
        ("内部引用", "internal_reference"),
        ("Figure sources must", "visual_reference"),
        ("Figure insertion point", "visual_reference"),
        ("Supplement cannot remove figure source or insertion blocks", "visual_reference"),
        ("Pending metadata", "pending_metadata"),
        ("Unknown or overlapping supplement edit target", "edit_target"),
        ("must retain model provenance", "provenance"),
        ("Decision selected an unavailable action", "invalid_action"),
        ("cover every content node", "work_plan_coverage"),
        ("max_sections_per_task", "work_plan_size"),
        ("ValidationError", "invalid_output" if model_output else "internal_validation"),
        ("ValueError", "invalid_output" if model_output else "internal_validation"),
    ):
        if marker in error:
            return code
    return "model_error"


def _event(row):
    return dict(row) | {"data": json.loads(row["data"])} if row else None


def _fatal_event(store, job):
    with store.connect() as conn:
        terminal = conn.execute(
            "SELECT id,created_at FROM events WHERE job_id=? AND kind='progress' "
            "AND json_extract(data,'$.status')='failed' ORDER BY id DESC LIMIT 1",
            (job["id"],),
        ).fetchone()
        if not terminal:
            return None
        boundary = (
            conn.execute(
                "SELECT max(id) FROM events WHERE job_id=? AND id<? "
                "AND ((kind IN ('progress','job_control') AND json_extract(data,'$.status')='running') "
                "OR (kind='progress' AND json_extract(data,'$.status')='failed'))",
                (job["id"], terminal["id"]),
            ).fetchone()[0]
            or 0
        )
        explicit = _event(
            conn.execute(
                "SELECT * FROM events WHERE job_id=? AND kind='job_failure' AND id>? AND id<? "
                "ORDER BY id DESC LIMIT 1",
                (job["id"], boundary, terminal["id"]),
            ).fetchone()
        )
        if explicit:
            return explicit
        candidates = conn.execute(
            "SELECT * FROM events WHERE job_id=? AND kind='task_failed' AND id>? AND id<? "
            "ORDER BY id DESC LIMIT 30",
            (job["id"], boundary, terminal["id"]),
        ).fetchall()

        # Legacy jobs have no explicit exception attribution. Require the same
        # exception at termination, not merely the last error or cancelled call.
        def normalize(text):
            return re.sub(r" (?=\[[a-z_]+\]:)\[[a-z_]+\]", "", text or "")

        matches = []
        for row in candidates:
            elapsed = (
                datetime.fromisoformat(terminal["created_at"])
                - datetime.fromisoformat(row["created_at"])
            ).total_seconds()
            event = _event(row)
            if 0 <= elapsed <= 5 and normalize(event["data"].get("error")) == normalize(
                job.get("error")
            ):
                resolved = conn.execute(
                    "SELECT 1 FROM events WHERE job_id=? AND kind='task_completed' AND id>? "
                    "AND id<? AND json_extract(data,'$.key')=? LIMIT 1",
                    (job["id"], row["id"], terminal["id"], event["data"].get("key")),
                ).fetchone()
                if not resolved:
                    matches.append(event)
        return matches[0] if len(matches) == 1 else None


def _request_context(store, job_id, task_key, before):
    with store.connect() as conn:
        row = conn.execute(
            "SELECT data FROM events WHERE job_id=? AND kind='request_prepared' AND id<=? "
            "AND json_extract(data,'$.task_key')=? ORDER BY id DESC LIMIT 1",
            (job_id, before, task_key),
        ).fetchone()
    if not row:
        return {}
    reference = json.loads(row[0]).get("context_ref", "")
    if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{24}", reference):
        return {}
    folder = store.root / "jobs" / job_id / "contexts" / "requests"
    path = folder / (reference + ".json")
    try:
        if not path.resolve().is_relative_to(folder.resolve()) or path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text(encoding="utf-8")).get("context", {})
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def failure_details(store, job, event=None, *, terminal=False):
    """Expose only fixed reasons, existing chapter titles and numeric diagnostics."""
    event = event or {}
    data = event.get("data", {})
    key = data.get("task_key") or data.get("key") or ""
    action = data.get("task") or key.split(":", 1)[0]
    action = action if action in ACTION_LABELS else ""
    event_id = event.get("id", 0)
    context = _request_context(store, job["id"], key, event_id) if key else {}
    section = context.get("section") or {}
    ids = {section.get("section_id")} if isinstance(section, dict) else set()
    for name in ("sections", "affected_sections"):
        ids.update(
            item.get("section_id") for item in context.get(name, []) if isinstance(item, dict)
        )
    for name in ("finding", "work_task"):
        item = context.get(name)
        if isinstance(item, dict):
            # A specific section is more useful than its task's entire chapter list.
            if name != "work_task" or not any(ids):
                ids.update(item.get("section_ids", []))
    plan = (job.get("plan") or {}).get("sections", [])
    sections = [item for item in plan if item["section_id"] in ids]
    work_tasks = (job.get("work_plan") or {}).get("tasks", [])
    task = next(
        (
            item
            for item in work_tasks
            if any(s["section_id"] in item.get("section_ids", []) for s in sections)
        ),
        {},
    )
    attempts = data.get("attempt", 0)
    usage = data.get("usage") or {}
    if key:
        with store.connect() as conn:
            start = conn.execute(
                "SELECT data FROM events WHERE job_id=? AND kind='task_started' AND id<=? "
                "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                (job["id"], event_id, key),
            ).fetchone()
            failure = conn.execute(
                "SELECT data FROM events WHERE job_id=? AND kind='task_failed' AND id<=? "
                "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                (job["id"], event_id, key),
            ).fetchone()
        if start:
            attempts = json.loads(start[0]).get("attempt", attempts)
        if failure and not usage:
            usage = json.loads(failure[0]).get("usage") or {}
    details = {}
    metrics = usage.get("diagnostics") or data.get("diagnostics") or {}
    for name in (
        "received_chars",
        "trailing_whitespace_chars",
        "elapsed_seconds",
        "reasoning_chars",
        "first_content_seconds",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = metrics.get(name)
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 10**12:
            details[name] = value
    for name, allowed in (("protocol_issue", PROTOCOL_MESSAGES), ("finish_reason", FINISH_REASONS)):
        value = metrics.get(name)
        if isinstance(value, str) and value in allowed:
            details[name] = value
    body_ids = {item["section_id"] for item in plan if item.get("content_mode") != "container"}
    saved = sum(state.section_id in body_ids for state in store.states(job["id"]))
    total = len(body_ids)
    code = _code(
        data.get("error") or job.get("error") if terminal else data.get("error"), data.get("code"),
        model_output=bool(key) and event.get("kind") in {"task_failed", "job_failure"},
    )
    stage = job.get("stage")
    if event_id:
        with store.connect() as conn:
            progress = conn.execute(
                "SELECT data FROM events WHERE job_id=? AND kind='progress' AND id<=? "
                "AND json_extract(data,'$.stage') IS NOT NULL ORDER BY id DESC LIMIT 1",
                (job["id"], event_id),
            ).fetchone()
        if progress:
            stage = json.loads(progress[0])["stage"]
    return {
        "diagnostic_id": f"{job['id'][:8]}-{event_id}",
        "stage": stage if stage in STAGES else "",
        "stage_label": STAGES.get(stage, "文档处理"),
        "action": action,
        "action_label": ACTION_LABELS.get(action, "当前步骤"),
        "task_title": str(task.get("title", ""))[:200],
        "section_labels": [
            (str(item.get("display_number", "")) + " " + item["title"]).strip() for item in sections
        ],
        "attempts": attempts if type(attempts) is int and 0 <= attempts <= 1000 else 0,
        "code": code,
        "reason": REASONS[code],
        "saved_sections": saved,
        "total_sections": total,
        "progress_note": (
            "已确认的目录已保留。"
            if stage == "work_planning"
            else f"已保存 {saved}/{total} 个正文章节，已有内容和检索进度保留。"
        ),
        "details": details,
    }


def job_failure(store, job):
    return failure_details(store, job, _fatal_event(store, job), terminal=True)


def failure_text(failure):
    location = "、".join(failure["section_labels"][:3])
    action = failure["action_label"]
    attempts = f"已尝试 {failure['attempts']} 次。" if failure["attempts"] else ""
    return (
        (f"《{location}》的" if location else "")
        + action
        + "未完成："
        + failure["reason"]
        + attempts
        + failure["progress_note"]
    )


def retry_details(store, job, event):
    failure = failure_details(store, job, event)
    key = event["data"].get("key")
    with store.connect() as conn:
        next_start = conn.execute(
            "SELECT 1 FROM events WHERE job_id=? AND kind='task_started' AND id>? "
            "AND json_extract(data,'$.key')=? LIMIT 1",
            (job["id"], event["id"], key),
        ).fetchone()
        grant = conn.execute(
            "SELECT data FROM events WHERE job_id=? AND kind='task_retry_authorized' "
            "AND id<? AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
            (job["id"], event["id"], key),
        ).fetchone()
    limits = job.get("input", {}).get("limits", {})
    action = failure["action"]
    attempt_limit = (
        1
        if action
        in {"format_document", "check_supplement_conflicts", "repair_supplement_conflicts"}
        else limits.get("work_plan_attempts", 3)
        if action == "work_plan"
        else min(limits.get("call_attempts", 2), 2)
        if action == "integrate_supplement"
        else limits.get("call_attempts", 2)
    )
    if grant:
        attempt_limit += json.loads(grant["data"])["attempts"]
    no_retry = failure["attempts"] >= attempt_limit or failure["code"] in {
        "authentication",
        "billing",
        "configuration",
        "schema_unsupported",
        "context_limit",
    }
    failure["is_terminal"] = False
    failure["retry_status"] = "retrying" if next_start else "stopped" if no_retry else "unknown"
    suffix = (
        "随后已发起重试。"
        if next_start
        else "本次请求不再自动重试，后续由当前阶段处理。"
        if no_retry
        else "本次调用未完成，后续由当前阶段处理。"
    )
    location = "、".join(failure["section_labels"][:3])
    prefix = f"《{location}》" if location else ""
    text = f"{prefix}{failure['action_label']}第 {failure['attempts']} 次未通过：{failure['reason']}{suffix}"
    return text, failure


def retry_started_details(store, job, event):
    with store.connect() as conn:
        previous = _event(
            conn.execute(
                "SELECT * FROM events WHERE job_id=? AND kind='task_failed' AND id<? "
                "AND json_extract(data,'$.key')=? ORDER BY id DESC LIMIT 1",
                (job["id"], event["id"], event["data"].get("key")),
            ).fetchone()
        )
    if not previous:
        return None, None
    failure = failure_details(store, job, previous)
    failure.update(is_terminal=False, retry_status="retrying", attempts=event["data"]["attempt"])
    location = "、".join(failure["section_labels"][:3])
    prefix = f"《{location}》" if location else ""
    return (
        f"{prefix}正在进行第 {failure['attempts']} 次{failure['action_label']}，修正上次未完成的结果。",
        failure,
    )
