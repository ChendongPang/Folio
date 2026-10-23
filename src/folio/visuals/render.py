"""Bounded local renderers: static HTML and strict Mermaid flowcharts.

Only trusted Mermaid code executes. UI source never gets JavaScript privileges.
Network routing, CSP, and a fresh browser context provide independent boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path

from lxml import etree, html
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

MAX_UI_BYTES = 200_000
MAX_FLOW_BYTES = 40_000
MAX_WIDTH = 2400
MAX_HEIGHT = 3600
MAX_PIXELS = 6_000_000
MAX_PNG_BYTES = 12_000_000
RENDER_TIMEOUT_SECONDS = 30
_RENDER_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_FONT = '"Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif'
_BASE_CSS = f"""
* {{box-sizing: border-box; animation: none !important; transition: none !important;}}
html {{background: #ffffff; color: #172b4d; font-family: {_FONT}; font-size: 16px;}}
body {{margin: 0; padding: 24px; min-height: 120px; width: 1200px;}}
input, button, select, textarea {{font: inherit;}}
table {{border-collapse: collapse;}}
"""
_ALLOWED_TAGS = frozenset(
    "html head body title meta style div span main section article header footer aside nav "
    "h1 h2 h3 h4 h5 h6 p br hr b strong i em u s small sub sup pre code blockquote "
    "ul ol li dl dt dd table thead tbody tfoot tr th td caption col colgroup "
    "form fieldset legend label input button select option optgroup textarea "
    "details summary figure figcaption img a svg path circle rect line polyline "
    "polygon ellipse g defs lineargradient radialgradient stop clippath use".split()
)
_URL_ATTRS = {"src", "href", "action", "formaction", "poster", "background", "xlink:href"}
_UI_CSP = (
    "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; font-src 'none'; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


class VisualRenderError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_source"):
        super().__init__(message)
        self.code = code


class VisualDependencyError(VisualRenderError):
    def __init__(self, message: str):
        super().__init__(message, code="dependency_missing")


def _check_source(source: str, limit: int) -> str:
    if not isinstance(source, str) or not source.strip():
        raise VisualRenderError('Figure source cannot be empty.')
    if len(source.encode("utf-8")) > limit:
        raise VisualRenderError(f'The figure source exceeds the {limit}-byte limit. Split the figure.', code="too_large")
    source = source.strip()
    # A single outer fence is harmless, but never recover arbitrary surrounding prose.
    fenced = re.fullmatch(r"```(?:html|mermaid)?\s*\n([\s\S]*?)\n```", source)
    return fenced.group(1).strip() if fenced else source


def _render_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _RENDER_SLOTS:
        _RENDER_SLOTS[loop] = asyncio.Semaphore(2)
    return _RENDER_SLOTS[loop]


def _ui_document(source: str) -> str:
    source = _check_source(source, MAX_UI_BYTES)
    try:
        document = html.document_fromstring(source, parser=html.HTMLParser(no_network=True))
    except (etree.ParserError, ValueError) as error:
        raise VisualRenderError("UI HTML cannot be parsed. Provide static HTML/CSS.") from error
    for node in list(document.iter()):
        if not isinstance(node.tag, str):
            continue
        if node.tag.lower() not in _ALLOWED_TAGS:
            raise VisualRenderError(f"UI prototypes cannot use {node.tag} elements. Only static HTML/CSS is supported.")
        if node.tag == "meta":
            policy = node.get("http-equiv", "").lower()
            if policy == "content-security-policy" and node.get("content") == _UI_CSP:
                node.getparent().remove(node)
                continue
            if "charset" in node.attrib and not policy:
                node.getparent().remove(node)
                continue
            if policy or node.get("name", "").lower() != "viewport":
                raise VisualRenderError(
                    "UI metadata may specify only charset and viewport; redirects and security-setting changes are not allowed."
                )
        if node.tag == "style" and node.text == _BASE_CSS:
            node.getparent().remove(node)
            continue
        for key, value in node.attrib.items():
            name = key.lower()
            if name.startswith("on") or name in {"srcdoc", "srcset", "ping", "is"}:
                raise VisualRenderError("UI prototypes cannot use script events, embedded pages or external resources.")
            if name in _URL_ATTRS and value and not value.startswith("#"):
                if name == "src" and re.match(r"^data:image/(?:png|jpeg|webp);base64,", value):
                    continue
                raise VisualRenderError("UI prototypes cannot reference network resources or local files; use inline styles.")
        css = (node.text or "") if node.tag == "style" else node.get("style", "")
        if re.search(r"@import\b|url\s*\(|expression\s*\(", css, re.I):
            raise VisualRenderError("UI styles cannot use external fonts, images, imports or script expressions.")
    body = document.find("body")
    text = "".join(body.xpath(".//text()[not(ancestor::style)]")) if body is not None else ""
    controls = (
        body.xpath(
            ".//*[self::input or self::select or self::textarea or self::button or self::img or self::svg]"
        )
        if body is not None
        else []
    )
    if not text.strip() and not controls:
        raise VisualRenderError(
            "UI HTML has no body content or controls; a blank figure is invalid. Close </style> and provide actual UI content in <body>."
        )
    head = document.find("head")
    if head is None:
        head = etree.Element("head")
        document.insert(0, head)
    head.insert(0, etree.Element("meta", charset="utf-8"))
    policy = etree.Element("meta", {"http-equiv": "Content-Security-Policy", "content": _UI_CSP})
    head.insert(1, policy)
    style = etree.Element("style")
    style.text = _BASE_CSS
    head.insert(2, style)
    return "<!doctype html>\n" + html.tostring(document, encoding="unicode")


def _flow_source(source: str) -> str:
    source = _check_source(source, MAX_FLOW_BYTES)
    if not re.match(r"^(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)\b", source):
        raise VisualRenderError("Start a flowchart with a direction declaration such as flowchart TD or graph LR.")
    if re.search(r"%%\s*\{|^---\s*$|@\{|(?:^|[;\n])\s*click\s", source, re.M | re.I):
        raise VisualRenderError("Flowcharts cannot use configuration directives, interactive links or external graphics.")
    # Mermaid treats a bare br as a label line break. This exception grants no
    # attributes or general HTML capability; strict browser policy stays intact.
    text_only = re.sub(r"<br[ \t]*/?>", "", source, flags=re.I)
    if re.search(r"(?:https?|file|javascript|data):|<\s*/?\s*[a-z]", text_only, re.I):
        raise VisualRenderError(
            "Flowcharts support only text nodes and attribute-free line breaks; other HTML, external links and local files are not allowed."
        )
    return source


def _mermaid_js() -> Path:
    configured = os.environ.get("FOLIO_MERMAID_JS")
    path = (
        Path(configured)
        if configured
        else Path(__file__).parent / "node_modules/mermaid/dist/mermaid.min.js"
    )
    if not path.is_file():
        raise VisualDependencyError(
            'Local Mermaid is not installed. Run uv run python -m folio.visuals.install.'
        )
    return path


@asynccontextmanager
async def _page(*, javascript: bool):
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("FOLIO_CHROMIUM_EXECUTABLE") or None,
                chromium_sandbox=True,
                args=["--disable-dev-shm-usage"],
                timeout=15_000,
            )
        except BrowserError as error:
            raise VisualDependencyError(
                'Chromium could not start. Run uv run playwright install chromium; on Linux, install missing system libraries with uv run playwright install-deps chromium.'
            ) from error
        try:
            context = await browser.new_context(
                viewport={"width": 1200, "height": 800},
                device_scale_factor=1,
                java_script_enabled=javascript,
                service_workers="block",
                accept_downloads=False,
                locale="zh-CN",
            )
            await context.route("**/*", lambda route: route.abort("blockedbyclient"))
            await context.set_offline(True)
            page = await context.new_page()
            page.set_default_timeout(10_000)
            yield page
        finally:
            await browser.close()


def _dimensions(width: float, height: float) -> tuple[int, int]:
    width, height = math.ceil(width), math.ceil(height)
    if width < 1 or height < 1:
        raise VisualRenderError('The figure has no visible content.')
    if width > MAX_WIDTH or height > MAX_HEIGHT or width * height > MAX_PIXELS:
        raise VisualRenderError(
            f'Image dimensions {width}×{height} are too large. Split it into overview and detail views so text remains readable in Word.',
            code="too_large",
        )
    return width, height


def _write_atomic(path: Path, data: bytes) -> None:
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _save(output_dir: Path, kind: str, source: str, png: bytes, width: int, height: int) -> dict:
    if len(png) > MAX_PNG_BYTES:
        raise VisualRenderError('The figure exceeds 12 MB. Simplify the page or split the figure.', code="too_large")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(source.encode()).hexdigest()[:16]
    source_path = output_dir / f"{kind}-{token}.{'html' if kind == 'ui' else 'mmd'}"
    image_path = output_dir / f"{kind}-{token}.png"
    _write_atomic(source_path, source.encode("utf-8"))
    _write_atomic(image_path, png)
    return {
        "image_path": str(image_path),
        "source_path": str(source_path),
        "width": width,
        "height": height,
    }


async def _assert_visible_ui(page, width: int, height: int) -> None:
    visible = await page.evaluate(
        """({width, height}) => {
        const excluded = new Set(['STYLE', 'SCRIPT', 'TITLE', 'OPTION']);
        const transparent = value => value === 'transparent' || /rgba\\([^)]*,\\s*0(?:\\.0+)?\\)$/.test(value);
        function visibleInk(element, css) {
            const ink = css.webkitTextFillColor || css.color;
            if (parseFloat(css.fontSize) < 1 || transparent(ink)) return false;
            if (css.textShadow !== 'none' || parseFloat(css.webkitTextStrokeWidth) > 0) return true;
            for (let parent = element; parent; parent = parent.parentElement) {
                const background = getComputedStyle(parent);
                if (background.backgroundImage !== 'none') return true;
                if (!transparent(background.backgroundColor)) return ink !== background.backgroundColor;
            }
            return true;
        }
        function intersects(rect, element) {
            const ownStyle = getComputedStyle(element);
            if (ownStyle.visibility === 'hidden' || ownStyle.visibility === 'collapse') return false;
            let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
            let right = Math.min(width, rect.right), bottom = Math.min(height, rect.bottom);
            if (right - left < 1 || bottom - top < 1) return false;
            for (let parent = element; parent; parent = parent.parentElement) {
                const css = getComputedStyle(parent);
                if (css.display === 'none' || Number(css.opacity) <= 0.01 ||
                    css.contentVisibility === 'hidden') return false;
                const box = parent.getBoundingClientRect();
                if (/(hidden|clip|scroll|auto)/.test(css.overflowX)) {
                    left = Math.max(left, box.left + parent.clientLeft);
                    right = Math.min(right, box.left + parent.clientLeft + parent.clientWidth);
                }
                if (/(hidden|clip|scroll|auto)/.test(css.overflowY)) {
                    top = Math.max(top, box.top + parent.clientTop);
                    bottom = Math.min(bottom, box.top + parent.clientTop + parent.clientHeight);
                }
                const clip = css.clip.match(/^rect\\(([^)]+)\\)$/);
                if (clip) {
                    const values = clip[1].split(/[, ]+/);
                    if (values.length === 4) {
                        const value = (s, fallback) => s === 'auto' ? fallback : parseFloat(s);
                        top = Math.max(top, box.top + value(values[0], 0));
                        right = Math.min(right, box.left + value(values[1], box.width));
                        bottom = Math.min(bottom, box.top + value(values[2], box.height));
                        left = Math.max(left, box.left + value(values[3], 0));
                    }
                }
                const inset = css.clipPath.match(/^inset\\(([^)]+)\\)$/);
                if (inset) {
                    const v = inset[1].split(/\\s+round\\s+/)[0].trim().split(/\\s+/);
                    const offsets = [v[0], v[1] || v[0], v[2] || v[0], v[3] || v[1] || v[0]];
                    const px = (s, size) => parseFloat(s) * (s.endsWith('%') ? size / 100 : 1);
                    top = Math.max(top, box.top + px(offsets[0], box.height));
                    right = Math.min(right, box.right - px(offsets[1], box.width));
                    bottom = Math.min(bottom, box.bottom - px(offsets[2], box.height));
                    left = Math.max(left, box.left + px(offsets[3], box.width));
                }
                if (right - left < 1 || bottom - top < 1) return false;
            }
            return true;
        }
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const parent = node.parentElement;
            if (!node.textContent.trim() || !parent || excluded.has(parent.tagName)) continue;
            const css = getComputedStyle(parent);
            if (!visibleInk(parent, css)) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            if ([...range.getClientRects()].some(rect => intersects(rect, parent))) return true;
        }
        return [...document.body.querySelectorAll('input:not([type="hidden"]),select,textarea,button,img,svg')]
            .some(element => intersects(element.getBoundingClientRect(), element));
    }""",
        {"width": width, "height": height},
    )
    if not visible:
        raise VisualRenderError(
            "The UI has no visible body content or controls; a blank figure is invalid. Check hidden, display, visibility, opacity, "
            "positioning and overflow clipping, and place actual UI content within the screenshot bounds."
        )


async def _measure_ui(page) -> tuple[int, int]:
    size = await page.evaluate("""() => {
        let width = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
        let height = Math.max(document.body.scrollHeight, document.body.offsetHeight);
        let fixedOutsideViewport = false;
        for (const element of document.body.querySelectorAll('*')) {
            const ownStyle = getComputedStyle(element);
            if (ownStyle.visibility === 'hidden' || ownStyle.visibility === 'collapse') continue;
            let fixed = false, outOfFlow = false, hidden = false;
            const rect = element.getBoundingClientRect();
            let left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
            for (let parent = element; parent; parent = parent.parentElement) {
                const css = getComputedStyle(parent);
                fixed ||= css.position === 'fixed';
                outOfFlow ||= css.position === 'fixed' || css.position === 'absolute';
                hidden ||= css.display === 'none' || Number(css.opacity) <= 0.01 || css.contentVisibility === 'hidden';
                // A fixed container's overflow can intentionally hide a large inactive panel.
                // Include visible descendants without counting those clipped regions.
                if (parent !== element) {
                    const box = parent.getBoundingClientRect();
                    if (/(hidden|clip|scroll|auto)/.test(css.overflowX)) {
                        left = Math.max(left, box.left + parent.clientLeft);
                        right = Math.min(right, box.left + parent.clientLeft + parent.clientWidth);
                    }
                    if (/(hidden|clip|scroll|auto)/.test(css.overflowY)) {
                        top = Math.max(top, box.top + parent.clientTop);
                        bottom = Math.min(bottom, box.top + parent.clientTop + parent.clientHeight);
                    }
                }
            }
            if (!outOfFlow || hidden || right - left < 1 || bottom - top < 1) continue;
            if (fixed && (left < 0 || top < 0 || right > innerWidth + 1 || bottom > innerHeight + 1)) {
                fixedOutsideViewport = true;
            }
            width = Math.max(width, right);
            height = Math.max(height, bottom);
        }
        return {width, height, fixedOutsideViewport};
    }""")
    if size["fixedOutsideViewport"]:
        raise VisualRenderError(
            "Fixed-position UI content exceeds the visible viewport, so the screenshot would crop fields or buttons. "
            "Use normal document flow, or fit the modal and all its contents inside the 1200×800 viewport."
        )
    return _dimensions(size["width"], size["height"])


async def render_ui(source: str, output_dir: Path) -> dict:
    """Render an isolated, noninteractive HTML/CSS screen to a PNG and editable HTML."""
    document = _ui_document(source)
    try:
        async with _render_slots(), asyncio.timeout(RENDER_TIMEOUT_SECONDS):
            async with _page(javascript=False) as page:
                await page.set_content(document, wait_until="load")
                width, height = await _measure_ui(page)
                await _assert_visible_ui(page, width, height)
                png = await page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": width, "height": height},
                    full_page=True,
                    animations="disabled",
                    timeout=10_000,
                )
    except (TimeoutError, BrowserTimeout) as error:
        raise VisualRenderError("UI rendering exceeded 30 seconds; simplify the layout.", code="timeout") from error
    except BrowserError as error:
        raise VisualRenderError(
            "UI rendering failed; check the static HTML/CSS layout.", code="render_failed"
        ) from error
    return _save(output_dir, "ui", document, png, width, height)


async def render_flowchart(source: str, output_dir: Path) -> dict:
    """Render strict Mermaid flowchart source using pinned, local JavaScript only."""
    source = _flow_source(source)
    javascript = _mermaid_js()
    try:
        async with _render_slots(), asyncio.timeout(RENDER_TIMEOUT_SECONDS):
            async with _page(javascript=True) as page:
                await page.set_content(
                    '<!doctype html><html><head><meta charset="utf-8">'
                    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
                    "connect-src 'none'; base-uri 'none'; form-action 'none'\">"
                    f"<style>{_BASE_CSS}body {{width: max-content;}}</style></head>"
                    '<body><div id="diagram"></div></body></html>',
                    wait_until="load",
                )
                await page.add_script_tag(path=str(javascript))
                rendered_graph = await page.evaluate(
                    """async ({source, limits}) => {
                    let graph, layout;
                    // Only whitespace changes. Keep all labels, nodes, branches,
                    // direction and the 18px font; never crop or scale the graph.
                    for (const [nodeSpacing, rankSpacing] of [[35,45], [24,28], [18,18]]) {
                    mermaid.initialize({
                        startOnLoad: false, securityLevel: 'strict', theme: 'neutral',
                        suppressErrorRendering: true, maxTextSize: 40000, maxEdges: 150,
                        fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, sans-serif',
                        themeVariables: {fontSize: '18px'},
                        flowchart: {htmlLabels: false, useMaxWidth: false, nodeSpacing, rankSpacing}
                    });
                    const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
                    const nodes = [...diagram.db.getVertices().values()].map(v => ({
                        id: v.id, label: v.text || v.id
                    }));
                    const edges = diagram.db.getEdges().map(e => ({
                        from_node: e.start, to_node: e.end, label: e.text || '',
                        type: e.type || '', stroke: e.stroke || 'normal'
                    }));
                    const current = {nodes, edges, complete: true};
                    if (graph && JSON.stringify(current) !== JSON.stringify(graph)) {
                        throw new Error('Compact layout changed the flowchart graph');
                    }
                    graph = current;
                    const result = await mermaid.render('folio-flowchart', source);
                    const container = document.getElementById('diagram');
                    container.innerHTML = result.svg;
                    container.querySelectorAll('a').forEach(a => a.removeAttribute('href'));
                    const svg = container.querySelector('svg');
                    const box = svg.viewBox.baseVal;
                    svg.style.maxWidth = 'none';
                    svg.setAttribute('width', String(Math.ceil(box.width)));
                    svg.setAttribute('height', String(Math.ceil(box.height)));
                    await document.fonts.ready;
                    const size = container.getBoundingClientRect();
                    const width = Math.ceil(size.width + 48), height = Math.ceil(size.height + 48);
                    const textSizes = [...svg.querySelectorAll('text')]
                        .filter(element => element.textContent.trim())
                        .map(element => parseFloat(getComputedStyle(element).fontSize))
                        .filter(size => Number.isFinite(size) && size > 0);
                    layout = {node_spacing: nodeSpacing, rank_spacing: rankSpacing,
                        font_size: textSizes.length ? Math.min(...textSizes) : 18};
                    if (width <= limits.width && height <= limits.height &&
                        width * height <= limits.pixels) break;
                    }
                    return {graph, layout};
                }""",
                    {
                        "source": source,
                        "limits": {"width": MAX_WIDTH, "height": MAX_HEIGHT, "pixels": MAX_PIXELS},
                    },
                )
                size = await page.evaluate("""() => {
                    const box = document.getElementById('diagram').getBoundingClientRect();
                    return {width: box.width + 48, height: box.height + 48};
                }""")
                width, height = _dimensions(size["width"], size["height"])
                png = await page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": width, "height": height},
                    full_page=True,
                    animations="disabled",
                    timeout=10_000,
                )
    except (TimeoutError, BrowserTimeout) as error:
        raise VisualRenderError(
            "Flowchart rendering exceeded 30 seconds; simplify nodes and edges.", code="timeout"
        ) from error
    except BrowserError as error:
        detail = str(error).split("\n")[:8]
        raise VisualRenderError(
            "Flowchart rendering failed; correct Mermaid syntax or split a complex flow: " + "\n".join(detail)[:1200],
            code="render_failed",
        ) from error
    return _save(output_dir, "flowchart", source, png, width, height) | {
        "graph_index": rendered_graph["graph"],
        "layout": rendered_graph["layout"],
    }
