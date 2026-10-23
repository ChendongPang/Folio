from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright, expect

WEB = Path(__file__).parents[1] / "src/folio/web/dist"


@pytest.fixture
async def task_page():
    """Exercise the real UI with controlled, indefinitely open event streams."""
    assert (WEB / "index.html").is_file(), "Run npm --prefix frontend ci && npm --prefix frontend run build"
    async with async_playwright() as playwright:
        executable = os.environ.get("FOLIO_CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install local Chromium for browser integration tests")
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        calls = []
        states = {
            "a": {"id": "a", "messages": [{"role": "user", "content": "任务 A"}], "files": [], "active_job": "job-a", "active_job_status": "running"},
            "b": {"id": "b", "messages": [{"role": "user", "content": "普通会话 B"}], "files": [], "active_job": None},
        }
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def serve(route):
            path = urlsplit(route.request.url).path
            assert urlsplit(route.request.url).netloc == "folio.test"
            # Source uploads contain binary DOCX bytes; only JSON request bodies
            # are decoded for assertions below.
            body = None if path.endswith("/sources") else route.request.post_data
            calls.append((route.request.method, path, body))
            if path == "/" or path.startswith("/ui/"):
                source = WEB / ("index.html" if path == "/" else path.removeprefix("/ui/"))
                if not source.is_file():
                    await route.fulfill(status=404, body="")
                else:
                    await route.fulfill(body=source.read_bytes(), content_type=mimetypes.guess_type(source)[0] or "application/octet-stream")
                return
            result = {}
            if path == "/writing-guides":
                result = {"guides": []}
            elif path == "/conversations" and route.request.method == "GET":
                result = {"conversations": [{"id": key, "title": value["messages"][0]["content"] if value["messages"] else "新会话"} for key, value in states.items()], "has_more": False}
            elif path == "/conversations" and route.request.method == "POST":
                result = {"id": "new", "messages": [], "files": [], "active_job": None}
                states["new"] = result
            elif path.startswith("/conversations/") and path.endswith("/sources") and route.request.method == "POST":
                cid = path.split("/")[2]
                files = states[cid].setdefault("files", [])
                name = parse_qs(urlsplit(route.request.url).query)["filename"][0]
                item = {"id": f"source-{cid}-{len(files) + 1}", "name": name, "size": len(route.request.post_data_buffer or b""), "role": "source"}
                files.append(item)
                states[cid].pop("directory_review", None)
                result = {**item, "directory_invalidated": True}
            elif path.startswith("/conversations/") and "/sources/" in path and route.request.method == "DELETE":
                cid, source_id = path.split("/")[2], path.rsplit("/", 1)[1]
                states[cid]["files"] = [item for item in states[cid]["files"] if item["id"] != source_id]
                states[cid].pop("directory_review", None)
                result = {"removed": source_id, "directory_invalidated": True}
            elif path.startswith("/conversations/") and "/sources/" in path and route.request.method == "PATCH":
                cid, source_id = path.split("/")[2], path.rsplit("/", 1)[1]
                role = json.loads(route.request.post_data)["role"]
                if role == "template" and any(item["role"] == "template" and item["id"] != source_id for item in states[cid]["files"]):
                    await route.fulfill(status=422, json={"detail": "已选择模板，请先移除原模板，或明确将其改为普通资料后再选择。"})
                    return
                for item in states[cid]["files"]:
                    if item["id"] == source_id:
                        item["role"] = role
                states[cid].pop("directory_review", None)
                result = {"files": states[cid]["files"], "directory_invalidated": True}
            elif path.startswith("/conversations/") and route.request.method == "GET":
                result = states[path.rsplit("/", 1)[1]]
            elif path.startswith("/conversations/") and route.request.method == "PUT":
                cid, action = path.split("/")[2:]
                body = json.loads(route.request.post_data)
                if action == "draft":
                    assert "files" not in body, "Text drafts must never overwrite the source bindings"
                    states[cid]["draft"] = body["text"]
                    result = {"saved": True}
            elif path.startswith("/jobs/") and route.request.method == "POST":
                body = json.loads(route.request.post_data)
                job_id = path.split("/")[2]
                status = "paused" if path.endswith("/pause") else "running"
                states[body["conversation_id"]]["active_job_status"] = status
                result = {"job_id": job_id, "status": status}
            await route.fulfill(json=result)

        await page.route("**/*", serve)
        await page.add_init_script(r"""
            localStorage.setItem('folio.conversation', 'a');
            const actualFetch = window.fetch;
            window.streams = [];
            window.fetch = async (url, options = {}) => {
              if (/\/jobs\/[^/]+\/stream/.test(url) || /\/conversations\/[^/]+\/messages/.test(url)) {
                const record = {url, options}; streams.push(record);
                const stream = new ReadableStream({start(control) { record.control = control; }});
                record.emit = (type, data) => record.control.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
                // Abort completion is deliberately delayed to expose stale finally races.
                options.signal?.addEventListener('abort', () => { record.aborted = true; });
                return new Response(stream, {headers: {'Content-Type': 'text/event-stream'}});
              }
              return actualFetch(url, options);
            };
        """)
        await page.goto("http://folio.test/")
        await page.wait_for_function("streams.length === 1")
        await expect(page.locator("#new-chat")).to_be_enabled()
        try:
            yield page, calls, states, errors
        finally:
            await browser.close()


