"""Standalone style analysis is a message result, never a directory transition."""

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


REPORT = {
    "revision": "style-revision",
    "status": "partial",
    "document_names": ["长篇示例.pdf", "补充风格.docx"],
    "style_url": "/conversations/b/styles/style-revision/style",
    "summary": "已分析代表性片段，写法正式、客观；部分页面特征待确认。",
    "cache_hit": False,
}


async def test_standalone_style_events_show_progress_and_preserve_confirmed_directory(task_page):
    page, _, states, errors = task_page
    states["b"]["files"] = [{"id": "sample", "name": "长篇示例.pdf", "size": 1000, "role": "style_reference"}]
    states["b"]["directory_review"] = {
        "phase": "directory_review", "revision": "existing-directory",
        "directory": {"sections": [{"number": "1", "title": "现有目录章节", "level": 1}]},
    }
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#message-input").fill("看一下这个风格示例")
    await expect(page.locator("#generate-button")).to_have_count(0)
    await page.locator("#message-input").press("Enter")
    await page.wait_for_function("streams.length === 2")
    assert await page.evaluate("JSON.parse(streams[1].options.body).action") == "chat"
    await expect(page.locator(".agent-working")).to_be_visible()
    await page.evaluate("streams[1].emit('message.started', {})")
    await expect(page.locator(".agent-working-label")).to_have_text("正在思考...")
    await page.evaluate("""() => {
      streams[1].emit('message.update', {activity: {id: 'intent-1', text: '正在判断你的请求是否需要分析风格', status: 'running'}, model_status: 'waiting', elapsed_seconds: 2, step_elapsed_seconds: 2});
      streams[1].emit('message.update', {activity: {id: 'intent-1', text: '正在判断你的请求是否需要分析风格', status: 'running'}, model_status: 'thinking', elapsed_seconds: 5, step_elapsed_seconds: 5});
    }""")
    await expect(page.locator("#messages .activity")).to_have_count(0)
    await expect(page.locator(".agent-working")).to_be_visible()
    await expect(page.locator(".agent-working-label")).to_have_text("正在思考...")
    await page.evaluate("""() => {
      streams[1].emit('style.update', {activity: {id: 'style-1', text: '正在分析代表性片段', status: 'running'}, model_status: 'waiting', elapsed_seconds: 3, step_elapsed_seconds: 1});
      streams[1].emit('style.update', {activity: {id: 'style-1', text: '正在分析代表性片段', detail: '已选择前部、中部和后部内容', status: 'running'}, model_status: 'thinking', elapsed_seconds: 7, step_elapsed_seconds: 5});
    }""")
    activity = page.locator("#messages .activity").filter(has_text="风格分析过程")
    await expect(activity).to_be_visible()
    await expect(activity.locator(".activity-item")).to_have_count(2)
    await expect(activity.locator(".activity-item").first).to_contain_text("正在判断你的请求")
    await expect(activity).to_contain_text("已选择前部、中部和后部内容")
    await expect(page.locator(".agent-working")).to_have_count(0)
    await expect(page.locator(".directory-review-tree")).to_contain_text("现有目录章节")
    await page.evaluate("report => streams[1].emit('style.completed', report)", REPORT)
    await expect(activity).not_to_have_attribute("open", "")
    assert await activity.evaluate("""element => {
      const text = element.parentElement.querySelector('.message-text');
      return Boolean(text && element.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    await expect(page.locator(".style-analysis-card")).to_contain_text("部分特征待确认")
    await expect(page.locator(".style-analysis-card")).to_contain_text("长篇示例.pdf")
    await expect(page.locator(".style-analysis-card a")).to_have_attribute("href", REPORT["style_url"])
    await page.evaluate("""() => {
      streams[1].emit('message.update', {activity: {id: 'reply-1', text: '正在根据风格说明准备回答', status: 'running'}, model_status: 'waiting', elapsed_seconds: 10});
      streams[1].emit('style.update', {activity: {id: 'reply-2', text: '正在载入已保存的风格说明', status: 'running'}, model_status: 'waiting', elapsed_seconds: 11});
    }""")
    await expect(activity.locator(".activity-item")).to_have_count(2)
    await expect(activity.locator(".activity-label")).to_have_text("风格分析过程 · 展开查看")
    await expect(activity).not_to_have_attribute("open", "")
    await expect(activity.locator(".activity-wait")).to_have_count(0)
    await expect(activity).not_to_contain_text("正在根据风格说明准备回答")
    await expect(activity).not_to_contain_text("正在载入已保存的风格说明")
    await page.evaluate("streams[1].emit('message.delta', {text: '这份示例的写法正式、客观。'})")
    await expect(page.locator("#messages .assistant-message").last.locator(".message-text")).to_have_text(
        "这份示例的写法正式、客观。"
    )
    await expect(activity).not_to_have_attribute("open", "")
    await expect(page.locator(".agent-working")).to_have_count(0)
    await page.evaluate("""() => {
      streams[1].emit('message.completed', {text: '这份示例的写法正式、客观。'});
      streams[1].control.close();
    }""")
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator(".style-analysis-card")).to_have_count(1)
    confirm = page.get_by_role("button", name="确认目录，生成文档", exact=True)
    await expect(confirm).to_be_enabled()
    await confirm.click()
    await page.wait_for_function("streams.length === 3")
    payload = await page.evaluate("JSON.parse(streams[2].options.body)")
    assert payload["action"] == "confirm_directory"
    assert payload["selection_revision"] == "existing-directory"
    assert errors == []


async def test_style_card_restores_after_reload_even_when_followup_answer_failed(task_page):
    page, _, states, errors = task_page
    states["a"]["active_job"] = None
    states["a"]["active_job_status"] = None
    states["a"]["messages"] = [
        {"role": "user", "content": "看一下这个风格示例"},
        {
            "role": "assistant", "content": "风格说明已保存，本次回答未完成。",
            "style_analysis": REPORT | {"status": "analyzed", "cache_hit": True},
            "activity_label": "风格分析过程",
            "activities": [{"id": "saved-step", "text": "已分析 8 个代表性片段", "status": "completed"}],
            "error_diagnostics": {"stage": "style_analysis", "code": "timeout"},
        },
    ]
    await page.reload()
    card = page.locator(".style-analysis-card")
    await expect(card).to_be_visible()
    await expect(card).to_contain_text("分析完成 · 已复用分析结果")
    await expect(card.locator("a")).to_have_attribute("href", REPORT["style_url"])
    await expect(page.locator(".activity-label")).to_contain_text("风格分析")
    await page.get_by_text("错误详情", exact=True).click()
    await expect(page.locator(".request-error-details")).to_contain_text("风格分析")
    await expect(page.locator(".directory-review-tree")).to_have_count(0)
    await expect(page.locator(".agent-working")).to_have_count(0)
    assert errors == []


async def test_interrupted_style_history_keeps_style_progress_label(task_page):
    page, _, states, errors = task_page
    states["b"]["messages"].append({
        "role": "assistant", "content": "", "interrupted": True,
        "activity_label": "风格分析过程",
        "activities": [{"id": "partial-step", "text": "已整理示例结构", "status": "completed"}],
    })
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("风格分析已中断，已保存完成的步骤。")
    await expect(page.locator(".activity-label")).to_have_text("风格分析未完成 · 查看过程")
    await expect(page.locator(".style-analysis-card")).to_have_count(0)
    await expect(page.locator(".directory-review-tree")).to_have_count(0)
    assert errors == []
