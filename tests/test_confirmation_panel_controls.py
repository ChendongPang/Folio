"""Optional follow-up questions stay available without obstructing the workspace."""

from playwright.async_api import expect
from test_workspace_confirmation import CONFIRMATION, show_pending

pytest_plugins = ("test_workspace_task_navigation",)


async def test_collapsing_confirmation_preserves_answers_and_download(task_page):
    page, _, states, errors = task_page
    await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=CONFIRMATION))
    await show_pending(page, states)
    await page.locator("#confirmation-answer-0").fill("审批上限为100万元")
    await page.locator("#confirmation-calls").fill("12")

    await expect(page.locator("#confirmation-toggle")).to_have_attribute("aria-expanded", "true")
    await page.locator("#confirmation-toggle").click()
    await expect(page.locator("#confirmation-toggle")).to_have_attribute("aria-expanded", "false")
    await expect(page.locator("#confirmation-content")).to_be_hidden()
    await expect(page.locator(".download-link")).to_have_attribute("href", "/jobs/job-a/artifact")
    await expect(page.locator("#message-input")).to_be_enabled()

    await page.locator("#confirmation-toggle").click()
    await expect(page.locator("#confirmation-content")).to_be_visible()
    await expect(page.locator("#confirmation-answer-0")).to_have_value("审批上限为100万元")
    await expect(page.locator("#confirmation-calls")).to_have_value("12")
    assert errors == []


async def test_skipping_is_local_preserves_draft_and_allows_chat(task_page):
    page, _, states, errors = task_page
    posts = []
    page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
    await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=CONFIRMATION))
    await show_pending(page, states)
    await page.locator("#confirmation-answer-1").fill("财务主管审批")
    await page.locator("#confirmation-calls").fill("15")
    stream_count = await page.evaluate("streams.length")

    await page.locator("#confirmation-skip").click()
    await expect(page.locator("#confirmation-panel")).to_contain_text("已跳过本次补充")
    await expect(page.locator("#confirmation-content")).to_be_hidden()
    await expect(page.locator("#confirmation-reopen")).to_have_text("重新补充")
    await expect(page.locator(".download-link")).to_have_attribute("href", "/jobs/job-a/artifact")
    await expect(page.locator("#message-input")).to_be_enabled()
    assert posts == []
    assert await page.evaluate("streams.length") == stream_count
    assert states["a"]["active_job_status"] == "needs_confirmation"

    await page.locator("#confirmation-reopen").click()
    await expect(page.locator("#confirmation-toggle")).to_have_attribute("aria-expanded", "true")
    await expect(page.locator("#confirmation-answer-1")).to_have_value("财务主管审批")
    await expect(page.locator("#confirmation-calls")).to_have_value("15")
    assert posts == []
    assert await page.evaluate("streams.length") == stream_count

    await page.locator("#confirmation-skip").click()
    await page.locator("#message-input").fill("先介绍一下这份草稿")
    await page.locator("#send-button").click()
    await page.wait_for_function("count => streams.length === count + 1", arg=stream_count)
    await page.evaluate("""() => {
      streams.at(-1).emit('message.completed', {text: '草稿已保留，随时可以继续补充。'});
      streams.at(-1).control.close();
    }""")
    await expect(page.locator("#messages")).to_contain_text("草稿已保留，随时可以继续补充。")
    await expect(page.locator("#message-input")).to_be_enabled()
    assert not any("/confirmation" in url or "/resume" in url for url in posts)
    assert errors == []


async def test_skip_survives_navigation_and_reload_but_new_revision_reopens(task_page):
    page, _, states, errors = task_page
    view = dict(CONFIRMATION)
    await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=view))
    await show_pending(page, states)
    await page.locator("#confirmation-answer-0").fill("已填写的部分答复")
    await page.locator("#confirmation-skip").click()

    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#confirmation-panel")).to_have_count(0)
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#confirmation-reopen")).to_be_visible()
    await expect(page.locator("#confirmation-content")).to_be_hidden()
    await page.reload()
    await expect(page.locator("#confirmation-reopen")).to_be_visible()
    await expect(page.locator("#confirmation-content")).to_be_hidden()

    view["revision"] = "review-2"
    view["issues"] = [CONFIRMATION["issues"][0] | {"description": "新一轮确认：审批上限是否仍为100万元？"}]
    await page.reload()
    await expect(page.locator("#confirmation-toggle")).to_have_attribute("aria-expanded", "true")
    await expect(page.locator("#confirmation-content")).to_be_visible()
    await expect(page.locator("#confirmation-panel")).to_contain_text("新一轮确认")
    await expect(page.locator("#confirmation-reopen")).to_be_hidden()
    await expect(page.locator("#confirmation-answer-0")).to_have_value("已填写的部分答复")
    assert errors == []


async def test_collapsed_state_restores_and_controls_fit_small_screen(task_page):
    page, _, states, errors = task_page
    await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=CONFIRMATION))
    await show_pending(page, states)
    await page.locator("#confirmation-toggle").click()
    await page.reload()
    await expect(page.locator("#confirmation-toggle")).to_have_attribute("aria-expanded", "false")
    await expect(page.locator("#confirmation-content")).to_be_hidden()

    await page.set_viewport_size({"width": 390, "height": 844})
    await expect(page.locator("#confirmation-toggle")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.locator("#confirmation-toggle").click()
    await expect(page.locator("#confirmation-content")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.locator("#confirmation-skip").click()
    await expect(page.locator("#confirmation-reopen")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []
