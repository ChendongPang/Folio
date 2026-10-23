"""Safe, persistent task failure diagnostics in the real browser UI."""

from copy import deepcopy

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)

FAILURE = {
    "diagnostic_id": "diag-test-123",
    "stage": "generation",
    "stage_label": "正文生成",
    "action": "extract",
    "action_label": "抽取正文事实",
    "task_title": "数据需求",
    "section_labels": ["6.2 数据资产"],
    "attempts": 2,
    "code": "stalled_output",
    "reason": "模型持续输出空白，未返回完整结果；本次结果未采用。",
    "saved_sections": 21,
    "total_sections": 37,
    "progress_note": "已保存 21/37 节正文，已有资料与目录仍保留。",
    "details": {"received_chars": 4100, "trailing_whitespace_chars": 4000, "elapsed_seconds": 25.3},
}


async def emit(page, event, data):
    await page.evaluate("([event, data]) => streams.at(-1).emit(event, data)", [event, data])


async def test_failure_card_keeps_actual_cause_and_safe_retry_history(task_page):
    page, _, _, errors = task_page
    failure = deepcopy(FAILURE)
    failure["section_labels"] = ['6.2 <img src=x onerror="window.injected=true">']
    failure["raw_request"] = "private-input-must-not-render"
    failure["details"]["api_key"] = "private-key-must-not-render"
    failure["details"].update(protocol_issue="tool_id_missing", finish_reason="tool_calls")
    retry = {
        "event_id": 90,
        "text": "抽取正文事实未成功，正在进行第 2 次尝试。",
        "failure": failure | {"attempts": 1},
        "is_terminal": False,
        "retry_status": "retrying",
    }
    await emit(page, "work.update", retry)
    await emit(page, "work.update", retry)
    await emit(page, "work.update", {"event_id": 91, "text": "其他章节已完成。"})
    await expect(page.locator(".failure-activity")).to_have_count(1)
    await expect(page.locator(".failure-card")).to_have_count(0)
    await page.locator(".failure-activity summary").first.click()
    assert "已尝试 1 次" in await page.locator(".failure-activity").inner_text()

    await emit(page, "job.failed", {"job_id": "job-a", "text": "本次生成未完成。", "failure": failure})
    card = page.locator(".failure-card")
    await expect(card).to_have_count(1)
    await expect(card).to_contain_text("正文生成 · 抽取正文事实")
    await expect(card).to_contain_text(FAILURE["reason"])
    await expect(card).to_contain_text("已尝试 2 次")
    await expect(card).to_contain_text("已保存 21/37 节正文")
    assert not await card.locator(".failure-technical").evaluate("node => node.open")
    await card.locator(".failure-technical summary").click()
    await expect(card).to_contain_text("diag-test-123")
    await expect(card).to_contain_text("4100")
    await expect(card).to_contain_text("工具调用缺少编号")
    await expect(card).to_contain_text("tool_calls")
    assert await page.locator(".failure-card img, .failure-activity img").count() == 0
    assert await page.evaluate("window.injected === undefined")
    assert "private-input-must-not-render" not in await page.locator("#messages").text_content()
    assert "private-key-must-not-render" not in await page.locator("#messages").text_content()

    await emit(page, "work.update", {"event_id": 92, "text": "迟到的进度不应替换失败结论"})
    await page.evaluate("streams.at(-1).control.error(new Error('迟到的连接错误'))")
    await expect(page.locator(".activity-label")).to_have_text("任务未完成")
    await expect(card).to_contain_text(FAILURE["reason"])
    assert "迟到的" not in await page.locator("#messages").inner_text()
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#stop-button")).to_be_hidden()
    assert errors == []


async def test_failed_history_restores_diagnosis_even_before_stream_or_on_disconnect(task_page):
    page, _, states, errors = task_page
    states["a"].update(active_job_status="failed", active_job_failure=FAILURE)
    await page.reload()
    await page.wait_for_function("streams.length === 1")
    await expect(page.locator("#new-chat")).to_be_enabled()
    card = page.locator(".failure-card")
    await expect(card).to_contain_text(FAILURE["reason"])
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#stop-button")).to_be_hidden()
    await emit(page, "work.update", {"event_id": 3, "text": "历史步骤已完成。"})
    await expect(page.locator(".activity-label")).to_have_text("任务未完成")
    await page.evaluate("streams.at(-1).control.error(new Error('无法重播历史进度'))")
    await expect(card).to_contain_text("6.2 数据资产")
    await expect(card).to_contain_text(FAILURE["reason"])
    assert "无法重播历史进度" not in await page.locator("#messages").inner_text()

    await page.reload()
    await page.wait_for_function("streams.length === 1")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await emit(page, "job.failed", {"job_id": "job-a", "text": "本次生成未完成。", "failure": FAILURE})
    await expect(page.locator(".failure-card")).to_have_count(1)
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await expect(page.locator(".failure-card")).to_have_count(0)
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#messages")).to_contain_text("任务 A")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await expect(page.locator(".failure-card")).to_contain_text(FAILURE["reason"])
    assert errors == []


