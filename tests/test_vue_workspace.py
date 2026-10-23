"""Browser checks for the Vue settings and persisted material interactions."""

import asyncio
import json
from copy import deepcopy

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)

MODEL_CONFIG = {
    "configured": True,
    "provider_mode": "custom",
    "base_url": "https://model.example.test/v1",
    "model": "workspace-model",
    "has_api_key": True,
    "no_auth": False,
    "decision_model": "judge",
    "decision_base_url": "https://judge.test/v1",
    "search_provider": "tavily",
    "has_deepseek_api_key": False,
    "decision_enabled": False,
    "has_decision_api_key": False,
    "tavily_enabled": False,
    "has_tavily_api_key": False,
    "max_tokens": 8192,
    "planning_max_tokens": 16384,
    "temperature": 0.1,
    "enable_thinking": False,
    "structured_output": "json_schema",
    "test_status": {"llm": True, "decision": False, "tavily": False},
}


async def model_routes(page, config=None):
    """Use only synthetic model responses; never contact a provider."""
    saved = deepcopy(config or MODEL_CONFIG)
    requests = []

    async def serve(route):
        request = route.request
        if request.method == "GET":
            await route.fulfill(json=saved)
            return
        body = json.loads(request.post_data)
        requests.append((request.method, request.url.rsplit("/", 1)[-1], body))
        if request.method == "POST":
            await route.fulfill(json={"ok": True, "message": "测试通过"})
            return
        for key, value in body.items():
            if key not in {"api_key", "decision_api_key", "tavily_api_key", "deepseek_api_key"}:
                saved[key] = value
        saved["configured"] = True
        for key in ["api_key", "decision_api_key", "tavily_api_key", "deepseek_api_key"]:
            saved[f"has_{key}"] = bool(body[key] or saved.get(f"has_{key}"))
        saved["test_status"] = {
            "llm": True,
            "decision": body["decision_enabled"],
            "tavily": body["tavily_enabled"],
        }
        await route.fulfill(json=saved)

    await page.route("**/settings/model**", serve)
    return requests


async def test_document_requirements_use_chat_without_a_document_type_selector(task_page):
    page, calls, states, errors = task_page
    guide = {"id": "srs", "name": "软件需求规格说明书", "version": 1}
    states["b"]["writing_guide"] = guide
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#messages")).to_contain_text("普通会话 B")
    assert states["b"]["writing_guide"] == guide
    await page.locator("#new-chat").click()
    await expect(page.locator(".brand-wordmark")).to_have_text("Folio")
    await expect(page.locator(".workspace-section-bar")).to_contain_text("文档工作台")
    await expect(page.locator("#writing-guide")).to_have_count(0)
    await expect(page.get_by_text("文档类型", exact=True)).to_have_count(0)
    await expect(page.get_by_text("通用文档", exact=True)).to_have_count(0)
    request = "请根据这些资料写一份需求说明书，重点说明业务流程和验收标准。"
    await page.locator("#message-input").fill(request)
    await page.locator("#send-button").click()
    await page.wait_for_function("streams.some(item => item.url.endsWith('/messages'))")
    sent = await page.evaluate("JSON.parse(streams.find(item => item.url.endsWith('/messages')).options.body)")
    assert sent["text"] == request
    assert sent["action"] == "chat"
    assert "guide_id" not in sent
    created = [body for method, path, body in calls if method == "POST" and path == "/conversations"]
    assert created == [None]  # The server's existing general-document default remains authoritative.
    assert not any("writing-guide" in path for _, path, _ in calls)
    assert errors == []


