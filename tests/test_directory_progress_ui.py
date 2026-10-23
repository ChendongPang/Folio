"""Readable directory progress from live events, without exposing model internals."""

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


async def start_directory(task_page, files=None):
    page, _, states, _ = task_page
    states["a"].update(
        active_job=None,
        active_job_status=None,
        files=files or [{"id": "source-a", "name": "招标文件.docx", "size": 12000}],
        directory_review={
            "phase": "directory_review",
            "revision": "old-directory",
            "title": "技术响应方案",
            "directory": {"sections": [{"number": "1", "title": "技术方案", "level": 1}]},
        },
    )
    await page.reload()
    await page.get_by_role("button", name="重新生成目录", exact=True).click()
    await page.wait_for_function("streams.length === 1")
    return page


async def emit(page, event, data):
    await page.evaluate("([event, data]) => streams.at(-1).emit(event, data)", [event, data])


async def test_english_preview_reason_uses_localized_runtime_text(task_page):
    page = await start_directory(task_page)
    await page.locator("#language-select").select_option("en")
    await emit(page, "directory.review", {
        "phase": "directory_review", "revision": "english-directory", "title": "Tender response",
        "reason": "已根据资料研究生成目录，请查看阅读记录并确认或提出修改建议。确认后才会生成文档正文。",
        "directory": {"sections": [{"number": "1", "title": "Technical response", "level": 1}]},
    })
    await page.locator(".preview-toggle").click()

    reason = page.locator(".preview-reason").last
    await expect(reason).to_have_text(
        "The outline was generated from source research. Review the reading record, "
        "then confirm or revise it before body generation."
    )
    assert task_page[3] == []


async def test_directory_review_displays_elapsed_time_and_token_usage(task_page):
    page = await start_directory(task_page)
    await page.locator("#language-select").select_option("en")
    await emit(page, "directory.review", {
        "phase": "directory_review", "revision": "usage-directory", "title": "Tender response",
        "reason": "Outline complete.",
        "directory": {
            "sections": [{"number": "1", "title": "Technical response", "level": 1}],
            "task_usage": [{
                "task_id": "directory_planning",
                "title": "Outline planning",
                "elapsed_ms": 24500,
                "started_at": "2026-10-23T00:00:00Z",
                "finished_at": "2026-10-23T00:00:24.500Z",
                "input_tokens": 800,
                "output_tokens": 200,
                "total_tokens": 1000,
                "model_calls": 3,
            }],
        },
    })

    usage = page.locator(".task-usage").last
    await expect(usage.locator("summary")).to_contain_text("24s · 1,000 Token")
    await expect(usage).to_contain_text("Outline planning")
    await expect(usage).to_contain_text("Input tokens: 800")
    await expect(usage).to_contain_text("Output tokens: 200")
    assert task_page[3] == []


@pytest.mark.parametrize("code,message", [
    ("tool_error", "研究工具执行失败，已有阅读结果和笔记已保留；需要修复或恢复研究后确认目录。"),
    ("source_insufficient", "研究结论仍有资料或范围缺口，请查看具体缺口后补充资料或明确范围。"),
    ("directory_coverage", "目录遗漏已确认的验收要求，需要补齐对应章节。"),
])
async def test_directory_block_displays_the_actual_reason(task_page, code, message):
    page = await start_directory(task_page)
    await emit(page, "directory.review", {
        "phase": "directory_review", "revision": "incomplete-directory", "title": "技术方案",
        "directory": {
            "sections": [{"number": "1", "title": "技术需求响应", "level": 1}],
            "confirmable": False, "confirmation_issues": [{"code": code, "message": message}],
        },
    })
    await page.evaluate("streams.at(-1).control.close()")
    review = page.locator(".directory-review").last
    await expect(review.locator(".directory-error")).to_have_text(message)
    await expect(review.get_by_role("button", name="确认目录，生成文档", exact=True)).to_be_disabled()
    await expect(review.get_by_role("button", name="重新生成目录", exact=True)).to_be_enabled()
    assert "补充范围或修改要求后重新生成目录" not in await review.inner_text()
    assert task_page[3] == []


