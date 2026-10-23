"""Real browser commits for the settings' native selects and checkboxes."""

import pytest
from playwright.async_api import expect
from test_vue_workspace import model_routes

pytest_plugins = ("test_workspace_task_navigation",)


async def choose_native_option(page, selector, key):
    # select_option() emits input/change synchronously and misses the browser's
    # render opportunity between the native popup's two events.
    await page.locator(selector).click()
    await page.keyboard.press(key)
    await page.keyboard.press("Enter")


async def test_native_search_provider_selection_invalidates_previous_test(task_page):
    page, _, _, errors = task_page
    await model_routes(page)
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#search-provider")).to_be_enabled()
    await choose_native_option(page, "#search-provider", "ArrowDown")
    await expect(page.locator("#search-provider")).to_have_value("deepseek")
    await page.locator("#deepseek-api-key").fill("synthetic-key")
    await page.locator("#test-tavily-button").click()
    await page.locator("#tavily-enabled").check()
    await choose_native_option(page, "#search-provider", "ArrowUp")
    await expect(page.locator("#tavily-enabled")).to_be_disabled()
    await expect(page.locator("#tavily-enabled")).not_to_be_checked()
    assert errors == []


async def test_native_checkbox_commits_before_test_invalidation(task_page):
    page, _, _, errors = task_page
    await model_routes(page)
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-test-status")).to_have_text("测试通过")
    await page.locator("#model-no-auth").click()
    await expect(page.locator("#model-no-auth")).to_be_checked()
    await expect(page.locator("#model-api-key")).to_be_disabled()
    await expect(page.locator("#model-test-status")).to_contain_text("重新测试")
    await page.locator(".advanced-settings summary").click()
    await page.locator("#supports-image-input").click()
    await expect(page.locator("#supports-image-input")).to_be_checked()
    await expect(page.locator("#layout-review-enabled")).to_have_count(0)
    assert errors == []


@pytest.mark.parametrize(
    ("button", "output"),
    [
        ("#test-model-button", "#model-test-status"),
        ("#save-model-button", "#model-settings-result"),
    ],
)
async def test_outdated_backend_explains_rejected_settings(task_page, button, output):
    page, _, _, errors = task_page
    await model_routes(page)

    async def outdated_backend(route):
        if route.request.method == "GET":
            await route.fallback()
            return
        await route.fulfill(status=422, json={
            "detail": [
                {
                    "loc": ["body", field],
                    "msg": "Extra inputs are not permitted",
                    "type": "extra_forbidden",
                    "input": "synthetic-secret-never-display",
                }
                for field in ("supports_image_input", "max_input_tokens")
            ],
        })

    await page.route("**/settings/model**", outdated_backend)
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-base-url")).to_be_enabled()
    await page.locator("#model-api-key").fill("synthetic-key")
    await page.locator(button).click()
    await expect(page.locator(output)).to_have_text(
        "当前后台不支持以下配置项：图片输入、最大输入 Token。"
        "请更新或重启 Folio 后台，再刷新页面重试。"
    )
    await expect(page.locator("#model-settings-dialog")).not_to_contain_text("synthetic-secret-never-display")
    await expect(page.locator(button)).to_be_enabled()
    assert errors == []