async def test_failed_attempt_can_recover_without_becoming_terminal_failure(task_page):
    page, _, _, errors = task_page
    await emit(page, "work.update", {
        "event_id": 20,
        "text": "抽取未成功，交由当前阶段继续处理。",
        "failure": FAILURE | {"attempts": 1},
        "is_terminal": False,
        "retry_status": "unknown",
    })
    await emit(page, "artifact.ready", {"filename": "文档.docx", "download_url": "/download", "status": "published", "layout_note": "实际页面版式审校未完整完成，已保留原版式。"})
    await expect(page.locator("#messages .download-link")).to_be_visible()
    await expect(page.locator("#messages")).to_contain_text("实际页面版式审校未完整完成")
    await expect(page.locator(".failure-card")).to_have_count(0)
    await expect(page.locator(".failure-activity")).to_have_count(1)
    await expect(page.locator(".activity-label")).to_have_text("处理结束 · 展开查看过程")
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []


REQUEST_DIAGNOSTICS = {
    "conversation_id": "b",
    "stage": "directory_planning",
    "code": "schema_unsupported",
    "elapsed_ms": 2120,
    "status_code": 400,
    "backend_error": "invalid_json_schema",
    "schema_constraint": "required_all_properties",
    "provider_message": "private-provider-message-must-not-render",
    "raw_request": "private-request-must-not-render",
}
REQUEST_FAILURE = {
    "code": "schema_unsupported",
    "text": "本次修改未成功，已有目录仍保留。模型接口拒绝了本次结构化输出 Schema：对象字段未全部声明为必填。",
    "diagnostics": REQUEST_DIAGNOSTICS,
}
DIRECTORY_REVIEW = {
    "phase": "directory_review",
    "revision": "d" * 32,
    "reason": "完整目录已生成，请确认或提出修改建议。",
    "directory": {
        "sections": [
            {"number": "1", "title": "外汇交易", "level": 1},
            {"number": "1.1", "title": "交易撤销", "level": 2},
        ]
    },
}
SOURCE = {"id": "e" * 32, "name": "业务资料.md", "size": 42, "role": "source"}
REVISION_ADVICE = "补充交易撤销的审批要求"


@pytest.mark.parametrize("known", [True, False])
async def test_protocol_request_diagnostics_show_only_known_causes(task_page, known):
    page, _, states, errors = task_page
    diagnostics = {
        "stage": "directory_planning",
        "code": "protocol",
        "protocol_issue": "tool_arguments_json" if known else "private-provider-error",
        "finish_reason": "missing" if known else "private-finish-reason",
        "research_request_id": "research_004" if known else "research_004-private",
        "raw_tool_calls": "private-tool-arguments",
    }
    states["b"]["messages"].append({
        "role": "assistant",
        "content": "模型服务没有返回完整结果。",
        "error_diagnostics": diagnostics,
    })
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    details = page.locator(".request-error-details")
    await details.locator("summary").click()
    await expect(details).to_contain_text("protocol")
    if known:
        await expect(details).to_contain_text("工具参数不是合法的 JSON")
        await expect(details).to_contain_text("未收到")
        await expect(details).to_contain_text("research_004")
    else:
        assert "具体原因" not in await details.inner_text()
        assert "结束标记" not in await details.inner_text()
        assert "请求编号" not in await details.inner_text()
    assert "private-" not in await page.locator("#messages").text_content()
    assert errors == []


