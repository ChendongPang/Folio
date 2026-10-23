"""Persisted review progress reaches both live and restored workspace views."""

import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from folio.api.app import create_app
from folio.models import JobInput


def sse_events(response):
    events = []
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if kind and data:
            events.append((kind, json.loads(data)))
    return events


def review_job(app, inputs):
    store = app.state.store
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    started = {
        "phase": "consistency",
        "status": "started",
        "round_id": "round-1",
        "unit_id": "group-2",
        "index": 2,
        "total": 4,
        "sections": [
            {"section_id": "n1", "title": "交易录入", "revision": 1},
            {"section_id": "n2", "title": "交易撤销", "revision": 2},
        ],
        "reasons": ["核对两个章节的审批角色是否一致"],
    }
    completed = {
        "phase": "consistency",
        "status": "completed",
        "round_id": "round-1",
        "unit_id": "group-2",
        "completed": 2,
        "findings": [
            {"finding_id": "f1", "description": "审批角色可能不一致", "section_ids": ["n1", "n2"]}
        ],
        "elapsed_ms": 1250,
        "calls": {
            "model_calls": 2,
            "cache_hits": 1,
            "failed_calls": 0,
            "tasks": {"review": {"model_calls": 2, "cache_hits": 1, "failed_calls": 0}},
        },
    }
    summary = {
        "phase": "consistency",
        "status": "summary",
        "step": "completed",
        "round_id": "round-1",
        "completed": 4,
        "total": 4,
        "reason": "已检查全部分组，发现一项疑点，接下来核对原文。",
    }
    store.work_event(job_id, "正在整理正文。")
    # Fixture persists the exact optional contract without depending on the
    # producer's convenience signature; unknown internal fields must not leak.
    with store.connect() as conn:
        for text, review in [
            ("正在比较交易录入和交易撤销。", started),
            ("第 2 组已完成，发现一项疑点。", completed),
            ("本轮一致性检查已完成。", summary),
        ]:
            store._event(
                conn,
                job_id,
                "work_update",
                {"text": text, "review": review, "request": "private-raw-request"},
            )
    store.update(job_id, status="failed", stage="review", error="test stopped without model")
    return job_id, started, completed, summary


def test_review_event_details_persist_and_replay_with_cursor(tmp_path, inputs):
    root = tmp_path / "web"
    app = create_app(root)
    job_id, started, completed, summary = review_job(app, inputs)
    with TestClient(app) as client:
        first = client.get(f"/jobs/{job_id}/stream")
        assert first.status_code == 200
        events = sse_events(first)
        rich = [data for kind, data in events if kind == "work.update" and "review" in data]
        assert [data["review"] for data in rich] == [started, completed, summary]
        legacy = next(data for _, data in events if data.get("text") == "正在整理正文。")
        assert set(legacy) == {"text", "event_id"}
        assert "private-raw-request" not in first.text
        cursor = client.get(f"/jobs/{job_id}/stream?after={rich[1]['event_id']}")
        assert [data["review"] for _, data in sse_events(cursor) if "review" in data] == [summary]
    # A new app/Store reconstructs details entirely from disk, including on a
    # terminal job. No in-memory subscription state or browser storage is needed.
    with TestClient(create_app(root)) as client:
        assert sse_events(client.get(f"/jobs/{job_id}/stream")) == events


