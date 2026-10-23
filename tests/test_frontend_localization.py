"""System copy follows UI language; user text and source titles stay unchanged."""

import re
from pathlib import Path

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)
ROOT = Path(__file__).resolve().parents[1]


def test_all_frontend_translation_keys_have_english_values():
    catalog = (ROOT / "frontend/src/i18n.ts").read_text()
    keys = set(re.findall(r"^  '([^']+)':", catalog, re.MULTILINE))
    missing = []
    for path in (ROOT / "frontend/src").rglob("*"):
        if path.suffix not in (".ts", ".vue") or path.name == "i18n.ts":
            continue
        for key in re.findall(r"\btrf?\('([^']+)'", path.read_text()):
            if key not in keys:
                missing.append(f"{path.relative_to(ROOT)}: {key}")
    assert missing == []


async def switch(page, language):
    await page.locator("#language-select").select_option(language)
    await expect(page.locator("html")).to_have_attribute("lang", language)


async def test_legacy_review_stream_translates_complete_sentence(task_page):
    page, _, _, errors = task_page
    source = "跨章节检查第 16/47 组：《2.5.1 Event Calendar》、《2.5.2 Job Board》等 6 节。"
    await switch(page, "en")
    await page.evaluate("text => streams[0].emit('work.update', {event_id: 1, text})", source)
    await expect(page.locator(".activity-copy").last).to_have_text(
        "Cross-section review, group 16/47: “2.5.1 Event Calendar”, “2.5.2 Job Board” and others (6 sections total)."
    )
    await switch(page, "zh-CN")
    await expect(page.locator(".activity-copy").last).to_have_text(source)
    assert errors == []


async def test_default_session_title_switches_but_explicit_same_title_does_not(task_page):
    page, _, states, errors = task_page
    states["a"].update(title="新文档", title_is_default=True, messages=[], active_job=None)
    states["b"].update(title="New document", title_is_default=False)

    async def listing(route):
        await route.fulfill(json={"conversations": [
            {"id": key, "title": value["title"], "title_is_default": value["title_is_default"]}
            for key, value in states.items()
        ], "has_more": False})

    await page.route("**/conversations?*", listing)
    await page.reload()
    await switch(page, "en")
    await expect(page.locator(".session-title").nth(0)).to_have_text("New document")
    await expect(page.locator("#conversation-title")).to_have_text("New document")
    await switch(page, "zh-CN")
    await expect(page.locator(".session-title").nth(0)).to_have_text("新的文档")
    await expect(page.locator(".session-title").nth(1)).to_have_text("New document")
    await page.locator(".session-item").nth(1).click()
    await expect(page.locator("#conversation-title")).to_have_text("New document")
    assert errors == []


async def test_progress_switches_both_ways_without_hiding_chinese_source_names(task_page):
    page, _, _, errors = task_page
    chinese = "正在将补充内容整合到《正文原文研究》（第 3/7 项）。"
    english = "Integrating supplemental content into “正文原文研究” (item 3/7)."
    await switch(page, "en")
    await page.evaluate("text => streams[0].emit('work.update', {event_id: 1, text})", chinese)
    await expect(page.locator(".activity-copy").last).to_have_text(english)
    await switch(page, "zh-CN")
    await expect(page.locator(".activity-copy").last).to_have_text(chinese)
    await expect(page.locator(".activity-label").last).to_have_text(chinese)
    await switch(page, "en")
    await expect(page.locator(".activity-label").last).to_have_text(english)
    assert errors == []


async def test_server_translation_metadata_updates_saved_and_live_system_copy(task_page):
    page, _, states, errors = task_page
    original = "已核对 17 条正文约束。"
    translated = "Verified 17 body constraints."
    states["a"].update(
        messages=[{"role": "assistant", "content": translated, "content_origin": "system"}],
        _translations={original: translated},
    )
    await page.reload()
    await expect(page.locator(".message-text").first).to_have_text(original)
    await switch(page, "en")
    await expect(page.locator(".message-text").first).to_have_text(translated)
    await page.evaluate("""() => streams[0].emit('work.update', {
        event_id: 22, text: 'Verified 42 paragraphs.',
        _translations: {'已核对 42 个段落。': 'Verified 42 paragraphs.'}
    })""")
    await expect(page.locator(".activity-copy").last).to_have_text("Verified 42 paragraphs.")
    await switch(page, "zh-CN")
    await expect(page.locator(".activity-copy").last).to_have_text("已核对 42 个段落。")
    await expect(page.locator(".activity-label").last).to_have_text("已核对 42 个段落。")
    assert errors == []