async def assert_compact_task_control(page, symbol):
    button = page.locator("#composer #stop-button")
    await expect(button).to_be_visible()
    await expect(button.locator("svg")).to_be_visible()
    await expect(button).to_have_attribute("aria-label", "继续生成" if symbol == "play" else "停止生成")
    await expect(page.locator("#task-control")).to_have_count(0)
    await expect(page.locator("#generate-button")).to_have_count(0)
    await expect(page.locator("#send-button")).to_be_hidden()
    assert not (await button.inner_text()).strip()
    control = await button.bounding_box()
    composer = await page.locator("#composer").bounding_box()
    assert 24 <= control["width"] <= 44
    assert 24 <= control["height"] <= 44
    assert abs(control["width"] - control["height"]) <= 2
    assert 0 <= composer["x"] + composer["width"] - control["x"] - control["width"] <= 32


async def test_active_job_navigation_and_old_stream_cannot_unlock_new_request(task_page):
    page, calls, _, errors = task_page
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#new-chat")).to_be_enabled()
    await expect(page.locator("#session-list button").last).to_be_enabled()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await assert_compact_task_control(page, "stop")
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-settings-dialog")).to_be_visible()
    await page.locator("#model-settings-close").click()
    await page.locator("#materials-toggle").click()
    await expect(page.locator("#materials-panel")).to_be_visible()
    await expect(page.locator("#panel-upload")).to_be_disabled()
    await page.locator("#close-materials").click()
    await page.locator("#new-chat").click()
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#stop-button")).to_be_hidden()
    assert await page.evaluate("streams[0].aborted")
    assert not any(path.endswith("/pause") for _, path, _ in calls)

    await page.locator("#message-input").fill("新会话自己的请求")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 2")
    await page.evaluate("""() => {
      streams[0].emit('work.update', {text: '旧会话不该出现的进度'});
      streams[0].emit('job.failed', {text: '旧会话不该出现的错误'});
      streams[0].control.error(new DOMException('detached', 'AbortError'));
    }""")
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#new-chat")).to_be_enabled()
    assert "旧会话不该" not in await page.locator("#messages").inner_text()
    await page.evaluate("streams[1].emit('job.created', {job_id: 'job-new'})")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await page.locator("#stop-button").click()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    assert ("POST", "/jobs/job-new/pause", '{"conversation_id":"new"}') in calls
    assert errors == []


async def test_pause_and_resume_preserve_conversation_lock_and_navigation(task_page):
    page, calls, states, errors = task_page
    await page.locator("#stop-button").click()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    assert states["a"]["active_job_status"] == "paused"
    assert ("POST", "/jobs/job-a/pause", '{"conversation_id":"a"}') in calls
    await page.evaluate("streams[0].emit('job.paused', {job_id: 'job-a', text: '已保存进度'})")
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await assert_compact_task_control(page, "play")
    await page.set_viewport_size({"width": 1280, "height": 900})

    await page.locator("#stop-button").click()
    await page.wait_for_function("streams.length === 2")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    assert ("POST", "/jobs/job-a/resume", '{"conversation_id":"a"}') in calls
    # A frame still queued in the old observer cannot undo the resume.
    await page.evaluate("streams[0].emit('job.paused', {job_id: 'job-a', text: '迟到的暂停'})")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await page.evaluate("streams[1].emit('artifact.ready', {filename: '文档.docx', download_url: '/download', status: 'published'})")
    await expect(page.locator("#stop-button")).to_be_hidden()
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#messages .download-link")).to_be_visible()
    assert errors == []


async def test_return_to_running_or_paused_session_does_not_wait_for_stream(task_page):
    page, _, states, errors = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await expect(page.locator("#message-input")).to_be_enabled()
    states["a"]["active_job_status"] = "paused"
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#messages")).to_contain_text("任务 A")
    await page.wait_for_function("streams.length === 2")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.locator("#new-chat").click()
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []


