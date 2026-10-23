from __future__ import annotations

import asyncio
import base64
import struct
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from folio.visuals import render
from folio.visuals.render import VisualDependencyError, VisualRenderError


@pytest.fixture
async def browser_ready():
    async with async_playwright() as playwright:
        executable = render.os.environ.get("FOLIO_CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install Chromium with python -m folio.visuals.install for browser integration tests")


@pytest.fixture
def mermaid_ready():
    try:
        render._mermaid_js()
    except VisualDependencyError:
        pytest.skip("Install local Mermaid with python -m folio.visuals.install")


def _assert_png(result: dict) -> None:
    data = Path(result["image_path"]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (result["width"], result["height"])
    assert len(data) > 1000
    assert Path(result["source_path"]).is_file()


@pytest.mark.parametrize("source", [
    '<script>fetch("https://example.test")</script>',
    '<button onclick="alert(1)">提交</button>',
    '<iframe src="file:///etc/passwd"></iframe>',
    '<img src="http://127.0.0.1:8765/settings">',
    '<img src="file:///etc/passwd">',
    '<link rel="stylesheet" href="https://example.test/style.css">',
    '<style>@import "https://example.test/style.css";</style><p>界面</p>',
    '<p style="background:url(http://127.0.0.1/private)">界面</p>',
    '<meta http-equiv="refresh" content="0;url=http://127.0.0.1">',
    '<svg><foreignObject><iframe src="file:///etc/passwd"></iframe></foreignObject></svg>',
])
async def test_ui_rejects_active_or_external_content(source, tmp_path):
    with pytest.raises(VisualRenderError, match="cannot|not allowed"):
        await render.render_ui(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("source", [
    'flowchart TD\nA --> B\nclick A "https://example.test"',
    'flowchart TD\nA --> B; click A callback',
    'flowchart TD\n%%{init: {"securityLevel":"loose"}}%%\nA --> B',
    'flowchart TD\nA["<img src=x onerror=alert(1)>"] --> B',
    'flowchart TD\nA@{ img: "https://example.test/image.png" }',
    'flowchart TD\nA["file:///etc/passwd"] --> B',
    'sequenceDiagram\nAlice->>Bob: Hello',
])
async def test_flowchart_rejects_interaction_or_non_flowcharts(source, tmp_path):
    with pytest.raises(VisualRenderError):
        await render.render_flowchart(source, tmp_path)
    assert not list(tmp_path.iterdir())


async def test_source_limits_and_missing_dependency(tmp_path, monkeypatch):
    with pytest.raises(VisualRenderError, match="cannot be empty"):
        await render.render_ui(" ", tmp_path)
    with pytest.raises(VisualRenderError, match="exceeds"):
        await render.render_ui("界" * 100_000, tmp_path)
    monkeypatch.setenv("FOLIO_MERMAID_JS", str(tmp_path / "missing.js"))
    with pytest.raises(VisualDependencyError) as raised:
        await render.render_flowchart("flowchart TD\nA --> B", tmp_path)
    assert raised.value.code == "dependency_missing"


async def test_chinese_ui_and_editable_source_round_trip(browser_ready, tmp_path):
    source = '''<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>.dialog {padding:24px;border:1px solid #bbb;} h1 {color:#1769cc;}
    td,th {padding:12px;border:1px solid #bbb;} button {padding:8px 24px;}</style>
    </head><body><main class="dialog"><h1>拆分明细</h1>
    <label>拆分币种 <input value="USD"></label><p>业务日期：2026-10-09</p>
    <table><tr><th>币种</th><th>拆分金额</th></tr><tr><td>人民币</td><td>100,000.00</td></tr></table>
    <p><button>取消</button> <button>确定</button></p></main></body></html>'''
    first = await render.render_ui(source, tmp_path / "first")
    _assert_png(first)
    assert first["width"] == 1200 and 200 < first["height"] < 800
    editable = Path(first["source_path"]).read_text()
    assert "拆分明细" in editable and "Content-Security-Policy" in editable
    second = await render.render_ui(editable, tmp_path / "second")
    assert Path(first["image_path"]).read_bytes() == Path(second["image_path"]).read_bytes()
    assert not list((tmp_path / "first").glob(".*"))


async def test_chinese_flowchart_is_cropped_and_source_preserved(browser_ready, mermaid_ready, tmp_path):
    source = 'flowchart TD\n A[提交交易] --> B{审批通过？}\n B -->|是| C[成交登记]\n B -->|否| D[退回修改]\n D --> A'
    result = await render.render_flowchart(source, tmp_path)
    _assert_png(result)
    assert 100 < result["width"] < 700
    assert 200 < result["height"] < 800
    assert Path(result["source_path"]).read_text() == source


async def test_tall_images_include_content_below_the_browser_viewport(browser_ready, mermaid_ready, tmp_path):
    ui = await render.render_ui('<div style="height:1000px">开始</div><p>页面末尾</p>', tmp_path / "ui")
    _assert_png(ui)
    assert ui["height"] > 1000
    flow = await render.render_flowchart(
        'flowchart TD\n' + '\n'.join(f'A{i}[步骤{i}] --> A{i + 1}[步骤{i + 1}]' for i in range(9)),
        tmp_path / "flow",
    )
    _assert_png(flow)
    assert flow["height"] > 800


async def test_bad_mermaid_returns_actionable_error_without_artifact(browser_ready, mermaid_ready, tmp_path):
    with pytest.raises(VisualRenderError, match="Mermaid syntax") as raised:
        await render.render_flowchart("flowchart TD\nA[broken --> B", tmp_path)
    assert raised.value.code == "render_failed"
    assert not list(tmp_path.iterdir())


async def test_oversized_ui_is_rejected_instead_of_cropped(browser_ready, tmp_path):
    with pytest.raises(VisualRenderError, match="dimensions") as raised:
        await render.render_ui('<div style="height:6000px">长表格</div>', tmp_path)
    assert raised.value.code == "too_large"
    assert not list(tmp_path.iterdir())


async def test_browser_cannot_make_network_requests_even_with_escaped_css(browser_ready, tmp_path):
    received = []

    async def requested(reader, writer):
        received.append(True)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(requested, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # CSS escapes evade the simple source check. Browser policy still denies the request.
        source = '<div style="height:100px;background:u\\72l(http://127.0.0.1:' + str(port) + '/private)">静态界面</div>'
        result = await render.render_ui(source, tmp_path)
        _assert_png(result)
        assert received == []
    finally:
        server.close()
        await server.wait_closed()


async def test_render_timeout_is_bounded(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def stuck_page(**kwargs):
        await asyncio.sleep(10)
        yield None

    monkeypatch.setattr(render, "_page", stuck_page)
    monkeypatch.setattr(render, "RENDER_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(VisualRenderError) as raised:
        await render.render_ui("<p>超时</p>", tmp_path)
    assert raised.value.code == "timeout"
    assert not list(tmp_path.iterdir())


async def test_browser_dependency_error_is_not_a_source_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_CHROMIUM_EXECUTABLE", str(tmp_path / "missing-chromium"))
    with pytest.raises(VisualDependencyError):
        await render.render_ui("<p>界面</p>", tmp_path)


@pytest.mark.parametrize("source", [
    '<!doctype html><html><head><style>body {background:#f4f6fa;min-height:800px;}',
    '<style>body {background:#f4f6fa;}</style>',
    '<html><head><title>交易申请</title></head><body></body></html>',
    '<div style="width:900px;height:600px;background:#eee"></div>',
])
async def test_style_only_or_empty_ui_cannot_create_a_successful_image(source, tmp_path):
    with pytest.raises(VisualRenderError, match="no body content or controls") as raised:
        await render.render_ui(source, tmp_path)
    assert raised.value.code == "invalid_source"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source", [
    '<main hidden><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="display:none"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="visibility:hidden"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="opacity:0"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="content-visibility:hidden"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="position:absolute;left:-2000px"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="transform:translateY(-2000px)"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="height:0;overflow:hidden"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="width:0;overflow:hidden"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="height:100px;overflow:hidden"><div style="position:relative;top:200px">审批界面<input value="交易金额"></div></main>',
    '<main style="clip-path:inset(50%)"><h1>审批界面</h1><input value="交易金额"></main>',
    '<main style="position:absolute;clip:rect(0px,0px,0px,0px)"><h1>审批界面</h1><input value="交易金额"></main>',
    '<p style="font-size:0">审批界面</p>',
    '<p style="color:transparent">审批界面</p>',
    '<p style="-webkit-text-fill-color:transparent">审批界面</p>',
    '<p style="color:white">审批界面</p>',
    '<input type="hidden" value="交易金额">',
])
async def test_invisible_or_clipped_ui_cannot_create_a_successful_image(browser_ready, source, tmp_path):
    with pytest.raises(VisualRenderError, match="visible") as raised:
        await render.render_ui(source, tmp_path)
    assert raised.value.code == "invalid_source"
    assert not list(tmp_path.iterdir())


async def test_visible_controls_and_partially_visible_content_are_allowed(browser_ready, tmp_path):
    controls = await render.render_ui('<input placeholder="交易金额"><select><option>人民币</option></select>', tmp_path / "controls")
    _assert_png(controls)
    partial = await render.render_ui(
        '<main style="height:100px;overflow:hidden"><h1>审批界面</h1><div style="height:1000px">更多字段</div></main>',
        tmp_path / "partial",
    )
    _assert_png(partial)


async def test_fixed_modal_captures_the_fields_below_normal_body_height(browser_ready, tmp_path):
    source = '<div style="position:fixed;top:50px;left:40px;width:600px;height:400px;background:#eaf0ff">拆分明细<div style="margin-top:250px">提交按钮</div></div>'
    result = await render.render_ui(source, tmp_path / "fixed")
    _assert_png(result)
    assert result["height"] == 450
    # Check actual dark text pixels in the formerly missing bottom portion;
    # fixed elements can use different text antialiasing than absolute elements.
    async with render._page(javascript=True) as page:
        bottom_ink = await page.evaluate("""async (png) => {
            const image = new Image();
            image.src = 'data:image/png;base64,' + png;
            await image.decode();
            const canvas = new OffscreenCanvas(image.width, image.height);
            const context = canvas.getContext('2d');
            context.drawImage(image, 0, 0);
            const pixels = context.getImageData(0, 280, image.width, image.height - 280).data;
            let ink = 0;
            for (let i = 0; i < pixels.length; i += 4) {
                if (pixels[i] < 100 && pixels[i+1] < 100 && pixels[i+2] < 150) ink++;
            }
            return ink;
        }""", base64.b64encode(Path(result["image_path"]).read_bytes()).decode())
        assert bottom_ink > 20
    expected = await render.render_ui(source.replace('position:fixed', 'position:absolute'), tmp_path / "absolute")
    _assert_png(expected)
    assert expected["height"] == 450


async def test_fixed_modal_includes_children_that_extend_beyond_its_box(browser_ready, tmp_path):
    source = '<div style="position:fixed;top:50px;left:40px;width:600px;height:100px;background:#eaf0ff">拆分明细<button style="position:absolute;top:400px;height:40px">提交按钮</button></div>'
    result = await render.render_ui(source, tmp_path)
    _assert_png(result)
    assert result["height"] == 490


@pytest.mark.parametrize("position", ['top:700px', 'top:-20px', 'left:-20px', 'left:1100px'])
async def test_fixed_modal_outside_viewport_is_rejected_before_screenshot(browser_ready, position, tmp_path):
    source = '<div style="position:fixed;top:0;left:0;width:600px;height:400px;' + position + '">审批界面<button>提交</button></div>'
    with pytest.raises(VisualRenderError, match="Fixed-position UI content exceeds the visible viewport") as raised:
        await render.render_ui(source, tmp_path)
    assert raised.value.code == "invalid_source"
    assert not list(tmp_path.iterdir())