async def test_language_setting_switches_workspace_and_persists_choice(task_page):
    page, _, _, errors = task_page
    selector = page.locator("#language-select")
    await selector.select_option("en")
    await expect(page.locator(".workspace-section-bar")).to_contain_text("Document workspace")
    await expect(page.locator("#new-chat")).to_contain_text("New document")
    await page.locator("#new-chat").click()
    await expect(page.locator("#conversation-title")).to_have_text("New document")
    await expect(page.locator(".suggestion-card")).to_have_text([
      "01Bid proposal↗",
      "02Requirements specification↗",
      "03Research report↗",
    ])
    await expect(page.locator("#upload-sources")).to_contain_text("Upload sources")
    assert await page.evaluate("document.documentElement.lang") == "en"
    assert await page.evaluate("localStorage.getItem('folio.language')") == "en"
    await page.reload()
    await expect(page.locator("#language-select")).to_have_value("en")
    await expect(page.locator(".workspace-section-bar")).to_contain_text("Document workspace")
    await page.locator("#new-chat").click()
    await expect(page.locator("#upload-sources")).to_contain_text("Upload sources")
    assert errors == []


async def test_long_message_is_sent_without_the_old_six_thousand_character_cap(task_page):
    page, _, _, errors = task_page
    await page.locator("#new-chat").click()
    text = "本章需要覆盖业务流程、失败处理与用户确认。\n" * 400
    assert len(text) > 6000
    await page.locator("#message-input").fill(text)
    await expect(page.locator("#message-input")).to_have_value(text)
    await page.locator("#message-input").press("Enter")
    await page.wait_for_function("streams.some(item => item.url.endsWith('/messages'))")
    sent = await page.evaluate("JSON.parse(streams.find(item => item.url.endsWith('/messages')).options.body)")
    assert sent["text"] == text.strip()
    assert errors == []


async def test_model_settings_retains_keys_only_at_saved_endpoint_and_clears_secrets(task_page):
    page, _, _, errors = task_page
    requests = await model_routes(page)
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-base-url")).to_have_value(MODEL_CONFIG["base_url"])
    await expect(page.locator("#model-api-key")).to_have_value("")
    await expect(page.locator("#saved-key-label")).to_contain_text("留空保持不变")
    await expect(page.locator("#model-test-status")).to_have_text("测试通过")

    # A blank key at the original endpoint is an explicit retain operation.
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    assert requests[-1][0] == "PUT"
    assert requests[-1][2]["api_key"] is None
    assert requests[-1][2]["enable_thinking"] is False
    assert requests[-1][2]["structured_output"] == "json_schema"

    # Changing the endpoint cannot reuse a secret belonging to another service.
    await page.locator("#model-base-url").fill("https://another.example.test/v1")
    await expect(page.locator("#saved-key-label")).to_be_empty()
    await expect(page.locator("#model-test-status")).to_contain_text("重新测试")
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-test-status")).to_contain_text("请填写此服务的 API Key")
    assert len([request for request in requests if request[0] == "PUT"]) == 1

    secrets = ["llm-local-secret", "jev-local-secret", "tavily-local-secret"]
    for field, secret in zip(["model-api-key", "decision-api-key", "tavily-api-key"], secrets, strict=True):
        await page.locator(f"#{field}").fill(secret)
    browser_storage = await page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
    assert all(secret not in browser_storage for secret in secrets)
    await page.keyboard.press("Escape")
    await expect(page.locator("#model-settings-dialog")).to_be_hidden()
    for field in ["model-api-key", "decision-api-key", "tavily-api-key"]:
        await expect(page.locator(f"#{field}")).to_have_value("")
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-base-url")).to_have_value(MODEL_CONFIG["base_url"])
    assert errors == []


