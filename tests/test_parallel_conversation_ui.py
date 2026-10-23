"""Keep each session's in-flight reply independent while navigating."""

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


async def start_two_requests(task_page):
    page, _, states, _ = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#message-input").fill("研究 B 的投标资料")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 2")
    states["b"]["messages"].append({"role": "user", "content": "研究 B 的投标资料"})
    await page.locator("#new-chat").click()
    await page.locator("#message-input").fill("研究新会话的资料")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 3")
    states["new"]["messages"].append({"role": "user", "content": "研究新会话的资料"})
    return page


async def emit(page, stream, event, data):
    await page.evaluate(
        "([stream, event, data]) => streams[stream].emit(event, data)",
        [stream, event, data],
    )


async def test_parallel_directory_requests_keep_their_progress_on_return(task_page):
    page = await start_two_requests(task_page)
    await emit(page, 1, "directory.update", {
        "activity": {"id": "read-b", "text": "正在读取 B 的招标要求", "status": "running"},
        "elapsed_seconds": 71, "step_elapsed_seconds": 8, "model_status": "thinking",
    })
    await emit(page, 2, "directory.update", {
        "activity": {"id": "read-new", "text": "正在读取新会话的业务说明", "status": "running"},
        "elapsed_seconds": 32, "step_elapsed_seconds": 5, "model_status": "waiting",
    })
    await expect(page.locator("#messages")).to_contain_text("正在读取新会话的业务说明")
    await expect(page.locator("#messages")).not_to_contain_text("正在读取 B 的招标要求")
    assert not await page.evaluate("Boolean(streams[1].aborted || streams[2].aborted)")

    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    response = page.locator("#messages .assistant-message").last
    await expect(response.locator(".activity")).to_be_visible()
    await expect(response).to_contain_text("正在读取 B 的招标要求")
    await expect(response).to_contain_text("已用时 1 分 11 秒")
    await expect(response).to_contain_text("本步已用时 8 秒")
    await expect(response.locator(".agent-working")).to_have_count(0)
    await expect(page.locator("#messages")).not_to_contain_text("正在读取新会话的业务说明")
    await expect(page.locator("#message-input")).to_be_disabled()

    # Both streams keep advancing; returning must not create another request.
    await emit(page, 2, "directory.update", {
        "activity": {"id": "plan-new", "text": "正在编排新会话目录", "status": "running"},
        "elapsed_seconds": 45, "step_elapsed_seconds": 2, "model_status": "responding",
    })
    await emit(page, 1, "directory.update", {
        "activity": {"id": "plan-b", "text": "正在编排 B 的投标目录", "status": "running"},
        "elapsed_seconds": 88, "step_elapsed_seconds": 3, "model_status": "responding",
    })
    await expect(response).to_contain_text("正在编排 B 的投标目录")
    await page.locator("#session-list button").filter(has_text="研究新会话的资料").click()
    response = page.locator("#messages .assistant-message").last
    await expect(response).to_contain_text("正在读取新会话的业务说明")
    await expect(response).to_contain_text("正在编排新会话目录")
    await expect(response.locator(".activity-item")).to_have_count(2)
    assert await page.evaluate("streams.length") == 3
    assert not await page.evaluate("Boolean(streams[1].aborted || streams[2].aborted)")
    assert task_page[3] == []


async def test_streamed_reply_is_restored_without_losing_or_repeating_text(task_page):
    page = await start_two_requests(task_page)
    await emit(page, 2, "message.delta", {"text": "这是新会话的"})
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await emit(page, 2, "message.delta", {"text": "独立回复。"})
    await emit(page, 1, "message.delta", {"text": "B 会话自己的回复"})
    await expect(page.locator("#messages")).to_contain_text("B 会话自己的回复")

    await page.locator("#session-list button").filter(has_text="研究新会话的资料").click()
    response = page.locator("#messages .assistant-message").last
    await expect(response.locator(".message-text")).to_have_text("这是新会话的独立回复。")
    await expect(response.locator(".activity")).to_have_count(0)
    await expect(page.locator("#messages")).not_to_contain_text("B 会话自己的回复")
    await emit(page, 2, "message.delta", {"text": "接着输出。"})
    await expect(response.locator(".message-text")).to_have_text("这是新会话的独立回复。接着输出。")

    # Stopping this reply must not cancel the other conversation's request.
    await page.locator("#stop-button").click()
    assert await page.evaluate("Boolean(streams[2].aborted)")
    assert not await page.evaluate("Boolean(streams[1].aborted)")
    assert task_page[3] == []


@pytest.mark.parametrize("failed", [False, True])
async def test_offscreen_directory_result_or_failure_returns_to_its_session(task_page, failed):
    page = await start_two_requests(task_page)
    await emit(page, 1, "directory.update", {
        "activity": {"id": "plan-b", "text": "正在核对 B 的目录", "status": "running"},
        "elapsed_seconds": 92, "step_elapsed_seconds": 7, "model_status": "responding",
    })
    if failed:
        await emit(page, 1, "message.failed", {
            "text": "B 的目录请求失败，已保留资料。",
            "diagnostics": {"stage": "directory_planning", "status_code": 502, "code": "model_error"},
        })
    else:
        await emit(page, 1, "directory.review", {
            "phase": "directory_review", "revision": "b-directory",
            "title": "B 的投标文件", "reason": "B 的投标目录已生成。",
            "directory": {"sections": [{"number": "1", "title": "B 的投标方案", "level": 1}]},
        })
    await expect(page.locator("#messages")).not_to_contain_text("B 的投标目录已生成。")
    await expect(page.locator("#messages")).not_to_contain_text("B 的目录请求失败")
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    response = page.locator("#messages .assistant-message").last
    await expect(response).to_contain_text("正在核对 B 的目录")
    if failed:
        await expect(response).to_contain_text("B 的目录请求失败，已保留资料。")
        await expect(response.locator(".request-error-details")).to_contain_text("502")
        await expect(response.locator(".is-failed")).to_have_count(1)
    else:
        await expect(response).to_contain_text("B 的投标目录已生成。")
        await expect(response).to_contain_text("B 的投标方案")
        await expect(response).to_contain_text("用时 1 分 32 秒")
    assert not await page.evaluate("Boolean(streams[2].aborted)")
    assert task_page[3] == []