async def test_background_message_completion_does_not_replace_new_conversation(task_page):
    page, _, _, errors = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.locator("#message-input").fill("处理 B 的消息")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 2")
    await page.locator("#new-chat").click()
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.locator("#message-input").fill("处理新会话的消息")
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.length === 3")
    await page.evaluate("""() => {
      streams[1].emit('job.created', {job_id: 'job-b'});
      streams[1].emit('job.failed', {text: '旧请求错误'});
      streams[1].control.close();
    }""")
    await expect(page.locator("#message-input")).to_be_disabled()
    await expect(page.locator("#messages")).to_contain_text("处理新会话的消息")
    await expect(page.locator("#messages")).not_to_contain_text("处理 B 的消息")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止回答")
    assert "旧请求错误" not in await page.locator("#messages").inner_text()
    await page.evaluate("""() => {
      streams[2].emit('message.completed', {text: '新会话完成'});
      streams[2].control.close();
    }""")
    await expect(page.locator("#message-input")).to_be_enabled()
    assert "新会话完成" in await page.locator("#messages").inner_text()
    assert errors == []


async def test_local_draft_for_running_job_does_not_block_new_conversation(task_page):
    page, calls, _, errors = task_page
    await page.evaluate("localStorage.setItem('folio.draft.a', JSON.stringify({text: '保留的输入草稿', files: []}))")
    await page.reload()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "停止生成")
    await expect(page.locator("#message-input")).to_have_value("保留的输入草稿")
    await page.locator("#new-chat").click()
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_be_enabled()
    assert not any(method == "PUT" and path == "/conversations/a/draft" for method, path, _ in calls)
    assert await page.evaluate("JSON.parse(localStorage.getItem('folio.draft.a')).text") == "保留的输入草稿"
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []


async def test_pending_pause_waits_for_server_confirmation(task_page):
    page, _, _, errors = task_page
    await page.route("**/jobs/job-a/pause", lambda route: route.fulfill(status=202, json={"job_id": "job-a", "status": "pausing"}))
    await page.locator("#stop-button").click()
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "正在停止")
    await expect(page.locator("#stop-button")).to_be_disabled()
    await expect(page.locator("#new-chat")).to_be_enabled()
    await page.evaluate("streams[0].emit('job.paused', {job_id: 'job-a', text: '后台已暂停'})")
    await expect(page.locator("#stop-button")).to_have_attribute("aria-label", "继续生成")
    await expect(page.locator("#stop-button")).to_be_enabled()
    await expect(page.locator("#message-input")).to_be_disabled()
    assert errors == []


async def test_conversation_draft_is_saved_before_switch_and_restored(task_page):
    page, calls, states, errors = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.locator("#message-input").fill("下次继续整理的文档要求")
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#messages")).to_contain_text("任务 A")
    assert states["b"]["draft"] == "下次继续整理的文档要求"
    assert any(method == "PUT" and path == "/conversations/b/draft" for method, path, _ in calls)
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#message-input")).to_have_value("下次继续整理的文档要求")
    await expect(page.locator("#session-save-status")).to_have_text("已保存")
    assert errors == []


async def test_workspace_has_only_materials_and_no_knowledge_placeholders(task_page):
    page, calls, _, errors = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(page.locator("#knowledge-open, #knowledge-dialog, #knowledge-save")).to_have_count(0)
    await page.locator("#materials-toggle").click()
    await expect(page.locator("#materials-panel")).to_be_visible()
    await expect(page.locator("#material-list")).to_contain_text("还没有资料")
    await expect(page.locator("#panel-upload")).to_be_enabled()
    await page.locator("#close-materials").click()
    await page.locator("#new-chat").click()
    await expect(page.locator("#welcome")).to_be_visible()
    for stale_label in ("Wiki", "RAG", "知识工程", "待接入"):
        await expect(page.locator("body")).not_to_contain_text(stale_label)
    assert not any("/knowledge" in path for _, path, _ in calls)
    assert errors == []


async def test_restored_directory_uses_saved_sources_and_current_revision(task_page):
    page, _, states, errors = task_page
    states["b"]["files"] = [{"id": "source-b", "name": "research.md", "size": 12, "role": "source"}]
    states["b"]["directory_review"] = {
        "phase": "directory_review", "revision": "directory-current", "reason": "请确认完整目录。",
        "directory": {"sections": [{"number": "1", "title": "项目背景", "level": 1}]},
    }
    # A legacy local draft must not replace the server's authoritative source list.
    await page.evaluate("localStorage.setItem('folio.draft.b', JSON.stringify({text: '继续完善方案', files: []}))")
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#file-count")).to_have_text("1")
    await expect(page.locator("#messages .directory-review-tree")).to_contain_text("项目背景")
    await page.get_by_role("button", name="确认目录，生成文档", exact=True).click()
    await page.wait_for_function("streams.length === 2")
    payload = await page.evaluate("JSON.parse(streams.at(-1).options.body)")
    assert payload["action"] == "confirm_directory"
    assert payload["selection_revision"] == "directory-current"
    assert "files" not in payload
    assert states["b"]["files"][0]["id"] == "source-b"
    assert states["b"]["draft"] == "继续完善方案"
    assert errors == []


