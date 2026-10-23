"""Replaying saved work must not make an already stopped document task look live."""

import re

import pytest
from playwright.async_api import expect
from test_workspace_confirmation import CONFIRMATION

pytest_plugins = ("test_workspace_task_navigation",)

HISTORICAL_WORK = "正在将补充内容整合到《需求优化与关键环节建议》（第 3/7 项）。"
GENERATING_TEXT = "正在根据已上传资料和确认目录生成文档。"
TERMINAL_LABELS = [
    ("needs_confirmation", "处理结束 · 展开查看过程"),
    ("published", "处理结束 · 展开查看过程"),
    ("draft", "处理结束 · 展开查看过程"),
    ("paused", "任务已暂停"),
    ("failed", "任务未完成"),
]


async def assert_stopped_history(page, label):
    message = page.locator("#messages .assistant-message").last
    activity = message.locator("details.activity").first
    await expect(message).to_have_attribute("aria-busy", "false")
    await expect(activity).not_to_have_class(re.compile(r"\bis-running\b"))
    await expect(activity.locator(".activity-label")).to_contain_text(label)
    await expect(activity).not_to_have_attribute("open", "")
    await expect(message.locator(".message-text")).not_to_contain_text(GENERATING_TEXT)
    await expect(message.locator(".agent-working")).to_have_count(0)
    await expect(activity.locator(".is-current")).to_have_count(0)
    await activity.locator("summary").first.click()
    await expect(activity.locator(".activity-list")).to_be_visible()
    await expect(activity.locator(".activity-list")).to_contain_text(HISTORICAL_WORK)


@pytest.mark.parametrize(("status", "label"), TERMINAL_LABELS)
async def test_restored_terminal_task_stays_still_while_history_arrives(task_page, status, label):
    page, _, states, errors = task_page
    states["a"]["active_job_status"] = status
    if status == "needs_confirmation":
        states["a"]["pending_confirmation_job"] = "job-a"
        await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=CONFIRMATION))

    await page.reload()
    await page.wait_for_function("streams.length === 1")
    # The server may be slow to replay its terminal event. The persisted status
    # must already suppress the live spinner before the first history frame.
    await expect(page.locator("#messages .assistant-message").last).to_have_attribute("aria-busy", "false")
    await page.evaluate("text => streams[0].emit('work.update', {job_id: 'job-a', event_id: 17, text})", HISTORICAL_WORK)
    await assert_stopped_history(page, label)
    if status == "paused":
        await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    else:
        await expect(page.locator("#stop-button")).to_be_hidden()
    assert errors == []


@pytest.mark.parametrize(("status", "label"), [TERMINAL_LABELS[0], TERMINAL_LABELS[3], TERMINAL_LABELS[4]])
async def test_snapshot_status_wins_over_replayed_work(task_page, status, label):
    page, _, _, errors = task_page
    if status == "needs_confirmation":
        await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(json=CONFIRMATION))
    await page.evaluate("""({status, text}) => {
      streams[0].emit('job.snapshot', {job_id: 'job-a', status, stage: 'review'});
      streams[0].emit('work.update', {job_id: 'job-a', event_id: 18, text});
    }""", {"status": status, "text": HISTORICAL_WORK})

    await assert_stopped_history(page, label)
    if status == "paused":
        await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    else:
        await expect(page.locator("#message-input")).to_be_enabled()
        await expect(page.locator("#stop-button")).to_be_hidden()
    assert errors == []


async def test_late_work_does_not_replace_completed_artifact_or_status(task_page):
    page, _, _, errors = task_page
    await page.evaluate("""() => streams[0].emit('artifact.ready', {
      job_id: 'job-a', filename: '完成文档.docx', download_url: '/jobs/job-a/artifact', status: 'published'
    })""")
    await expect(page.locator("#messages .download-link")).to_be_visible()
    await page.evaluate("text => streams[0].emit('work.update', {job_id: 'job-a', event_id: 19, text})", HISTORICAL_WORK)

    await assert_stopped_history(page, "处理结束 · 展开查看过程")
    await expect(page.locator("#messages .message-text").last).to_have_text("文档已生成，可以下载了。")
    await expect(page.locator("#messages .download-link")).to_have_attribute("href", "/jobs/job-a/artifact")
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#stop-button")).to_be_hidden()
    assert errors == []


async def test_running_snapshot_keeps_real_work_live(task_page):
    page, _, _, errors = task_page
    await page.evaluate("""() => {
      streams[0].emit('job.snapshot', {job_id: 'job-a', status: 'running', stage: 'draft'});
      streams[0].emit('work.update', {job_id: 'job-a', event_id: 20, text: '正在撰写第 2 章。'});
    }""")
    message = page.locator("#messages .assistant-message").last
    activity = message.locator("details.activity").first
    await expect(message).to_have_attribute("aria-busy", "true")
    await expect(activity).to_have_class(re.compile(r"\bis-running\b"))
    await expect(activity).to_have_attribute("open", "")
    await expect(activity.locator(".activity-label")).to_have_text("正在撰写第 2 章。")
    await expect(activity.locator(".is-current")).to_have_count(1)
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await page.evaluate("streams[0].emit('work.update', {job_id: 'job-a', event_id: 21, text: '正在撰写第 3 章。'})")
    await expect(activity.locator(".activity-label")).to_have_text("正在撰写第 3 章。")
    await expect(activity.locator(".activity-item")).to_have_count(2)
    assert errors == []
