"""Decision settings explain the selected wire protocol without changing inputs."""

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_model_settings_contract",)


@pytest.mark.parametrize(
    ("endpoint", "native"),
    [
        ("https://api.typesafe.ai/v1/systemone", True),
        ("https://decision.test/systemone/", True),
        ("https://decision.test/v1", False),
        ("https://decision.test/v1/systemone-other", False),
    ],
)
async def test_decision_endpoint_help_follows_url_and_language(
    settings_page, endpoint, native
):
    page, _, _, errors = settings_page
    await page.locator("#decision-model").fill("provider-model-v2")
    await page.locator("#decision-base-url").fill(endpoint)
    help_text = page.locator("#decision-endpoint-help")
    await expect(help_text).to_have_text(
        "Jev 原生接口：地址原样使用。"
        if native
        else "OpenAI 兼容接口：填写 Base URL，不含 /chat/completions。"
    )
    await expect(page.locator("#decision-model")).to_have_attribute(
        "placeholder", "jev-latest" if native else "服务提供的模型 ID"
    )
    await page.locator("#language-select").select_option("en")
    await expect(help_text).to_have_text(
        "Native Jev endpoint: the URL is used as entered."
        if native
        else "OpenAI-compatible endpoint: enter the base URL without /chat/completions."
    )
    if native:
        await expect(page.locator("#decision-model-help")).to_have_text(
            "Enter a model ID offered by the service, such as jev-latest."
        )
    await expect(page.locator("#decision-base-url")).to_have_value(endpoint)
    await expect(page.locator("#decision-model")).to_have_value("provider-model-v2")
    assert errors == []


async def test_native_decision_settings_preserve_endpoint_model_and_key(settings_page):
    page, client, probes, errors = settings_page
    endpoint = "https://api.typesafe.ai/v1/systemone"
    await page.locator("#model-base-url").fill("https://generation.test/v1")
    await page.locator("#model-name").fill("generation-model")
    await page.locator("#model-api-key").fill("generation-secret")
    await page.locator("#decision-base-url").fill(endpoint)
    await page.locator("#decision-model").fill("jev-future-version")
    await page.locator("#decision-api-key").fill("synthetic-decision-key")
    await page.locator("#test-decision-button").click()
    await expect(page.locator("#decision-test-status")).to_have_text("测试通过")
    _, config = probes[-1]
    assert config["decision_base_url"] == endpoint
    assert config["decision_model"] == "jev-future-version"
    assert config["decision_api_key"] == "synthetic-decision-key"
    await page.locator("#decision-enabled").check()
    await page.locator("#save-model-button").click()
    await expect(page.locator("#model-settings-result")).to_contain_text("配置已保存")
    public = client.get("/settings/model").json()
    assert public["decision_base_url"] == endpoint
    assert public["decision_model"] == "jev-future-version"
    await page.locator("#model-settings-close").click()
    await page.locator("#model-settings-open").click()
    await expect(page.locator("#decision-api-key")).to_have_value("")
    await expect(page.locator("#decision-endpoint-help")).to_have_text(
        "Jev 原生接口：地址原样使用。"
    )
    await expect(page.locator("#decision-model")).to_have_value("jev-future-version")
    assert errors == []