async def test_optional_services_require_current_tests_and_provider_fields_do_not_block_save(task_page):
    page, _, _, errors = task_page
    requests = await model_routes(page)
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#test-decision-button")).to_be_enabled()
    await expect(page.locator("#decision-enabled")).to_be_disabled()
    await expect(page.locator("#tavily-enabled")).to_be_disabled()
    await page.locator("#decision-api-key").fill("first-jev-secret")
    await page.locator("#test-decision-button").click()
    await expect(page.locator("#decision-enabled")).to_be_enabled()
    await page.locator("#decision-enabled").check()
    await page.locator("#decision-api-key").fill("replacement-jev-secret")
    await expect(page.locator("#decision-enabled")).to_be_disabled()
    await expect(page.locator("#decision-enabled")).not_to_be_checked()

    await page.locator("#decision-base-url").fill("https://judge.test/v1")
    await page.locator("#decision-model").fill("judge")
    await page.locator("#decision-api-key").fill("judge-secret")
    await page.locator("#test-decision-button").click()
    await expect(page.locator("#decision-enabled")).to_be_enabled()
    await page.locator("#decision-enabled").check()
    await page.locator("#decision-base-url").fill("https://router.example.test/v1")
    await expect(page.locator("#decision-enabled")).not_to_be_checked()
    await expect(page.locator("#decision-enabled")).to_be_disabled()
    await page.locator("#test-decision-button").click()
    await expect(page.locator("#decision-enabled")).to_be_enabled()
    await page.locator("#decision-enabled").check()

    await page.locator("#tavily-api-key").fill("tavily-service-secret")
    await page.locator("#test-tavily-button").click()
    await expect(page.locator("#tavily-enabled")).to_be_enabled()
    await page.locator("#tavily-enabled").check()
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    body = requests[-1][2]
    assert requests[-1][0] == "PUT"
    assert body["provider_mode"] == "custom"
    assert body["api_key"] is None
    assert body["decision_api_key"] == "judge-secret"
    assert body["decision_enabled"] and body["tavily_enabled"]
    assert body["enable_thinking"] is False and body["structured_output"] == "json_schema"
    await expect(page.locator("#decision-api-key")).to_have_value("")
    await expect(page.locator("#tavily-api-key")).to_have_value("")
    browser_storage = await page.evaluate("JSON.stringify({...localStorage})")
    assert "router-secret" not in browser_storage and "tavily-service-secret" not in browser_storage
    assert errors == []