async def test_directory_request_failure_keeps_diagnostics_and_revision_retry(task_page):
    page, _, states, errors = task_page
    states["b"].update(files=[SOURCE], directory_review=deepcopy(DIRECTORY_REVIEW))
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.locator(".directory-advice").fill(REVISION_ADVICE)
    await page.get_by_role("button", name="按建议修改目录", exact=True).click()
    await page.wait_for_function("streams.length === 2")
    payload = await page.evaluate("JSON.parse(streams.at(-1).options.body)")
    assert payload["action"] == "revise_directory"
    assert payload["advice"] == REVISION_ADVICE
    assert payload["selection_revision"] == DIRECTORY_REVIEW["revision"]
    await emit(page, "message.failed", REQUEST_FAILURE)
    await emit(page, "directory.review", DIRECTORY_REVIEW | {
        "failed_advice": REVISION_ADVICE,
        "operation_error": REQUEST_FAILURE,
    })
    await emit(page, "directory.update", {"text": "迟到的进度不应覆盖目录失败"})
    await page.evaluate("streams.at(-1).control.error(new Error('迟到的连接错误'))")
    details = page.locator(".request-error-details")
    await expect(details).to_have_count(1)
    assert not await details.evaluate("node => node.open")
    await details.locator("summary").click()
    await expect(details).to_contain_text("目录规划")
    await expect(details).to_contain_text("400")
    await expect(details).to_contain_text("2.12 秒")
    await expect(details).to_contain_text("schema_unsupported")
    review = page.locator(".directory-review").last
    await expect(review.locator(".directory-review-tree")).to_contain_text("交易撤销")
    await expect(review.locator(".directory-advice")).to_have_value(REVISION_ADVICE)
    await expect(review.get_by_role("button", name="按建议修改目录", exact=True)).to_be_enabled()
    await expect(review.get_by_role("button", name="确认目录，生成文档", exact=True)).to_be_enabled()
    content = await page.locator("#messages").inner_text()
    assert content.count(REQUEST_FAILURE["text"]) == 1
    assert "迟到的" not in content
    assert "private-" not in await page.locator("#messages").text_content()
    await expect(page.locator(".failure-card")).to_have_count(0)
    await review.get_by_role("button", name="按建议修改目录", exact=True).click()
    await page.wait_for_function("streams.length === 3")
    retried = await page.evaluate("JSON.parse(streams.at(-1).options.body)")
    assert retried == payload
    assert errors == []


async def test_request_history_restores_diagnostics_without_duplicate_error_or_lost_retry(task_page):
    page, _, states, errors = task_page
    states["b"].update(
        messages=[
            {"role": "user", "content": "普通会话 B"},
            {"role": "assistant", "content": "之前的普通回复失败。", "error_diagnostics": {"stage": "chat", "code": "timeout", "elapsed_ms": 60000}},
            {"role": "user", "content": REVISION_ADVICE},
            {"role": "assistant", "content": REQUEST_FAILURE["text"], "error_diagnostics": REQUEST_DIAGNOSTICS},
        ],
        files=[SOURCE],
        directory_review=DIRECTORY_REVIEW | {
            "failed_advice": REVISION_ADVICE,
            "operation_error": REQUEST_FAILURE,
        },
    )
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await expect(page.locator(".request-error-details")).to_have_count(2)
    await expect(page.locator(".assistant-message")).to_have_count(2)
    details = page.locator(".request-error-details").last
    await details.locator("summary").click()
    await expect(details).to_contain_text("目录规划")
    await expect(details).to_contain_text("400")
    await expect(page.locator(".directory-advice")).to_have_value(REVISION_ADVICE)
    await expect(page.get_by_role("button", name="按建议修改目录", exact=True)).to_be_enabled()
    await expect(page.get_by_role("button", name="重新生成目录", exact=True)).to_be_enabled()
    await page.locator(".request-error-details").first.locator("summary").click()
    await expect(page.locator(".request-error-details").first).to_contain_text("对话回复")
    await expect(page.locator(".request-error-details").first).to_contain_text("60.00 秒")
    content = await page.locator("#messages").text_content()
    assert content.count(REQUEST_FAILURE["text"]) == 1
    assert "private-" not in content
    await page.reload()
    await page.wait_for_function("streams.length === 1")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator(".request-error-details")).to_have_count(2)
    await expect(page.locator(".assistant-message")).to_have_count(2)
    await expect(page.locator(".directory-advice")).to_have_value(REVISION_ADVICE)
    content = await page.locator("#messages").text_content()
    assert content.count(REQUEST_FAILURE["text"]) == 1
    assert "private-" not in content
    await page.get_by_role("button", name="按建议修改目录", exact=True).click()
    await page.wait_for_function("streams.length === 2")
    payload = await page.evaluate("JSON.parse(streams.at(-1).options.body)")
    assert payload["action"] == "revise_directory"
    assert payload["advice"] == REVISION_ADVICE
    assert payload["selection_revision"] == DIRECTORY_REVIEW["revision"]
    assert errors == []
