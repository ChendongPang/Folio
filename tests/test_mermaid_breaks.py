from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from folio.visuals import render


@pytest.mark.parametrize("linebreak", ["<br>", "<br/>", "<br />", "<BR/>"])
def test_bare_break_is_allowed_without_changing_source(linebreak):
    source = f'flowchart TD\n A["交易发起{linebreak}内部审批"] --> B["完成"]'
    assert render._flow_source(source) == source


@pytest.mark.parametrize(
    "forbidden",
    [
        '<br onclick="alert(1)">',
        '<br style="color:red">',
        '<br class="label">',
        '<br src="file:///etc/passwd">',
        "</br>",
        "<br/><script>alert(1)</script>",
        "<br/><img src=x onerror=alert(1)>",
        "<br/><b>额外HTML</b>",
        "<br/><svg onload=alert(1)>",
        "<scr<br/>ipt>alert(1)</script>",
        "<br/>https://example.test",
        "<br/>file:///etc/passwd",
        "<br/>javascript:alert(1)",
        "<br/>data:text/html,malicious",
    ],
)
def test_break_does_not_allow_attributes_html_scripts_or_resources(forbidden):
    with pytest.raises(render.VisualRenderError):
        render._flow_source(f'flowchart TD\n A["审批{forbidden}"] --> B')


@pytest.mark.parametrize(
    "directive",
    [
        'click A "https://example.test"',
        "click A callback",
        '%%{init: {"securityLevel":"loose"}}%%',
        'A@{ img: "file:///tmp/source.png" }',
    ],
)
def test_break_preserves_interaction_and_configuration_rejection(directive):
    with pytest.raises(render.VisualRenderError):
        render._flow_source('flowchart TD\n A["第一行<br/>第二行"] --> B\n' + directive)


async def test_chinese_bare_breaks_render_as_separate_svg_text_lines(tmp_path, monkeypatch):
    try:
        render._mermaid_js()
    except render.VisualDependencyError:
        pytest.skip("Install local Mermaid for browser integration tests")
    async with async_playwright() as playwright:
        executable = (
            render.os.environ.get("FOLIO_CHROMIUM_EXECUTABLE")
            or playwright.chromium.executable_path
        )
        if not Path(executable).is_file():
            pytest.skip("Install Chromium for browser integration tests")
    # Capture the actual production DOM, not a second Mermaid implementation.
    original_page = render._page
    captured = []

    @asynccontextmanager
    async def inspected_page(**kwargs):
        async with original_page(**kwargs) as page:
            yield page
            captured.extend(
                await page.evaluate("""() => [...document.querySelectorAll('svg .node')].map(node => {
                const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
                const lines = [];
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (!text) continue;
                    const range = document.createRange();
                    range.selectNodeContents(walker.currentNode);
                    lines.push({text, y: range.getBoundingClientRect().y});
                }
                return {text: node.textContent, lines,
                    activeContent: node.querySelectorAll('script,iframe,img,a[href]').length};
            })""")
            )

    monkeypatch.setattr(render, "_page", inspected_page)
    source = 'flowchart TD\n A["交易发起<br/>内部审批<br>登记结果"] --> B["清算<br />完成"]'
    result = await render.render_flowchart(source, tmp_path)
    assert Path(result["image_path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert Path(result["source_path"]).read_text() == source
    node = next(node for node in captured if "交易发起" in node["text"])
    positions = {line["text"]: line["y"] for line in node["lines"]}
    assert positions["交易发起"] < positions["内部审批"] < positions["登记结果"]
    assert all(node["activeContent"] == 0 for node in captured)
