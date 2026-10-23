"""Exercise session menus and persistence without invoking any model."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, expect

WEB = Path(__file__).parents[1] / "src/folio/web/dist"


@pytest.fixture
async def deletion_page(request):
    assert (WEB / "index.html").is_file(), "Run npm --prefix frontend run build"
    async with async_playwright() as playwright:
        executable = os.environ.get("FOLIO_CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install local Chromium for browser integration tests")
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900}, **getattr(request, "param", {}))
        states = {
            "a": {"id": "a", "title": "研究报告 A", "pinned": False, "updated_at": "2026-10-20T10:00:00+08:00", "messages": [{"role": "user", "content": "研究报告 A"}], "files": []},
            "b": {"id": "b", "title": "需求说明 B", "pinned": False, "updated_at": "2026-10-19T10:00:00+08:00", "messages": [{"role": "user", "content": "需求说明 B"}], "files": []},
        }
        controls = {"draft_status": 200, "delete_status": 200, "metadata_status": 200, "metadata_patches": []}
        calls, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def serve(route):
            request = route.request
            location = urlsplit(request.url)
            assert location.netloc == "folio.test"
            path, method = location.path, request.method
            calls.append((method, path + (f"?{location.query}" if location.query else "")))
            if path == "/" or path.startswith("/ui/"):
                source = WEB / ("index.html" if path == "/" else path.removeprefix("/ui/"))
                if source.is_file():
                    await route.fulfill(body=source.read_bytes(), content_type=mimetypes.guess_type(source)[0] or "application/octet-stream")
                else:
                    await route.fulfill(status=404, body="")
                return
            if path == "/conversations" and method == "GET":
                history = sorted(states.values(), key=lambda item: (item["pinned"], item["updated_at"]), reverse=True)
                await route.fulfill(json={"conversations": [{key: item[key] for key in ["id", "title", "pinned", "updated_at"]} for item in history], "has_more": False})
                return
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "conversations":
                conversation_id = parts[1]
                if len(parts) == 2 and method == "DELETE":
                    status = controls["delete_status"]
                    if status in {200, 404}:
                        states.pop(conversation_id, None)
                    body = {"deleted": conversation_id} if status == 200 else {"detail": "会话正在处理，请先停止任务再删除。" if status == 409 else "会话不存在"}
                    await route.fulfill(status=status, json=body)
                    return
                if conversation_id not in states:
                    await route.fulfill(status=404, json={"detail": "会话不存在"})
                    return
                if len(parts) == 2 and method == "PATCH":
                    patch = json.loads(request.post_data)
                    controls["metadata_patches"].append((conversation_id, patch))
                    if controls["metadata_status"] != 200:
                        await route.fulfill(status=controls["metadata_status"], json={"detail": "无法保存会话设置，请稍后重试。"})
                        return
                    assert set(patch) <= {"title", "pinned"}
                    states[conversation_id].update(patch)
                    await route.fulfill(json={key: states[conversation_id][key] for key in ["id", "title", "pinned", "updated_at"]})
                    return
                if len(parts) == 2 and method == "GET":
                    await route.fulfill(json=states[conversation_id])
                    return
                if len(parts) == 3 and parts[2] == "draft" and method == "PUT":
                    if controls["draft_status"] != 200:
                        await route.fulfill(status=controls["draft_status"], json={"detail": "草稿暂时无法保存"})
                        return
                    states[conversation_id]["draft"] = json.loads(request.post_data)["text"]
                    await route.fulfill(json={"saved": True})
                    return
            await route.fulfill(json={})

        await page.route("**/*", serve)
        await page.add_init_script("""
            if (!localStorage.getItem('test.initialized')) {
              localStorage.setItem('folio.conversation', 'a');
              localStorage.setItem('test.initialized', 'true');
            }
        """)
        await page.goto("http://folio.test/")
        await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
        await expect(page.locator("#message-input")).to_be_enabled()
        try:
            yield page, states, controls, calls, errors
        finally:
            await browser.close()


def session(page, title):
    return page.locator("#session-list .session-row").filter(has_text=title)


async def open_session_menu(page, title):
    row = session(page, title)
    await row.hover()
    await row.locator(".session-more").click()
    await expect(page.locator(".session-menu")).to_be_visible()


async def request_delete(page, title, *, files=False):
    await open_session_menu(page, title)
    await page.locator(".session-menu").get_by_role("menuitem", name="删除对话和文件" if files else "仅删除对话", exact=True).click()
    await expect(page.locator("#delete-conversation-dialog")).to_be_visible()


async def confirm_delete(page):
    await page.locator("#confirm-delete-conversation").click()
    await expect(page.locator("#delete-conversation-dialog")).to_be_hidden()


async def test_cancel_delete_does_not_switch_or_delete_a_conversation(deletion_page):
    page, states, _, calls, errors = deletion_page
    await page.locator("#message-input").fill("当前会话的未发送内容")
    await expect(page.locator("#session-list .session-more")).to_have_count(2)
    await request_delete(page, "需求说明 B")
    await expect(page.locator("#delete-conversation-dialog")).to_contain_text("需求说明 B")
    await page.locator("#cancel-delete-conversation").click()
    await expect(page.locator("#delete-conversation-dialog")).to_be_hidden()
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    await expect(page.locator("#message-input")).to_have_value("当前会话的未发送内容")
    assert set(states) == {"a", "b"}
    assert not any(method == "DELETE" for method, _ in calls)
    assert ("GET", "/conversations/b") not in calls
    assert errors == []


async def test_delete_other_conversation_preserves_unsynced_current_draft(deletion_page):
    page, states, controls, _, errors = deletion_page
    controls["draft_status"] = 503
    draft = "保存失败时也必须保留的本地草稿"
    await page.locator("#message-input").fill(draft)
    await expect(page.locator("#session-save-status")).to_contain_text("保存失败")
    await request_delete(page, "需求说明 B")
    await confirm_delete(page)
    await expect(session(page, "需求说明 B")).to_have_count(0)
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    await expect(page.locator("#message-input")).to_have_value(draft)
    assert set(states) == {"a"}
    assert await page.evaluate("JSON.parse(localStorage.getItem('folio.draft.a')).text") == draft
    await page.reload()
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    await expect(page.locator("#message-input")).to_have_value(draft)
    await expect(session(page, "需求说明 B")).to_have_count(0)
    assert errors == []


async def test_delete_current_conversation_clears_its_cache_and_opens_empty_document(deletion_page):
    page, states, controls, _, errors = deletion_page
    controls["draft_status"] = 503
    await page.locator("#message-input").fill("这份未发送草稿随会话删除")
    await expect(page.locator("#session-save-status")).to_contain_text("保存失败")
    await page.evaluate("""() => {
      localStorage.setItem('folio.confirmation.a.job-one', '{"answers":{"one":"删除"}}');
      localStorage.setItem('folio.confirmation.a.job-two', '{"answers":{"two":"删除"}}');
      localStorage.setItem('folio.confirmation.ab.job-three', '{"answers":{"three":"保留"}}');
      localStorage.setItem('folio.draft.b', '{"text":"B 的草稿保留"}');
    }""")
    await request_delete(page, "研究报告 A")
    await confirm_delete(page)
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_have_value("")
    await expect(session(page, "研究报告 A")).to_have_count(0)
    assert set(states) == {"b"}
    saved = await page.evaluate("({...localStorage})")
    assert "folio.draft.a" not in saved
    assert "folio.confirmation.a.job-one" not in saved
    assert "folio.confirmation.a.job-two" not in saved
    assert "folio.confirmation.ab.job-three" in saved
    assert json.loads(saved["folio.draft.b"])["text"] == "B 的草稿保留"
    await page.reload()
    await expect(page.locator("#welcome")).to_be_visible()
    await expect(page.locator("#message-input")).to_have_value("")
    await expect(session(page, "研究报告 A")).to_have_count(0)
    assert errors == []


async def test_delete_last_conversation_keeps_an_empty_persistent_history(deletion_page):
    page, states, _, _, errors = deletion_page
    await request_delete(page, "需求说明 B")
    await confirm_delete(page)
    await request_delete(page, "研究报告 A")
    await confirm_delete(page)
    await expect(page.locator("#session-list .history-empty")).to_contain_text("暂无会话")
    await expect(page.locator("#session-list .session-row")).to_have_count(0)
    await expect(page.locator("#welcome")).to_be_visible()
    assert states == {}
    await page.reload()
    await expect(page.locator("#session-list .history-empty")).to_contain_text("暂无会话")
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []


async def test_busy_conversation_delete_is_rejected_without_losing_the_session(deletion_page):
    page, states, controls, calls, errors = deletion_page
    controls["delete_status"] = 409
    await page.locator("#message-input").fill("仍需保留的草稿")
    await request_delete(page, "研究报告 A")
    await page.locator("#confirm-delete-conversation").click()
    await expect(page.locator("#delete-conversation-error")).to_contain_text("请先停止任务再删除")
    await expect(page.locator("#delete-conversation-dialog")).to_be_visible()
    await expect(page.locator("#confirm-delete-conversation")).to_be_enabled()
    await expect(session(page, "研究报告 A")).to_have_count(1)
    await expect(page.locator("#message-input")).to_have_value("仍需保留的草稿")
    assert set(states) == {"a", "b"}
    assert calls.count(("DELETE", "/conversations/a")) == 1
    await page.locator("#cancel-delete-conversation").click()
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    assert errors == []


async def test_conversation_deleted_in_another_tab_is_removed_locally(deletion_page):
    page, states, controls, _, errors = deletion_page
    controls["delete_status"] = 404
    await request_delete(page, "研究报告 A")
    await confirm_delete(page)
    await expect(session(page, "研究报告 A")).to_have_count(0)
    await expect(page.locator("#welcome")).to_be_visible()
    assert set(states) == {"b"}
    assert errors == []


@pytest.mark.parametrize("deletion_page", [{"has_touch": True}], indirect=True)
async def test_session_delete_control_works_on_mobile_and_escape_cancels(deletion_page):
    page, states, _, calls, errors = deletion_page
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.locator("#menu-button").click()
    button = session(page, "需求说明 B").locator(".session-more")
    await expect(button).to_be_visible()
    await expect(button).to_have_css("opacity", "1")
    await button.tap()
    await page.locator(".session-menu").get_by_role("menuitem", name="仅删除对话", exact=True).tap()
    await expect(page.locator("#delete-conversation-dialog")).to_be_visible()
    await page.keyboard.press("Escape")
    await expect(page.locator("#delete-conversation-dialog")).to_be_hidden()
    assert set(states) == {"a", "b"}
    assert not any(method == "DELETE" for method, _ in calls)
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


async def test_session_actions_appear_on_hover_and_menu_has_four_options(deletion_page):
    page, _, _, _, errors = deletion_page
    row = session(page, "需求说明 B")
    await page.mouse.move(1100, 20)
    await expect(row.locator(".session-more")).to_have_css("opacity", "0")
    await expect(row.locator(".session-pin")).to_have_css("opacity", "0")
    await row.hover()
    await expect(row.locator(".session-more")).to_have_css("opacity", "1")
    await expect(row.locator(".session-pin")).to_have_css("opacity", "1")
    await row.locator(".session-more").click()
    await expect(page.locator(".session-menu").get_by_role("menuitem")).to_have_text(["重命名", "置顶", "仅删除对话", "删除对话和文件"])
    await page.keyboard.press("Escape")
    await expect(page.locator(".session-menu")).to_be_hidden()
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    assert errors == []


@pytest.mark.parametrize("current", [True, False])
async def test_session_rename_persists_without_rewriting_messages(deletion_page, current):
    page, states, controls, _, errors = deletion_page
    original = "研究报告 A" if current else "需求说明 B"
    renamed = "行业调研 · 终稿" if current else "采购需求 · 已整理"
    conversation_id = "a" if current else "b"
    await open_session_menu(page, original)
    await page.locator(".session-menu").get_by_role("menuitem", name="重命名", exact=True).click()
    await expect(page.locator("#rename-conversation-dialog")).to_be_visible()
    await expect(page.locator("#rename-conversation-input")).to_have_value(original)
    await page.locator("#rename-conversation-input").fill(renamed)
    await page.locator("#confirm-rename-conversation").click()
    await expect(page.locator("#rename-conversation-dialog")).to_be_hidden()
    await expect(session(page, renamed)).to_have_count(1)
    await expect(page.locator("#conversation-title")).to_have_text(renamed if current else "研究报告 A")
    await expect(page.locator("#messages")).to_contain_text("研究报告 A")
    assert states[conversation_id]["messages"] == [{"role": "user", "content": original}]
    assert controls["metadata_patches"] == [(conversation_id, {"title": renamed})]
    await page.reload()
    await expect(session(page, renamed)).to_have_count(1)
    await session(page, renamed).locator(".session-item").click()
    await expect(page.locator("#conversation-title")).to_have_text(renamed)
    await expect(page.locator("#messages")).to_contain_text(original)
    assert errors == []


async def test_session_pin_and_unpin_persist_and_restore_history_order(deletion_page):
    page, states, controls, _, errors = deletion_page
    titles = page.locator("#session-list .session-title")
    await expect(titles).to_have_text(["研究报告 A", "需求说明 B"])
    await session(page, "需求说明 B").hover()
    await session(page, "需求说明 B").locator(".session-pin").click()
    await expect(titles).to_have_text(["需求说明 B", "研究报告 A"])
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    assert states["b"]["pinned"] is True
    await page.reload()
    await expect(titles).to_have_text(["需求说明 B", "研究报告 A"])
    await open_session_menu(page, "需求说明 B")
    await expect(page.locator(".session-menu").get_by_role("menuitem")).to_have_text(["重命名", "取消置顶", "仅删除对话", "删除对话和文件"])
    await page.locator(".session-menu").get_by_role("menuitem", name="取消置顶", exact=True).click()
    await expect(titles).to_have_text(["研究报告 A", "需求说明 B"])
    assert states["b"]["pinned"] is False
    assert controls["metadata_patches"] == [("b", {"pinned": True}), ("b", {"pinned": False})]
    await page.reload()
    await expect(titles).to_have_text(["研究报告 A", "需求说明 B"])
    assert errors == []


async def test_delete_modes_explain_file_retention_and_send_distinct_requests(deletion_page):
    page, states, _, calls, errors = deletion_page
    await request_delete(page, "需求说明 B")
    await expect(page.locator("#delete-conversation-description")).to_contain_text("保留")
    await confirm_delete(page)
    assert ("DELETE", "/conversations/b") in calls
    await request_delete(page, "研究报告 A", files=True)
    await expect(page.locator("#delete-conversation-title")).to_have_text("删除对话和文件？")
    await expect(page.locator("#delete-conversation-description")).to_contain_text("删除这段对话，以及仅属于它的上传文件和生成文档")
    await expect(page.locator("#delete-conversation-description")).to_contain_text(re.compile(r"其他(?:会话|对话).*文件.*会保留"))
    await confirm_delete(page)
    assert ("DELETE", "/conversations/a?delete_files=true") in calls
    assert states == {}
    assert errors == []


async def test_failed_rename_keeps_existing_title_and_entered_name(deletion_page):
    page, states, controls, _, errors = deletion_page
    controls["metadata_status"] = 503
    await open_session_menu(page, "研究报告 A")
    await page.locator(".session-menu").get_by_role("menuitem", name="重命名", exact=True).click()
    await page.locator("#rename-conversation-input").fill("尚未保存的新名称")
    await page.locator("#confirm-rename-conversation").click()
    await expect(page.locator("#rename-conversation-dialog")).to_contain_text("无法保存会话设置")
    await expect(page.locator("#rename-conversation-input")).to_have_value("尚未保存的新名称")
    await expect(page.locator("#conversation-title")).to_have_text("研究报告 A")
    await expect(session(page, "研究报告 A")).to_have_count(1)
    assert states["a"]["title"] == "研究报告 A"
    await page.locator("#cancel-rename-conversation").click()
    await expect(page.locator("#rename-conversation-dialog")).to_be_hidden()
    assert errors == []