async def test_source_upload_and_removal_persist_without_role_or_draft_file_writes(task_page):
    page, calls, states, errors = task_page
    await page.locator("#new-chat").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.locator("#file-input").set_input_files([
        {"name": "project-notes.md", "mimeType": "text/markdown", "buffer": b"# Project\n## Context\n"},
        {"name": "meeting.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "buffer": b"synthetic document upload"},
    ])
    await expect(page.locator("#material-list .status-saved")).to_have_count(2)
    await expect(page.locator("#file-count")).to_have_text("2")
    await expect(page.locator("#staged-files select")).to_have_count(0)
    assert {item["name"] for item in states["new"]["files"]} == {"project-notes.md", "meeting.docx"}
    assert all(item["role"] == "source" for item in states["new"]["files"])
    source = next(item for item in states["new"]["files"] if item["name"] == "project-notes.md")
    async with page.expect_response(f"**/conversations/new/sources/{source['id']}"):
        await page.locator("#materials-panel").get_by_role("button", name="移除 project-notes.md", exact=True).click()
    await expect(page.locator("#file-count")).to_have_text("1")
    assert [item["name"] for item in states["new"]["files"]] == ["meeting.docx"]
    await page.locator("#close-materials").click()
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await expect(page.locator("#messages")).to_contain_text("任务 A")
    await page.locator("#session-list button").filter(has_text="新会话").click()
    await expect(page.locator("#file-count")).to_have_text("1")
    await expect(page.locator("#staged-files")).to_contain_text("meeting.docx")
    source_id = states["new"]["files"][0]["id"]
    async with page.expect_response(f"**/conversations/new/sources/{source_id}"):
        await page.locator("#staged-files").get_by_role("button", name="移除 meeting.docx", exact=True).click()
    await expect(page.locator("#file-count")).to_have_text("0")
    assert states["new"]["files"] == []
    assert len([1 for method, path, _ in calls if method == "POST" and path == "/conversations/new/sources"]) == 2
    assert len([1 for method, path, _ in calls if method == "DELETE" and path.startswith("/conversations/new/sources/")]) == 2
    assert all("files" not in json.loads(body) for method, path, body in calls if method == "PUT" and path.endswith("/draft"))
    assert not any(path == "/uploads" for _, path, _ in calls)
    assert errors == []


async def test_bulk_sources_use_bounded_uploads_and_material_counts_only_saved_files(task_page):
    page, _, states, errors = task_page
    release = asyncio.Event()
    three_started = asyncio.Event()
    requested = []
    active = 0
    peak = 0

    async def upload(route):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        requested.append(route.request.url)
        if len(requested) == 3:
            three_started.set()
        try:
            await release.wait()
            await route.fallback()
        finally:
            active -= 1

    await page.route("**/conversations/new/sources?*", upload)
    await page.locator("#new-chat").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    try:
        await page.locator("#file-input").set_input_files([
            {"name": f"source-{index:02}.md", "mimeType": "text/markdown", "buffer": f"# Source {index}".encode()}
            for index in range(12)
        ])
        await asyncio.wait_for(three_started.wait(), timeout=5)
        await expect(page.locator("#file-count")).to_have_text("12")
        await expect(page.locator("#material-list .status-uploading")).to_have_count(3)
        await expect(page.locator("#material-list .status-queued")).to_have_count(9)
        await expect(page.locator("#material-list .status-saved")).to_have_count(0)
        await expect(page.locator("#material-list .is-uploading .material-remove").first).to_be_disabled()
        assert len(requested) == 3
        await expect(page.locator(".source-stats > div strong")).to_have_text(["0", "12", "0"])
        await expect(page.locator(".source-stats > div").nth(1)).to_contain_text("3 上传中 · 9 排队")
    finally:
        release.set()
    await expect(page.locator("#material-list .status-saved")).to_have_count(12)
    assert len(requested) == 12 and peak == 3
    assert len(states["new"]["files"]) == 12
    await expect(page.locator(".source-stats > div strong")).to_have_text(["12", "0", "0"])
    await expect(page.locator("#knowledge-open, #knowledge-dialog")).to_have_count(0)
    assert errors == []


async def test_sources_search_pagination_and_last_page_removal(task_page):
    page, _, states, errors = task_page
    states["b"]["files"] = [
        {"id": f"source-{index}", "name": f"资料-{index:03}.md", "size": 100 + index, "role": "source"}
        for index in range(1, 52)
    ]
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await expect(page.locator("#file-count")).to_have_text("51")
    await page.locator("#materials-toggle").click()
    await expect(page.locator("#material-list .source-file-row")).to_have_count(50)
    await expect(page.locator(".source-pagination")).to_contain_text("1–50 / 51")
    await page.get_by_role("button", name="下一页资料", exact=True).click()
    await expect(page.locator("#material-list .source-file-row")).to_have_count(1)
    await expect(page.locator(".source-pagination")).to_contain_text("51–51 / 51")
    await page.locator("#material-search").fill("001")
    await expect(page.locator("#material-list .source-file-row")).to_have_count(1)
    await expect(page.locator("#material-list")).to_contain_text("资料-001.md")
    await expect(page.locator(".source-pagination")).to_contain_text("1–1 / 1")
    await page.locator("#material-status-filter").select_option("failed")
    await expect(page.locator("#material-list")).to_contain_text("没有匹配的资料")
    await page.get_by_role("button", name="清除筛选", exact=True).click()
    await expect(page.locator("#material-list .source-file-row")).to_have_count(50)
    await page.get_by_role("button", name="下一页资料", exact=True).click()
    await page.locator("#materials-panel").get_by_role("button", name="移除 资料-051.md", exact=True).click()
    await expect(page.locator("#file-count")).to_have_text("50")
    await expect(page.locator("#material-list .source-file-row")).to_have_count(50)
    await expect(page.locator(".source-pagination")).to_contain_text("1–50 / 50")
    await expect(page.get_by_role("button", name="下一页资料", exact=True)).to_have_count(0)
    assert len(states["b"]["files"]) == 50
    assert errors == []


async def test_source_retry_and_failed_removal_preserve_saved_material(task_page):
    page, calls, states, errors = task_page
    attempts = 0

    async def upload(route):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await route.fulfill(status=503, json={"detail": "暂时无法保存资料，请重试。"})
        else:
            await route.fallback()

    await page.route("**/conversations/new/sources?*", upload)
    await page.locator("#new-chat").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.locator("#file-input").set_input_files({"name": "meeting.txt", "mimeType": "text/plain", "buffer": b"Meeting notes"})
    await expect(page.locator("#material-list .status-failed")).to_have_count(1)
    await expect(page.locator("#material-list .source-file-error")).to_contain_text("暂时无法保存资料")
    await expect(page.locator(".source-stats > div strong")).to_have_text(["0", "0", "1"])
    assert states["new"]["files"] == []
    await page.get_by_role("button", name="重试上传 meeting.txt", exact=True).click()
    await expect(page.locator("#material-list .status-saved")).to_have_count(1)
    await expect(page.get_by_role("button", name="重试上传 meeting.txt", exact=True)).to_have_count(0)
    assert attempts == 2 and len(states["new"]["files"]) == 1
    source_id = states["new"]["files"][0]["id"]
    failed_delete = f"**/conversations/new/sources/{source_id}"
    await page.route(failed_delete, lambda route: route.fulfill(status=503, json={"detail": "保存服务暂时不可用"}))
    await page.locator("#materials-panel").get_by_role("button", name="移除 meeting.txt", exact=True).click()
    await expect(page.locator("#toast")).to_contain_text("无法移除资料")
    await expect(page.locator("#file-count")).to_have_text("1")
    await expect(page.locator("#material-list .status-saved")).to_have_count(1)
    assert len(states["new"]["files"]) == 1
    await page.unroute(failed_delete)
    await expect(page.locator(".source-stats > div strong")).to_have_text(["1", "0", "0"])
    await page.keyboard.press("Escape")
    await expect(page.locator("#materials-panel")).to_be_hidden()
    assert not any(path.startswith("/knowledge/") for _, path, _ in calls)
    assert errors == []


async def test_invalid_materials_are_rejected_before_upload(task_page):
    page, calls, _, errors = task_page
    await page.locator("#new-chat").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    await page.locator("#file-input").set_input_files({"name": "program.exe", "mimeType": "application/octet-stream", "buffer": b"not a document"})
    await expect(page.locator("#toast")).to_contain_text("请使用 Word、Excel、PDF、PPTX、文本")
    await page.locator("#file-input").set_input_files({"name": "empty.txt", "mimeType": "text/plain", "buffer": b""})
    await expect(page.locator("#toast")).to_contain_text("文件不能为空")
    await expect(page.locator("#file-count")).to_have_text("0")
    assert not any(path == "/uploads" or path.endswith("/sources") for _, path, _ in calls)
    assert errors == []


async def test_large_material_is_uploaded_without_size_rejection(task_page):
    page, calls, _, errors = task_page
    await page.locator("#new-chat").click()
    await expect(page.locator("#message-input")).to_be_enabled()
    # Exercise a File above the removed byte limit without transferring a fixture.
    await page.evaluate("""() => {
      const input = document.querySelector('#file-input');
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(20 * 1024 * 1024 + 1)], 'large.pdf', {type: 'application/pdf'}));
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    await expect(page.locator("#file-count")).to_have_text("1")
    await expect(page.locator("#materials-panel")).to_contain_text("large.pdf")
    await expect(page.locator("#materials-panel")).not_to_contain_text("20 MB")
    # Playwright relays this actual large body to the mock server over CDP.
    await expect(page.locator("#material-list .status-saved")).to_have_count(1, timeout=30000)
    await expect(page.locator("#message-input")).to_be_enabled()
    assert sum(method == "POST" and path.endswith("/sources") for method, path, _ in calls) == 1
    assert errors == []


async def test_template_purpose_replacement_is_explicit_and_survives_restore(task_page):
    page, calls, states, errors = task_page
    states["b"]["files"] = [
        {"id": "template-one", "name": "公司模板.docx", "size": 100, "role": "source"},
        {"id": "template-two", "name": "新版模板.docx", "size": 120, "role": "source"},
        {"id": "facts", "name": "业务资料.md", "size": 90, "role": "source"},
    ]
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#materials-toggle").click()
    first = page.get_by_label("公司模板.docx 文件用途", exact=True)
    second = page.get_by_label("新版模板.docx 文件用途", exact=True)
    await expect(page.locator("#material-list select")).to_have_count(3)
    await first.select_option("template")
    await expect(first).to_have_value("template")
    await second.select_option("template")
    await expect(page.locator("#toast")).to_contain_text("先移除原模板")
    await expect(second).to_have_value("source")
    await expect(first).to_have_value("template")
    await first.select_option("source")
    await expect(first).to_have_value("source")
    await second.select_option("template")
    await expect(second).to_have_value("template")
    await expect(first).to_have_value("source")
    assert [item["role"] for item in states["b"]["files"]] == ["source", "template", "source"]
    await page.locator("#close-materials").click()
    await page.locator("#message-input").fill("按照指定模板生成文档")
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#materials-toggle").click()
    await expect(second).to_have_value("template")
    await expect(first).to_have_value("source")
    mutations = [(method, path, json.loads(body)) for method, path, body in calls if method == "PATCH"]
    assert mutations == [
        ("PATCH", "/conversations/b/sources/template-one", {"role": "template"}),
        ("PATCH", "/conversations/b/sources/template-two", {"role": "template"}),
        ("PATCH", "/conversations/b/sources/template-one", {"role": "source"}),
        ("PATCH", "/conversations/b/sources/template-two", {"role": "template"}),
    ]
    assert all("files" not in json.loads(body) for method, path, body in calls if method == "PUT" and path.endswith("/draft"))
    assert errors == []


async def test_failed_template_purpose_keeps_saved_selection(task_page):
    page, _, states, errors = task_page
    states["b"]["files"] = [{"id": "template", "name": "公司模板.docx", "size": 100, "role": "source"}]
    await page.route("**/conversations/b/sources/template", lambda route: route.fulfill(status=503, json={"detail": "保存服务暂时不可用"}))
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#materials-toggle").click()
    selector = page.get_by_label("公司模板.docx 文件用途", exact=True)
    await selector.select_option("template")
    await expect(page.locator("#toast")).to_contain_text("文件用途未保存")
    await expect(selector).to_have_value("source")
    await expect(selector).to_be_enabled()
    assert states["b"]["files"][0]["role"] == "source"
    assert errors == []


async def test_style_reference_selection_supports_text_and_survives_restore(task_page):
    page, calls, states, errors = task_page
    states["b"]["files"] = [
        {"id": "style", "name": "示例.md", "size": 100, "role": "source"},
    ]
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#materials-toggle").click()
    selector = page.get_by_label("示例.md 文件用途", exact=True)
    await expect(selector.locator('option[value="template"]')).to_have_count(0)
    await selector.select_option("style_reference")
    await expect(selector).to_have_value("style_reference")
    await expect(page.locator("#toast")).to_contain_text("不作为业务事实")
    await page.locator("#close-materials").click()
    await page.locator("#session-list button").filter(has_text="任务 A").click()
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#materials-toggle").click()
    await expect(selector).to_have_value("style_reference")
    assert ("PATCH", "/conversations/b/sources/style", {"role": "style_reference"}) in [
        (method, path, json.loads(body)) for method, path, body in calls if method == "PATCH"
    ]
    assert errors == []
