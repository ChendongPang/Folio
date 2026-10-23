"""The built settings UI must work against the real API and settings store."""

import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from folio.api.app import WEB_DIST, create_app

ROUTER_KEY = "synthetic-contract-router-key"
TAVILY_KEY = "synthetic-contract-tavily-key"
ROUTER_URL = "https://router.contract.test/auto/v1"


@pytest.fixture
async def settings_page(tmp_path, monkeypatch):
    """Only provider calls are mocked; browser requests use the complete ASGI app."""
    assert (WEB_DIST / "index.html").is_file(), "Run npm --prefix frontend run build"
    probes = []
    for component, name in (
        ("llm", "test_connection"),
        ("decision", "test_decision_connection"),
        ("tavily", "test_tavily_connection"),
    ):

        async def probe(config, component=component):
            probes.append((component, deepcopy(config)))
            return {"ok": True, "message": "测试通过"}

        monkeypatch.setattr(f"folio.api.app.{name}", probe)

    async with async_playwright() as playwright:
        executable = (
            os.environ.get("FOLIO_CHROMIUM_EXECUTABLE")
            or playwright.chromium.executable_path
        )
        if not Path(executable).is_file():
            pytest.skip("Install local Chromium for browser integration tests")
        with TestClient(create_app(tmp_path / "workspace")) as client:
            browser = await playwright.chromium.launch(
                executable_path=executable, headless=True
            )
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def serve(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    assert parsed.netloc == "testserver", "No external request is permitted"
                    headers = {
                        name: value
                        for name, value in request.headers.items()
                        if name in {"content-type", "origin"}
                    }
                    response = client.request(
                        request.method,
                        parsed.path + ("?" + parsed.query if parsed.query else ""),
                        content=request.post_data_buffer,
                        headers=headers,
                    )
                    await route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        content_type=response.headers.get("content-type", "text/plain"),
                    )

                await page.route("**/*", serve)
                await page.goto("http://testserver/")
                await page.locator("#model-settings-open").click()
                await expect(page.locator("#model-base-url")).to_be_enabled()
                yield page, client, probes, errors
            finally:
                await browser.close()