async def test_directory_progress_shows_actions_and_waits_without_repeating_rows(task_page):
    page = await start_directory(task_page)
    inspect = {
        "id": "inspect-1", "phase": "research", "status": "running",
        "text": "正在查看《招标文件.docx》的章节结构",
        "detail": "确认技术要求、评标办法分别位于哪些章节。",
    }
    await emit(page, "directory.update", {"text": inspect["text"], "activity": inspect, "elapsed_seconds": 2})
    activity = page.locator(".directory-activity")
    await expect(activity).to_have_attribute("open", "")
    await expect(activity.locator(".activity-item")).to_have_count(1)
    await expect(activity).to_contain_text(inspect["detail"])
    await expect(page.locator(".agent-working")).to_have_count(0)
    read = {
        "id": "read-2", "phase": "research", "status": "running",
        "text": "正在阅读《招标文件.docx》中的第五章 技术需求书",
    }
    await emit(page, "directory.update", {"text": read["text"], "activity": read, "elapsed_seconds": 12})
    for elapsed in (20, 31, 65):
        await emit(page, "directory.update", {
            "text": read["text"], "activity": read,
            "elapsed_seconds": elapsed, "step_elapsed_seconds": elapsed - 12,
            "model_status": "thinking", "reasoning": "private-chain-of-thought",
            "response_chars": 120,
        })
    await expect(activity.locator(".activity-item")).to_have_count(2)
    await expect(activity.locator(".activity-check")).to_have_count(1)
    await expect(activity.locator(".activity-label")).to_have_text("资料研究与目录规划")
    await expect(activity.locator(".activity-wait")).to_contain_text("模型正在分析资料")
    await expect(activity.locator(".activity-wait")).to_contain_text("本步已用时 53 秒")
    await expect(activity.locator(".activity-elapsed")).to_have_text("已用时 1 分 5 秒")
    await expect(page.locator("#review-mode-bar")).to_have_count(0)
    await expect(page.locator("#review-stop")).to_have_count(0)
    await expect(page.locator("#composer #stop-button")).to_be_visible()
    assert (await page.locator("#messages").inner_text()).count(read["text"]) == 1
    assert "private-chain-of-thought" not in await page.locator("#messages").inner_text()
    assert "fs.inspect" not in await page.locator("#messages").inner_text()
    assert task_page[3] == []


async def test_directory_history_uses_main_scroll_and_preserves_reading_position(task_page):
    page = await start_directory(task_page)
    await page.emulate_media(reduced_motion="reduce")
    scroller = page.locator("#conversation-scroll")
    await scroller.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    activity = page.locator(".directory-activity")
    for index in range(24):
        await emit(page, "directory.update", {
            "activity": {
                "id": f"next-{index}", "status": "running",
                "text": f"正在读取技术需求第 {index + 1} 节，核对本节涉及的业务流程、验收标准和资料来源。",
            },
        })
        await expect(activity.locator(".activity-item")).to_have_count(index + 1)
        await page.wait_for_function("(() => { const el = document.querySelector('#conversation-scroll'); return el.scrollHeight - el.scrollTop - el.clientHeight < 2; })()")
    listing = activity.locator(".activity-list")
    assert await listing.evaluate("element => !['auto', 'scroll'].includes(getComputedStyle(element).overflowY)")
    assert await listing.evaluate("element => element.scrollHeight <= element.clientHeight + 1")
    assert await scroller.evaluate("element => element.scrollTop > 300")

    # A reader returning to older steps must not be pulled back to new events.
    await scroller.evaluate("element => { element.scrollTop = 60; }")
    previous_top = await scroller.evaluate("element => element.scrollTop")
    await emit(page, "directory.update", {"activity": {"id": "last", "text": "正在核对目录覆盖", "status": "running"}})
    await expect(activity.locator(".activity-item")).to_have_count(25)
    assert abs(await scroller.evaluate("element => element.scrollTop") - previous_top) <= 1

    # Following resumes when the reader returns to the bottom themselves.
    await scroller.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await emit(page, "directory.update", {"activity": {"id": "resume", "text": "正在编排目录章节", "status": "running"}})
    await expect(activity.locator(".activity-item")).to_have_count(26)
    await page.wait_for_function("(() => { const el = document.querySelector('#conversation-scroll'); return el.scrollHeight - el.scrollTop - el.clientHeight < 2; })()")
    assert task_page[3] == []