async def test_diagnostic_details_recompute_after_language_change(task_page):
    page, _, _, errors = task_page
    await page.evaluate("""() => streams[0].emit('message.failed', {
        text: '本次请求未完成。', diagnostics: {
            stage: 'directory_planning', elapsed_ms: 2100,
            code: 'protocol', protocol_issue: 'tool_arguments_json', finish_reason: 'length'
        }
    })""")
    details = page.locator(".request-error-details").last
    await details.locator("summary").click()
    await expect(details).to_contain_text("工具参数不是合法的 JSON")
    await switch(page, "en")
    await expect(details).to_contain_text("Tool arguments are not valid JSON")
    await expect(details).to_contain_text("Outline planning")
    await expect(details).to_contain_text("2.10 s")
    await expect(details).not_to_contain_text(re.compile(r"[\u3400-\u9fff]"))
    await switch(page, "zh-CN")
    await expect(details).to_contain_text("目录规划")
    await expect(details).to_contain_text("工具参数不是合法的 JSON")
    assert errors == []


async def test_research_and_confirmation_system_fields_follow_language(task_page):
    page, _, states, errors = task_page
    summary = "已完成资料检查。"
    issue = "模板结构尚未确认。"
    states["a"].update(
        active_job=None, active_job_status=None,
        messages=[{"role": "user", "content": "原始用户要求：保留中文标题"}],
        directory_review={
            "phase": "directory_review", "revision": "i18n-research", "title": "原文标题",
            "directory": {
                "confirmable": False,
                "sections": [{"number": "1", "title": "中文章节标题", "level": 1}],
                "confirmation_issues": [{"code": "template", "message": issue}],
                "research": {
                    "status": "partial", "summary": summary, "unresolved": [issue],
                    "files": [{"path": "D1", "name": "原文资料.docx", "read": True, "inspected": True,
                               "readings": [{"label": "第 1—3 页", "characters": 100}]}],
                    "web_research": {"searches": [{"query": "Public requirements", "status": "failed", "evidence_ids": []}]},
                },
            },
        },
        _translations={summary: "Source checks are complete.", issue: "Template structure is not yet confirmed."},
    )
    await page.reload()
    await switch(page, "en")
    await expect(page.locator(".directory-error")).to_contain_text("Template structure is not yet confirmed.")
    await page.locator(".preview-toggle").click()
    research = page.locator(".directory-research")
    await expect(research).to_contain_text("Source checks are complete.")
    await expect(research).to_contain_text("Pages 1–3")
    await expect(research).to_contain_text("Web research · 1 searches")
    await expect(research).to_contain_text("原文资料.docx")
    await expect(page.locator(".directory-review-tree")).to_contain_text("中文章节标题")
    await expect(page.locator(".user-content")).to_have_text("原始用户要求：保留中文标题")
    await switch(page, "zh-CN")
    await expect(research).to_contain_text(summary)
    await expect(page.locator(".directory-error")).to_contain_text(issue)
    assert errors == []