@pytest.mark.parametrize("decision_model", ["jev", "luna-decisions"])
@pytest.mark.parametrize("visual_enabled", [False, True], ids=["text", "vision"])
async def test_generic_browser_api_contract_saves_reopens_and_retains_credentials(
    settings_page, decision_model, visual_enabled
):
    page, client, probes, errors = settings_page
    await page.locator("#model-name").fill("any-model")
    await expect(page.locator("#custom-model-fields")).to_be_visible()
    await page.locator("#model-base-url").fill(ROUTER_URL)
    await page.locator("#model-api-key").fill(ROUTER_KEY)
    await page.locator("#decision-model").fill(decision_model)
    await page.locator("#decision-base-url").fill("https://judge.test/v1")
    await page.locator("#decision-api-key").fill("judge-secret")
    await page.locator("#tavily-api-key").fill(TAVILY_KEY)
    await page.locator(".advanced-settings summary").click()
    await expect(page.locator("#model-max-input-tokens")).to_have_value("600000")
    await expect(page.locator("#model-max-tokens")).to_have_value("128000")
    await expect(page.locator("#model-planning-max-tokens")).to_have_value("")
    if visual_enabled:
        await page.locator("#supports-image-input").check()
    await expect(page.locator("#layout-review-enabled")).to_have_count(0)
    await expect(page.locator("#model-settings-dialog")).not_to_contain_text("逐页评分")

    await expect(page.locator("#decision-enabled")).to_be_disabled()
    await expect(page.locator("#tavily-enabled")).to_be_disabled()
    for component, button, status in (
        ("llm", "test-model-button", "model-test-status"),
        ("decision", "test-decision-button", "decision-test-status"),
        ("tavily", "test-tavily-button", "tavily-test-status"),
    ):
        await page.locator(f"#{button}").click()
        await expect(page.locator(f"#{status}")).to_have_text("测试通过")
        if component != "llm":
            await expect(page.locator(f"#{component}-enabled")).to_be_enabled()
            await page.locator(f"#{component}-enabled").check()

    async with page.expect_request(
        lambda request: request.method == "PUT" and request.url.endswith("/settings/model")
    ) as request_info:
        await page.locator("#save-model-button").click()
    assert "layout_review_enabled" not in (await request_info.value).post_data_json
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    public = client.get("/settings/model").json()
    assert public["provider_mode"] == "custom"
    assert public["model"] == "any-model"
    assert public["max_tokens"] == 128000
    assert public["max_input_tokens"] == 600000
    assert public["planning_max_tokens"] is None
    assert public["decision_model"] == decision_model
    assert public["decision_enabled"] and public["tavily_enabled"]
    assert public["supports_image_input"] is visual_enabled
    assert "layout_review_enabled" not in public
    assert public["test_status"] == {"llm": True, "decision": True, "tavily": True}
    assert public["has_api_key"] and public["has_tavily_api_key"]
    assert not {"api_key", "decision_api_key", "tavily_api_key"} & public.keys()

    await page.locator("#model-settings-close").click()
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-base-url")).to_have_value(ROUTER_URL)
    await expect(page.locator("#decision-model")).to_have_value(decision_model)
    await expect(page.locator("#model-api-key")).to_have_value("")
    await expect(page.locator("#tavily-api-key")).to_have_value("")
    await expect(page.locator("#saved-key-label")).to_contain_text("留空保持不变")
    await expect(page.locator("#tavily-saved-key-label")).to_contain_text("留空保持不变")
    await expect(page.locator("#decision-enabled")).to_be_checked()
    await expect(page.locator("#tavily-enabled")).to_be_checked()
    await expect(page.locator("#supports-image-input")).to_be_checked(checked=visual_enabled)
    await expect(page.locator("#layout-review-enabled")).to_have_count(0)
    for status in ("model-test-status", "decision-test-status", "tavily-test-status"):
        await expect(page.locator(f"#{status}")).to_have_text("测试通过")

    # Save the actual UI's blank secret inputs, then prove each service still
    # receives its retained key through real resolution and probe endpoints.
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    assert client.get("/settings/model").json() == public
    for component, button, status in (
        ("llm", "test-model-button", "model-test-status"),
        ("decision", "test-decision-button", "decision-test-status"),
        ("tavily", "test-tavily-button", "tavily-test-status"),
    ):
        previous = len(probes)
        async with page.expect_response(
            lambda response: response.request.method == "POST"
            and "/settings/model/test" in response.url
        ) as response_info:
            await page.locator(f"#{button}").click()
        assert (await response_info.value).status == 200
        await expect(page.locator(f"#{status}")).to_have_text("测试通过")
        assert len(probes) == previous + 1
        observed_component, config = probes[-1]
        assert observed_component == component
        assert config["api_key"] == ROUTER_KEY
        assert config["tavily_api_key"] == TAVILY_KEY
        assert config["decision_model"] == decision_model
        assert config["supports_image_input"] is visual_enabled
        assert "layout_review_enabled" not in config

    assert len(probes) == 6
    storage = await page.evaluate(
        "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
    )
    assert ROUTER_KEY not in storage and TAVILY_KEY not in storage
    assert errors == []


async def test_browser_saves_large_planning_budget_and_custom_input_budget(settings_page):
    page, client, _, errors = settings_page
    await page.locator("#model-name").fill("any-model")
    await page.locator("#model-base-url").fill(ROUTER_URL)
    await page.locator("#model-api-key").fill(ROUTER_KEY)
    await page.locator(".advanced-settings summary").click()
    await page.locator("#model-max-input-tokens").fill("500000")
    await page.locator("#model-max-tokens").fill("128000")
    await page.locator("#model-planning-max-tokens").fill("128000")
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    public = client.get("/settings/model").json()
    assert public["max_input_tokens"] == 500000
    assert public["max_tokens"] == public["planning_max_tokens"] == 128000
    await page.locator("#model-settings-close").click()
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#model-max-input-tokens")).to_have_value("500000")
    await expect(page.locator("#model-max-tokens")).to_have_value("128000")
    await expect(page.locator("#model-planning-max-tokens")).to_have_value("128000")
    assert errors == []


