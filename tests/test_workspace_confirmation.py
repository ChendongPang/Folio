"""User answers survive failures and navigation without duplicate retry grants."""

import asyncio
import json
import re

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)

CONFIRMATION = {
    "job_id": "job-a", "status": "needs_confirmation", "revision": "review-1",
    "issues": [
        {"issue_id": "issue-1", "description": "审批上限是50万还是100万？", "section_ids": ["s1"], "key": "amount", "kind": "pending"},
        {"issue_id": "issue-2", "description": "需要什么角色审批？", "section_ids": ["s1"], "key": "role", "kind": "pending"},
    ],
    "used_calls": 120, "default_additional_calls": 200, "rounds": 0, "history": [],
}
ARCHIVE = {
    "round": 1, "request_id": "old-request",
    "download_url": "/jobs/job-a/confirmations/old-request/artifact",
    "filename": "补答前草稿（第1轮）.docx",
}


async def show_pending(page, states):
    states["a"].update(active_job_status="needs_confirmation", pending_confirmation_job="job-a")
    await page.evaluate("""() => streams.at(-1).emit('artifact.ready', {
      job_id: 'job-a', filename: '文档草稿.docx', download_url: '/jobs/job-a/artifact',
      status: 'needs_confirmation', pending_count: 2, confirmation_url: '/jobs/job-a/confirmation'
    })""")
    await expect(page.locator("#confirmation-panel")).to_be_visible()
    await expect(page.locator("#confirmation-calls")).to_have_value("200")


async def test_partial_answers_retry_same_request_and_archive_previous_download(task_page):
    page, _, states, errors = task_page
    submitted = []

    async def route(request):
        if request.request.method == "GET":
            await request.fulfill(json=CONFIRMATION)
            return
        body = json.loads(request.request.post_data)
        submitted.append(body)
        if len(submitted) == 1:
            await request.fulfill(status=503, json={"detail": "服务暂不可用"})
        else:
            states["a"].update(active_job_status="running", pending_confirmation_job=None)
            await request.fulfill(json={"job_id": "job-a", "status": "running"})

    await page.route("**/jobs/job-a/confirmation*", route)
    await show_pending(page, states)
    await expect(page.locator("#confirmation-panel")).to_contain_text("此前已调用 120 次")
    await expect(page.locator("#confirmation-submit")).to_have_text("重新检查")
    await page.locator("#confirmation-answer-0").fill("审批上限为100万元")
    await page.locator("#confirmation-calls").fill("7")
    await expect(page.locator("#confirmation-submit")).to_have_text("补答并继续")
    await page.locator("#confirmation-submit").click()
    await expect(page.locator(".confirmation-error")).to_contain_text("填写内容已保留")
    await expect(page.locator("#confirmation-answer-0")).to_have_value("审批上限为100万元")
    await expect(page.locator(".download-link")).to_have_attribute("href", "/jobs/job-a/artifact")
    await page.locator("#confirmation-submit").click()
    await page.wait_for_function("streams.length === 2")
    assert submitted[0] == submitted[1]
    assert re.fullmatch("[a-f0-9]{32}", submitted[0]["request_id"])
    assert submitted[0]["answers"] == [{"issue_id": "issue-1", "answer": "审批上限为100万元"}]
    assert submitted[0]["additional_calls"] == 7
    assert submitted[0]["revision"] == "review-1"
    await expect(page.locator("#confirmation-panel")).to_have_count(0)
    await expect(page.locator(".download-link")).to_have_attribute("href", f"/jobs/job-a/confirmations/{submitted[0]['request_id']}/artifact")
    await expect(page.locator(".download-link")).to_have_text("下载之前的草稿")
    await expect(page.locator("#message-input")).to_be_disabled()
    await page.evaluate("streams.at(-1).emit('artifact.ready', {job_id: 'job-a', filename: '完成文档.docx', download_url: '/jobs/job-a/artifact', status: 'published'})")
    await expect(page.locator(".download-link")).to_have_count(2)
    await expect(page.locator(".download-link").last).to_have_text("下载文档")
    await expect(page.locator(".download-link").first).to_have_text("下载之前的草稿")
    assert errors == []


async def test_confirmation_answers_restore_after_switching_and_reload(task_page):
    page, _, states, errors = task_page
    await page.route("**/jobs/job-a/confirmation*", lambda request: request.fulfill(json=CONFIRMATION))
    await show_pending(page, states)
    await page.locator("#confirmation-answer-1").fill("财务主管审批")
    await page.locator("#confirmation-calls").fill("15")
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#confirmation-panel")).to_have_count(0)
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#confirmation-answer-1")).to_have_value("财务主管审批")
    await expect(page.locator("#confirmation-calls")).to_have_value("15")
    await page.reload()
    await expect(page.locator("#confirmation-answer-1")).to_have_value("财务主管审批")
    await expect(page.locator("#confirmation-calls")).to_have_value("15")
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