async def test_review_rows_update_safely_and_restore_in_browser(tmp_path, inputs):
    app = create_app(tmp_path / "browser")
    job_id, started, completed, _ = review_job(app, inputs)
    async with async_playwright() as playwright:
        executable = os.environ.get("FOLIO_CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install local Chromium for browser integration tests")
        with TestClient(app) as client:
            cid = client.post("/conversations").json()["id"]
            # Use the real saved-conversation restoration endpoint and event log.
            with app.state.store.connect() as conn:
                state = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
                state["active_job"] = job_id
                conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
            browser = await playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1100, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def serve(route):
                    parsed = urlsplit(route.request.url)
                    assert parsed.netloc == "testserver", "No external request is permitted"
                    response = client.request(
                        route.request.method,
                        parsed.path + ("?" + parsed.query if parsed.query else ""),
                        content=route.request.post_data_buffer,
                        headers={"content-type": route.request.headers.get("content-type", "application/json")},
                    )
                    await route.fulfill(status=response.status_code, body=response.content, content_type=response.headers.get("content-type", "text/plain"))

                await page.route("**/*", serve)
                await page.add_init_script(f"localStorage.setItem('folio.conversation', {json.dumps(cid)})")
                await page.add_init_script(r"""
                    const actualFetch = window.fetch;
                    window.fetch = async (url, options = {}) => {
                      if (/\/conversations\/[^/]+\/messages/.test(url)) {
                        const stream = new ReadableStream({start(control) {
                          window.reviewStream = {
                            receive(type, data) {
                              control.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
                            }
                          };
                        }});
                        return new Response(stream, {headers: {'Content-Type': 'text/event-stream'}});
                      }
                      return actualFetch(url, options);
                    };
                """)
                await page.goto("http://testserver/")
                # Saved history loads only when the user expands the process.
                await page.locator(".activity").first.locator(":scope > summary").click()
                await expect(page.locator(".review-activity")).to_have_count(2)  # unit + summary
                await expect(page.locator("#message-input")).to_be_enabled()
                if not await page.locator(".activity").first.evaluate("node => node.open"):
                    await page.locator(".activity").first.locator(":scope > summary").click()
                unit = page.locator(".review-activity").first
                await unit.locator("summary").click()
                assert await unit.evaluate("node => node.open")
                assert "交易录入（第 1 版）" in await unit.inner_text()
                assert "核对两个章节的审批角色是否一致" in await unit.inner_text()
                assert "模型任务调用 2 次 · 复用缓存 1 次 · 失败调用 0 次" in await unit.inner_text()
                assert "1.3 秒" in await unit.inner_text()
                assert "跨章节一致性检查 · 已结束" in await page.locator(".review-activity").nth(1).text_content()

                # Simulate concurrent units finishing out of order. The original
                # row and its open state survive, and partial updates keep details.
                hostile = deepcopy(started)
                hostile["round_id"] = "live-round"
                hostile["sections"][0]["title"] = '<img src=x onerror="window.injected=true">'
                await page.locator("#message-input").fill("请核查章节一致性")
                await page.locator("#send-button").click()
                await page.wait_for_function("Boolean(window.reviewStream)")
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9001, text: '开始核查', review})", hostile)
                live_unit = page.locator(".review-activity").nth(2)
                await live_unit.locator("summary").click()
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9004, text: '另一组正在检查', review})", hostile | {"unit_id": "group-3"})
                final = completed | {"round_id": "live-round", "phase": "drill_down", "outcome": "dismissed", "reason": "原文说明审批角色相同，疑点不成立。"}
                await page.evaluate("review => { reviewStream.receive('work.update', {event_id: 9002, text: '核查完成，疑点不成立', review}); reviewStream.receive('work.update', {event_id: 9002, text: '重复推送', review}); }", final)
                await expect(page.locator(".review-activity")).to_have_count(4)
                await expect(live_unit).to_contain_text("模型判定疑点不成立")
                assert await live_unit.evaluate("node => node.open")
                assert await live_unit.evaluate("node => node.parentElement.classList.contains('is-current')")
                assert "模型判定疑点不成立" in await live_unit.inner_text()
                assert hostile["sections"][0]["title"] in await live_unit.inner_text()
                assert await live_unit.locator("img").count() == 0
                assert await page.evaluate("window.injected === undefined")
                assert "重复推送" not in await live_unit.inner_text()

                # Same unit in another round must remain a separate audit row.
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9003, text: '第二轮核查', review})", started | {"round_id": "second-round"})
                await expect(page.locator(".review-activity")).to_have_count(5)
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9005, text: '这组检查未完成，已保留进度', review})", {"round_id": "live-round", "unit_id": "group-3", "status": "failed"})
                await expect(page.locator(".activity-item.is-failed")).to_have_count(1)
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9006, text: '规则校验仍失败，保留待确认', review})", {"round_id": "live-round", "unit_id": "group-3", "status": "completed", "outcome": "hard_check_retained"})
                await expect(page.locator(".review-activity").nth(3)).to_contain_text("模型认为不成立，但结构或来源校验仍失败，保留待确认")
                await page.evaluate("review => reviewStream.receive('work.update', {event_id: 9007, text: '开始下一轮检查', review})", {"phase": "consistency", "round_id": "next-round", "status": "summary", "step": "started"})
                await expect(page.locator(".review-activity").last).to_contain_text("跨章节一致性检查 · 开始")
                await page.reload()
                # Saved history loads only when the user expands the process.
                await page.locator(".activity").first.locator(":scope > summary").click()
                await expect(page.locator(".review-activity")).to_have_count(2)
                if not await page.locator(".activity").first.evaluate("node => node.open"):
                    await page.locator(".activity").first.locator(":scope > summary").click()
                await page.locator(".review-activity").first.locator("summary").click()
                assert "审批角色可能不一致" in await page.locator(".review-activity").first.inner_text()
                await page.screenshot(path=str(tmp_path / "review-progress.png"))
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert errors == []
            finally:
                await browser.close()