async def test_model_test_status_code_follows_ui_language(settings_page, monkeypatch):
    page, _, _, errors = settings_page

    async def probe(config):
        return {
            "ok": True,
            "code": "native_tools_verified",
            "message": "连接测试通过，已验证模型支持原生工具调用。",
        }

    monkeypatch.setattr("folio.api.app.test_connection", probe)
    await page.locator("#language-select").select_option("en")
    await page.locator("#model-name").fill("any-model")
    await page.locator("#model-base-url").fill(ROUTER_URL)
    await page.locator("#model-api-key").fill(ROUTER_KEY)
    await page.locator("#test-model-button").click()
    await expect(page.locator("#model-test-status")).to_have_text(
        "Connection verified with native tool support."
    )
    assert errors == []


async def test_browser_selects_deepseek_and_retains_separate_key(settings_page):
    page, client, probes, errors = settings_page
    await page.locator('#model-base-url').fill('https://llm.test/v1')
    await page.locator('#model-name').fill('custom-model-id')
    await page.locator('#model-api-key').fill('generation-key')
    await page.locator('#search-provider').select_option('deepseek')
    await page.locator('#deepseek-api-key').fill('deepseek-key')
    await page.locator('#test-tavily-button').click()
    await expect(page.locator('#tavily-enabled')).to_be_enabled()
    assert probes[-1][1]['search_provider'] == 'deepseek'
    assert probes[-1][1]['deepseek_api_key'] == 'deepseek-key'
    await page.locator('#tavily-enabled').check()
    await page.locator('#save-model-button').click()
    await expect(page.locator('#model-settings-result')).to_contain_text('配置已保存')
    public = client.get('/settings/model').json()
    assert public['search_provider'] == 'deepseek' and public['has_deepseek_api_key']
    assert 'deepseek-key' not in str(public)
    await page.locator('#model-settings-close').click()
    await page.locator('#model-settings-open').click()
    await expect(page.locator('#search-provider')).to_have_value('deepseek')
    await expect(page.locator('#deepseek-api-key')).to_have_value('')
    await expect(page.locator('#tavily-enabled')).to_be_checked()
    await page.locator('#search-provider').select_option('tavily')
    await expect(page.locator('#tavily-enabled')).not_to_be_checked()
    await expect(page.locator('#tavily-enabled')).to_be_disabled()
    assert errors == []


@pytest.mark.parametrize('language', ['zh', 'en'])
async def test_probe_failure_preserves_specific_message_and_http_status(settings_page, monkeypatch, language):
    page, _, _, errors = settings_page

    async def probe(config):
        return {
            'ok': False, 'code': 'model_test_failed',
            'message': '模型认证失败，请在模型设置中检查 API Key。',
            'diagnostics': {'status_code': 401},
        }

    monkeypatch.setattr('folio.api.app.test_connection', probe)
    await page.locator('#language-select').select_option('zh-CN' if language == 'zh' else language)
    await page.locator('#model-name').fill('any-model')
    await page.locator('#model-base-url').fill('https://model.test/v1')
    await page.locator('#model-api-key').fill('synthetic-key')
    await page.locator('#test-model-button').click()
    status = page.locator('#model-test-status')
    await expect(status).to_contain_text('HTTP 401')
    await expect(status).to_contain_text('API Key' if language == 'zh' else 'API key')
    await expect(status).not_to_contain_text('模型连接测试失败')
    await expect(status).not_to_contain_text('connection test failed')
    assert errors == []


async def test_changed_endpoint_does_not_inherit_hidden_template_parameters(settings_page):
    page, client, probes, errors = settings_page
    client.put('/settings/model', json={
        'base_url': 'https://old.test/v1', 'model': 'old-model', 'api_key': 'old-key',
        'enable_thinking': False,
    })
    await page.locator('#model-settings-close').click()
    await page.locator('#model-settings-open').click()
    await expect(page.locator('#model-base-url')).to_have_value('https://old.test/v1')
    await page.locator('#model-base-url').fill('https://new.test/v1')
    await page.locator('#model-name').fill('new-model')
    await page.locator('#model-api-key').fill('new-key')
    await page.locator('#test-model-button').click()
    await expect(page.locator('#model-test-status')).to_contain_text('测试通过')
    assert probes[-1][1]['enable_thinking'] is None
    assert errors == []