async def test_long_directory_progress_keeps_composer_and_stop_in_small_viewport(task_page):
    filename = "SemiAnalysis - The Chinese AI Infrastructure Boom_Introduction_" + "LongDocumentName" * 8 + ".pdf"
    page = await start_directory(task_page, files=[
        {"id": f"source-{index}", "name": f"{index}-{filename}", "size": 12000}
        for index in range(8)
    ])
    await page.emulate_media(reduced_motion="reduce")
    for index in range(24):
        await emit(page, "directory.update", {
            "activity": {
                "id": f"long-{index}", "status": "running",
                "text": f"正在阅读《{filename}》中的第 {index + 1} 节，核对目录要求。",
            },
        })
    await expect(page.locator(".activity-item")).to_have_count(24)
    await page.set_viewport_size({"width": 390, "height": 640})
    await expect(page.locator("#review-mode-bar")).to_have_count(0)
    await expect(page.locator("#message-input")).to_be_visible()
    stop = page.locator("#composer #stop-button")
    await expect(stop).to_be_visible()
    await expect(stop).to_be_enabled()
    await expect(stop).to_have_css("border-radius", "50%")
    control = await stop.bounding_box()
    assert abs(control["width"] - control["height"]) <= 1
    for selector in ("#message-input", "#stop-button"):
        assert await page.locator(selector).evaluate("element => { const rect = element.getBoundingClientRect(); return rect.top >= 0 && rect.bottom <= innerHeight && rect.left >= 0 && rect.right <= innerWidth; }")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert await page.locator("#conversation-scroll").evaluate("element => element.scrollWidth <= element.clientWidth")
    assert task_page[3] == []


async def test_completed_directory_retains_collapsed_process(task_page):
    page = await start_directory(task_page)
    await emit(page, "directory.update", {
        "activity": {"id": "review-3", "text": "正在核对技术需求是否都有对应章节", "status": "running"},
        "elapsed_seconds": 42,
    })
    await emit(page, "directory.review", {
        "phase": "directory_review", "revision": "new-directory", "title": "技术方案",
        "reason": "目录已完成，请确认章节安排。",
        "directory": {"sections": [{"number": "1", "title": "技术需求响应", "level": 1}]},
    })
    await page.evaluate("streams.at(-1).control.close()")
    activity = page.locator(".directory-activity")
    await expect(activity).not_to_have_attribute("open", "")
    await expect(activity.locator(".activity-label")).to_have_text("目录处理结束 · 展开查看过程")
    await expect(activity.locator(".activity-wait")).to_have_count(0)
    await expect(activity.locator(".activity-elapsed")).to_have_text("用时 42 秒")
    await expect(page.locator("#review-mode-bar")).to_have_count(0)
    review = page.locator(".directory-review").last
    await expect(review.get_by_role("button", name="确认目录，生成文档", exact=True)).to_be_enabled()
    await expect(review.get_by_label("调整目录", exact=True)).to_be_enabled()
    await expect(page.locator("#message-input")).to_have_attribute("placeholder", "继续询问资料或文档内容…")
    await expect(page.locator("#stop-button")).to_have_count(0)
    await activity.locator("summary").click()
    await expect(activity.locator(".activity-item")).to_contain_text("核对技术需求")
    await expect(activity.locator(".activity-check")).to_have_count(1)
    assert task_page[3] == []


async def test_failed_directory_keeps_process_visible_and_ignores_late_heartbeat(task_page):
    page = await start_directory(task_page)
    await emit(page, "directory.update", {
        "activity": {"id": "inspect-1", "text": "正在查看《招标文件.docx》的章节结构", "status": "running"},
        "model_status": "waiting", "elapsed_seconds": 30,
    })
    await expect(page.locator(".activity-wait")).to_contain_text("正在等待模型响应")
    await emit(page, "message.failed", {"text": "模型响应超时，资料已保留。"})
    await emit(page, "directory.update", {"activity": {"id": "late", "text": "迟到的进度", "status": "running"}})
    await page.evaluate("streams.at(-1).control.close()")
    activity = page.locator(".directory-activity")
    await expect(activity).to_have_attribute("open", "")
    await expect(activity.locator(".is-failed")).to_have_count(1)
    await expect(activity.locator(".activity-wait")).to_have_count(0)
    await expect(page.locator("#messages")).to_contain_text("模型响应超时，资料已保留")
    await expect(page.locator("#messages")).not_to_contain_text("迟到的进度")
    assert task_page[3] == []


async def test_saved_directory_process_is_restored_without_a_running_indicator(task_page):
    page, _, states, _ = task_page
    states["a"].update(active_job=None, active_job_status=None)
    states["a"]["messages"].append({
        "role": "assistant", "content": "目录已完成。",
        "activities": [
            {"id": "manifest", "text": "已查看 3 份资料的清单", "status": "completed"},
            {"id": "read", "text": "已阅读第五章 技术需求书", "status": "completed"},
        ],
    })
    await page.reload()
    activity = page.locator(".directory-activity")
    await expect(activity).not_to_have_attribute("open", "")
    await expect(activity.locator(".activity-wait")).to_have_count(0)
    await activity.locator("summary").click()
    await expect(activity.locator(".activity-item")).to_have_count(2)
    await expect(activity).to_contain_text("已阅读第五章 技术需求书")
    await expect(page.locator(".agent-working")).to_have_count(0)
    assert task_page[3] == []