async def test_settings_feedback_and_validation_are_localized(task_page):
    page, _, _, errors = task_page
    headers = []

    async def settings(route):
        headers.append(route.request.headers.get("accept-language"))
        if route.request.method == "PUT":
            await route.fulfill(status=422, json={"detail": [
                {"loc": ["body", "supports_image_input"], "type": "extra_forbidden"},
            ]})
        else:
            await route.fulfill(json={"configured": True, "base_url": "https://model.test/v1", "model": "test", "no_auth": True})

    await page.route("**/settings/model", settings)
    await switch(page, "en")
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-base-url")).to_be_enabled()
    await page.locator("#model-name").fill("new-model")
    await expect(page.locator("#model-test-status")).to_have_text("Settings changed. Test again.")
    await page.locator("#save-model-button").click()
    result = page.locator("#model-settings-result")
    await expect(result).to_contain_text("The server does not support these settings: Image input.")
    await expect(page.locator("#model-settings-dialog")).not_to_contain_text(re.compile(r"[\u3400-\u9fff]"))
    await page.locator("#model-settings-close").click()
    await expect(page).to_have_title("Folio · Intelligent document workspace")
    await switch(page, "zh-CN")
    await expect(page).to_have_title("Folio · 智能文档工作台")
    assert headers and all(value == "en" for value in headers)
    assert errors == []


async def test_upload_validation_and_pending_confirmation_errors_switch_live(task_page):
    page, _, states, errors = task_page
    states["a"].update(active_job=None, active_job_status=None, pending_confirmation_job="job-a")
    await page.route("**/jobs/job-a/confirmation*", lambda route: route.fulfill(status=404, json={
        "detail": "Confirmation data was not found.", "_translations": {"待确认数据不存在。": "Confirmation data was not found."},
    }))
    await page.reload()
    await switch(page, "en")
    await expect(page.locator(".confirmation-error")).to_have_text("Unable to load confirmation questions: Confirmation data was not found.")
    await page.locator("#file-input").set_input_files({"name": "无内容.txt", "mimeType": "text/plain", "buffer": b""})
    await expect(page.locator("#toast")).to_contain_text("无内容.txt: Files must not be empty.")
    await switch(page, "zh-CN")
    await expect(page.locator("#toast")).to_contain_text("无内容.txt：文件不能为空。")
    await expect(page.locator(".confirmation-error")).to_have_text("待确认问题未能加载：待确认数据不存在。")
    assert errors == []


async def test_model_prose_keeps_requested_language_across_stream_and_history(task_page):
    page, _, states, errors = task_page
    states["a"].update(messages=[
        {"role": "assistant", "content": "是", "content_origin": "model"},
        {"role": "assistant", "content": "文件不能为空。"},
    ])
    await page.reload()
    await switch(page, "en")
    await expect(page.locator(".message-text").nth(0)).to_have_text("是")
    await expect(page.locator(".message-text").nth(1)).to_have_text("文件不能为空。")
    await page.evaluate("""() => {
        streams[0].emit('message.delta', {text: '文件不能为空。', content_origin: 'model'});
        streams[0].emit('message.completed', {text: '文件不能为空。', content_origin: 'model'});
    }""")
    await expect(page.locator(".message-text").last).to_have_text("文件不能为空。")
    await switch(page, "zh-CN")
    await expect(page.locator(".message-text").last).to_have_text("文件不能为空。")
    assert errors == []


async def test_preview_preserves_real_titles_and_body_but_translates_generated_label(task_page):
    page, _, states, errors = task_page
    states["a"].update(active_job=None, active_job_status=None, directory_review={
        "phase": "directory_review", "revision": "preserve-preview", "title": "正文",
        "directory": {"sections": [{"number": "1", "title": "标题", "level": 1}]},
    })
    await page.reload()
    await switch(page, "en")
    await expect(page.locator("#preview-title")).to_have_text("正文")
    states["a"]["directory_review"]["directory"]["preview"] = {
        "id": "preview-original", "name": "完整目录预览", "download_url": "/preview.docx",
    }
    await page.route("**/previews/preview-original", lambda route: route.fulfill(json={"blocks": [
        {"kind": "heading", "level": 1, "text": "正文"},
        {"kind": "paragraph", "text": "文件不能为空。"},
    ]}))
    await page.route("**/previews/preview-original/pages", lambda route: route.fulfill(json={"pages": []}))
    await page.reload()
    await expect(page.locator("#preview-title")).to_have_text("Complete outline preview")
    await expect(page.locator("#preview-structure")).to_contain_text("正文")
    await expect(page.locator("#preview-structure")).to_contain_text("文件不能为空。")
    await switch(page, "zh-CN")
    await expect(page.locator("#preview-title")).to_have_text("完整目录预览")
    assert errors == []
