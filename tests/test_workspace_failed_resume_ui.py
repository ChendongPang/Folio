"""Failed document tasks resume the same job from the browser, without planning."""

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)

FAILURE = {"reason": "模型接口暂时不可用。", "saved_sections": 2, "total_sections": 6}


async def emit(page, event, data):
    await page.evaluate("([event, data]) => streams.at(-1).emit(event, data)", [event, data])


async def test_failed_job_retry_reuses_job_and_ignores_late_old_stream(task_page):
    page, calls, _, errors = task_page
    await emit(page, "job.failed", {"job_id": "job-a", "failure": FAILURE})
    retry = page.get_by_role("button", name="重试并继续", exact=True)
    await expect(retry).to_be_enabled()
    await expect(page.locator("#message-input")).to_be_enabled()
    await retry.click()
    await page.wait_for_function("streams.length === 2")
    assert ("POST", "/jobs/job-a/resume", '{"conversation_id":"a"}') in calls
    assert await page.evaluate("streams[1].url") == "/jobs/job-a/stream?replay_history=false"
    await expect(retry).to_have_count(0)
    await expect(page.locator("#messages")).to_contain_text("从保存的进度继续生成正文")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await page.evaluate("""() => {
      streams[0].emit('job.failed', {job_id: 'job-a', text: '迟到的历史失败'});
      streams[0].control.error(new DOMException('detached', 'AbortError'));
    }""")
    await emit(page, "work.update", {"event_id": 20, "text": "正在继续处理未完成章节。"})
    await expect(page.locator("#messages")).to_contain_text("正在继续处理未完成章节")
    assert "迟到的历史失败" not in await page.locator("#messages").inner_text()
    await expect(page.locator("#message-input")).to_be_disabled()
    # A second failure keeps one retry entry, rather than reactivating every old card.
    await emit(page, "job.failed", {"job_id": "job-a", "failure": FAILURE})
    await expect(retry).to_have_count(1)
    assert errors == []


async def test_restored_failed_job_keeps_retry_after_resume_request_failure(task_page):
    page, _, states, errors = task_page
    states["a"].update(active_job_status="failed", active_job_failure=FAILURE)
    await page.reload()
    await page.wait_for_function("streams.length === 1")
    retry = page.get_by_role("button", name="重试并继续", exact=True)
    await expect(retry).to_be_enabled()
    await page.route("**/jobs/job-a/resume", lambda route: route.fulfill(status=503, json={"detail": "任务服务暂时不可用"}))
    await retry.click()
    await expect(page.locator("#toast")).to_contain_text("已保存的进度仍保留")
    await expect(retry).to_be_enabled()
    await expect(page.locator(".failure-card")).to_contain_text(FAILURE["reason"])
    assert await page.evaluate("streams.length") == 1
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#stop-button")).to_be_hidden()
    assert errors == []


async def test_chat_resume_event_clears_retry_and_shows_continuation(task_page):
    page, _, _, errors = task_page
    await emit(page, "job.failed", {"job_id": "job-a", "failure": FAILURE})
    await page.locator("#message-input").fill("接着上次的任务继续")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 2")
    await emit(page, "job.created", {"job_id": "job-a", "resumed": True})
    await expect(page.get_by_role("button", name="重试并继续", exact=True)).to_have_count(0)
    await expect(page.locator("#messages")).to_contain_text("沿用已确认目录，从保存的进度继续生成正文")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await emit(page, "artifact.ready", {"job_id": "job-a", "filename": "文档.docx", "download_url": "/jobs/job-a/artifact", "status": "published"})
    await expect(page.locator("#messages .download-link")).to_be_visible()
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []
