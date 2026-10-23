"""Exercise the preview's two resize axes with real browser layout and scrolling."""

from urllib.parse import urlsplit

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


async def show_long_preview(task_page, *, width=1600, height=960):
    page, _, states, _ = task_page
    states["a"]["directory_review"] = {
        "phase": "directory_review",
        "revision": "resizable-directory",
        "title": "详细需求规格说明书",
        "reason": "请确认完整目录。",
        "directory": {
            "sections": [
                {"number": str(index), "title": f"业务模块 {index} 的需求与验收标准", "level": 1}
                for index in range(1, 81)
            ],
            "preview": {
                "id": "resizable-directory",
                "name": "详细需求规格说明书",
                "download_url": "/previews/resizable-directory/download",
            },
        },
    }

    async def preview(route):
        path = urlsplit(route.request.url).path
        if "/pages/" in path:
            number = path.rsplit("/", 1)[-1]
            await route.fulfill(
                content_type="image/svg+xml",
                body=(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="840">'
                    '<rect width="600" height="840" fill="white"/>'
                    f'<text x="30" y="50">Preview page {number}</text></svg>'
                ),
            )
        elif path.endswith("/pages"):
            await route.fulfill(json={"pages": [f"{path}/{index}" for index in range(1, 6)]})
        else:
            await route.fulfill(json={"blocks": [{"kind": "heading", "level": 1, "text": "业务需求"}]})

    await page.route("**/previews/**", preview)
    await page.set_viewport_size({"width": width, "height": height})
    await page.reload()
    await expect(page.locator("#directory-toc li")).to_have_count(80)
    if width <= 1150:
        await page.get_by_role("button", name="打开文档预览").click()
    await expect(page.locator("#preview-directory-pane")).to_be_visible()
    await expect(page.locator("#preview-page-pane")).to_be_visible()
    await expect(page.locator("#preview-pages img")).to_have_count(5)
    await expect(page.locator("#preview-pages img").first).to_be_visible()
    await page.wait_for_function("document.querySelector('#preview-pages img').naturalWidth > 0")
    # Mobile previews slide in; a raw mouse drag must use the settled coordinates.
    await page.locator("#preview-split-handle").click(trial=True)
    return page


async def drag_separator(page, selector, *, dx=0, dy=0):
    await page.locator(selector).click(trial=True)
    box = await page.locator(selector).bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + dx, y + dy, steps=8)
    await page.mouse.up()


async def test_preview_width_and_pane_split_drag_and_restore(task_page):
    page = await show_long_preview(task_page)
    sidebar = await page.locator("#sidebar").bounding_box()
    initial = await page.locator("#document-preview").bounding_box()
    await drag_separator(page, "#preview-resize-handle", dx=-120)
    resized = await page.locator("#document-preview").bounding_box()
    assert resized["width"] == pytest.approx(initial["width"] + 120, abs=2)
    assert (await page.locator("#sidebar").bounding_box())["width"] == sidebar["width"]
    assert (await page.locator(".workspace").bounding_box())["width"] >= 420

    upper = await page.locator("#preview-directory-pane").bounding_box()
    lower = await page.locator("#preview-page-pane").bounding_box()
    await drag_separator(page, "#preview-split-handle", dy=90)
    upper_resized = await page.locator("#preview-directory-pane").bounding_box()
    lower_resized = await page.locator("#preview-page-pane").bounding_box()
    assert upper_resized["height"] == pytest.approx(upper["height"] + 90, abs=3)
    assert lower_resized["height"] == pytest.approx(lower["height"] - 90, abs=3)
    assert await page.evaluate("localStorage.getItem('folio.previewWidth') !== null")
    assert await page.evaluate("localStorage.getItem('folio.previewSplit') !== null")

    await page.reload()
    await expect(page.locator("#preview-pages img").first).to_be_visible()
    restored = await page.locator("#document-preview").bounding_box()
    restored_upper = await page.locator("#preview-directory-pane").bounding_box()
    assert restored["width"] == pytest.approx(resized["width"], abs=2)
    assert restored_upper["height"] == pytest.approx(upper_resized["height"], abs=3)
    assert task_page[3] == []


async def test_long_directory_and_pages_scroll_independently(task_page):
    page = await show_long_preview(task_page)
    directory = page.locator(".preview-directory-scroll")
    pages = page.locator(".preview-page-scroll")
    upper = await page.locator("#preview-directory-pane").bounding_box()
    lower = await page.locator("#preview-page-pane").bounding_box()
    assert upper["y"] + upper["height"] <= lower["y"]
    assert upper["height"] > 100 and lower["height"] > 100
    assert await directory.evaluate("element => element.scrollHeight > element.clientHeight")
    assert await pages.evaluate("element => element.scrollHeight > element.clientHeight")

    await directory.hover()
    await page.mouse.wheel(0, 400)
    await page.wait_for_function("document.querySelector('.preview-directory-scroll').scrollTop >= 350")
    directory_top = await directory.evaluate("element => element.scrollTop")
    assert await pages.evaluate("element => element.scrollTop") == 0

    await pages.hover()
    await page.mouse.wheel(0, 400)
    await page.wait_for_function("document.querySelector('.preview-page-scroll').scrollTop >= 350")
    assert await directory.evaluate("element => element.scrollTop") == directory_top
    assert (await page.locator("#preview-directory-pane").bounding_box())["y"] == upper["y"]
    assert (await page.locator("#preview-page-pane").bounding_box())["y"] == lower["y"]
    assert task_page[3] == []


async def test_preview_keyboard_bounds_and_sidebar_remain_usable(task_page):
    page = await show_long_preview(task_page)
    width_handle = page.locator("#preview-resize-handle")
    split_handle = page.locator("#preview-split-handle")
    await expect(width_handle).to_have_attribute("role", "separator")
    await expect(width_handle).to_have_attribute("aria-orientation", "vertical")
    await expect(split_handle).to_have_attribute("role", "separator")
    await expect(split_handle).to_have_attribute("aria-orientation", "horizontal")

    await width_handle.focus()
    await page.keyboard.press("Home")
    assert (await page.locator("#document-preview").bounding_box())["width"] == pytest.approx(280, abs=2)
    await page.keyboard.press("ArrowLeft")
    assert (await page.locator("#document-preview").bounding_box())["width"] > 280
    await page.keyboard.press("End")
    maximum = (await page.locator("#document-preview").bounding_box())["width"]
    assert maximum <= 820
    assert (await page.locator(".workspace").bounding_box())["width"] >= 420

    previous_upper = await page.locator("#preview-directory-pane").bounding_box()
    await split_handle.focus()
    await page.keyboard.press("ArrowDown")
    assert (await page.locator("#preview-directory-pane").bounding_box())["height"] > previous_upper["height"]

    previous_sidebar = await page.locator("#sidebar").bounding_box()
    await drag_separator(page, "#sidebar-resize-handle", dx=80)
    assert (await page.locator("#sidebar").bounding_box())["width"] > previous_sidebar["width"]
    await page.set_viewport_size({"width": 1180, "height": 960})
    await expect(width_handle).to_be_visible()
    assert (await page.locator("#document-preview").bounding_box())["width"] >= 280
    assert (await page.locator(".workspace").bounding_box())["width"] >= 420
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert task_page[3] == []


async def test_mobile_preview_keeps_both_panes_without_horizontal_overflow(task_page):
    page = await show_long_preview(task_page, width=390, height=844)
    await expect(page.locator("#preview-resize-handle")).to_be_hidden()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    upper = await page.locator("#preview-directory-pane").bounding_box()
    await drag_separator(page, "#preview-split-handle", dy=50)
    assert (await page.locator("#preview-directory-pane").bounding_box())["height"] > upper["height"]
    await expect(page.locator("#preview-page-pane")).to_be_visible()
    await page.get_by_role("button", name="关闭文档预览").click()
    await expect(page.get_by_role("button", name="打开文档预览")).to_have_attribute("aria-expanded", "false")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert task_page[3] == []