async def test_empty_retry_submission_does_not_interfere_with_other_conversation(task_page):
    page, _, states, errors = task_page
    posted, release = asyncio.Event(), asyncio.Event()
    submitted = []

    async def route(request):
        if request.request.method == "GET":
            await request.fulfill(json=CONFIRMATION)
            return
        submitted.append(json.loads(request.request.post_data))
        posted.set()
        await release.wait()
        states["a"].update(active_job_status="running", pending_confirmation_job=None)
        await request.fulfill(json={"job_id": "job-a", "status": "running"})

    await page.route("**/jobs/job-a/confirmation*", route)
    await show_pending(page, states)
    await page.locator("#confirmation-submit").click()
    await asyncio.wait_for(posted.wait(), 3)
    await expect(page.locator("#confirmation-submit")).to_be_disabled()
    await expect(page.locator("#message-input")).to_be_disabled()
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    release.set()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#confirmation-panel")).to_have_count(0)
    assert submitted[0]["answers"] == [] and submitted[0]["additional_calls"] == 200
    assert len(submitted) == 1
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#message-input")).to_be_disabled()
    assert errors == []


async def test_published_conversation_restores_archived_downloads_after_reload(task_page):
    page, _, states, errors = task_page
    states["a"].update(active_job_status="published", confirmation_history=[ARCHIVE])
    await page.reload()
    await expect(page.locator(".artifact-history summary")).to_contain_text("之前的草稿")
    await page.locator(".artifact-history summary").click()
    await expect(page.locator(".artifact-history .download-link")).to_have_attribute("href", ARCHIVE["download_url"])
    await page.evaluate("""history => streams.at(-1).emit('artifact.ready', {
      job_id: 'job-a', filename: '完成文档.docx', download_url: '/jobs/job-a/artifact',
      status: 'published', confirmation_history: history
    })""", [ARCHIVE])
    await expect(page.locator(".download-link")).to_have_count(2)
    await expect(page.locator(".artifact-history .download-link")).to_have_attribute("href", ARCHIVE["download_url"])
    assert errors == []


async def test_conflicting_continuation_archives_stale_download_before_observing(task_page):
    page, _, states, errors = task_page
    submitted = False

    async def route(request):
        nonlocal submitted
        if request.request.method == "POST":
            submitted = True
            await request.fulfill(status=409, json={"detail": "待确认内容已更新"})
            return
        await request.fulfill(json=CONFIRMATION | ({"status": "running", "history": [ARCHIVE], "rounds": 1} if submitted else {}))

    await page.route("**/jobs/job-a/confirmation*", route)
    await show_pending(page, states)
    await page.locator("#confirmation-submit").click()
    await page.wait_for_function("streams.length === 2")
    await expect(page.locator("#confirmation-panel")).to_have_count(0)
    await expect(page.locator(".download-link")).to_have_attribute("href", ARCHIVE["download_url"])
    await expect(page.locator(".download-link")).to_have_text("下载之前的草稿")
    await page.evaluate("""history => streams.at(-1).emit('artifact.ready', {
      job_id: 'job-a', filename: '完成文档.docx', download_url: '/jobs/job-a/artifact',
      status: 'published', confirmation_history: history
    })""", [ARCHIVE])
    await expect(page.locator(".download-link")).to_have_count(2)
    await expect(page.locator(".download-link").first).to_have_attribute("href", ARCHIVE["download_url"])
    assert errors == []


async def test_source_comment_requires_explicit_resolution_and_restores_checkbox(task_page):
    page, _, states, errors = task_page
    view = CONFIRMATION | {"issues": [CONFIRMATION["issues"][0] | {"key": "source-comment:1"}, CONFIRMATION["issues"][1]]}
    submitted = []

    async def route(request):
        if request.request.method == "GET":
            await request.fulfill(json=view)
            return
        submitted.append(json.loads(request.request.post_data))
        if len(submitted) == 1:
            await request.fulfill(status=503, json={"detail": "服务暂不可用"})
        else:
            await request.fulfill(json={"job_id": "job-a", "status": "running"})

    await page.route("**/jobs/job-a/confirmation*", route)
    await show_pending(page, states)
    checkbox = page.get_by_role("checkbox", name="此答复已解决这条来源批注")
    await expect(checkbox).to_have_count(1)
    await expect(checkbox).to_be_disabled()
    await page.locator("#confirmation-answer-0").fill("暂时不知道，请保留批注")
    await expect(checkbox).to_be_enabled()
    await expect(checkbox).not_to_be_checked()
    await page.locator("#confirmation-submit").click()
    await expect(page.locator(".confirmation-error")).to_be_visible()
    assert not submitted[0]["answers"][0].get("close_comment", False)
    await page.locator("#confirmation-answer-0").fill("已确认审批上限为100万元")
    await checkbox.check()
    await page.reload()
    await expect(checkbox).to_be_checked()
    await expect(page.locator("#confirmation-answer-0")).to_have_value("已确认审批上限为100万元")
    await page.locator("#confirmation-submit").click()
    await page.wait_for_function("streams.length === 2")
    assert submitted[1]["answers"][0]["close_comment"] is True
    assert submitted[1]["request_id"] != submitted[0]["request_id"]
    assert errors == []