async def test_upload_queue_is_cancelled_and_late_results_stay_in_the_original_session(task_page):
    page, _, states, errors = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.evaluate(r"""() => {
      const actualFetch = window.fetch;
      window.sourceUploads = [];
      window.fetch = (url, options = {}) => {
        if (/\/conversations\/[^/]+\/sources\?/.test(url)) {
          const record = {url}; sourceUploads.push(record);
          options.signal?.addEventListener('abort', () => { record.aborted = true; });
          // Deliberately let aborted requests finish later to test stale response isolation.
          return new Promise(resolve => {
            record.finish = id => resolve(new Response(JSON.stringify({id, directory_invalidated: true}), {
              headers: {'Content-Type': 'application/json'}
            }));
          });
        }
        return actualFetch(url, options);
      };
    }""")
    await page.locator("#file-input").set_input_files([
        {"name": f"source-{index}.md", "mimeType": "text/markdown", "buffer": f"Source {index}".encode()}
        for index in range(5)
    ])
    await page.wait_for_function("sourceUploads.length === 3")
    await expect(page.locator("#file-count")).to_have_text("5")
    await page.locator("#close-materials").click()
    await page.locator("#new-chat").click()
    await expect(page.locator("#file-count")).to_have_text("0")
    assert await page.evaluate("sourceUploads.every(upload => upload.aborted)")

    # New conversation uploads must not wait for obsolete requests' finally blocks.
    await page.locator("#file-input").set_input_files({"name": "new-session.txt", "mimeType": "text/plain", "buffer": b"New session source"})
    await page.wait_for_function("sourceUploads.length === 4")
    assert await page.evaluate("sourceUploads[3].url.includes('/conversations/new/sources?')")
    states["new"]["files"] = [{"id": "new-source", "name": "new-session.txt", "size": 18, "role": "source"}]
    await page.evaluate("sourceUploads[3].finish('new-source')")
    await expect(page.locator("#toast")).to_contain_text("资料已上传并保存")

    states["b"]["files"] = [
        {"id": f"old-source-{index}", "name": f"source-{index}.md", "size": 8, "role": "source"}
        for index in range(3)
    ]
    await page.evaluate("""async () => {
      sourceUploads.slice(0, 3).forEach((upload, index) => upload.finish(`old-source-${index}`));
      await new Promise(resolve => requestAnimationFrame(resolve));
    }""")
    assert await page.evaluate("sourceUploads.length") == 4
    await expect(page.locator("#file-count")).to_have_text("1")
    await expect(page.locator("#staged-files")).to_contain_text("new-session.txt")
    await expect(page.locator("#staged-files")).not_to_contain_text("source-0.md")
    await page.locator("#close-materials").click()
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#file-count")).to_have_text("3")
    await expect(page.locator("#staged-files")).to_contain_text("source-0.md")
    assert errors == []


async def test_completed_job_loads_result_before_optional_history(task_page):
    page, _, states, errors = task_page
    states['a']['active_job_status'] = 'published'
    await page.reload()
    await page.wait_for_function('streams.length === 1')
    assert 'replay_history=false' in await page.evaluate('streams[0].url')
    await page.evaluate("""() => {
      streams[0].emit('job.snapshot', {job_id:'job-a', status:'published'});
      streams[0].emit('artifact.ready', {job_id:'job-a', filename:'result.docx', download_url:'/download', status:'published', elapsed_ms:1021000, task_usage:[{task_id:'t1',elapsed_ms:2499000,total_tokens:100}]});
      streams[0].control.close();
    }""")
    await expect(page.locator('.download-card')).to_be_visible()
    await expect(page.locator('.task-usage > summary')).to_contain_text('17')
    await expect(page.locator('.task-usage > summary')).not_to_contain_text('41')
    assert await page.evaluate('streams.length') == 1
    summary = page.locator('details.activity > summary').first
    await summary.click()
    await page.wait_for_function('streams.length === 2')
    await page.evaluate("""() => {
      streams[1].emit('work.update', {text:'Saved history'});
      streams[1].control.close();
    }""")
    await expect(page.locator('.activity-copy')).to_contain_text('Saved history')
    await summary.click()
    await summary.click()
    assert await page.evaluate('streams.length') == 2
    assert errors == []
