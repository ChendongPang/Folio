"""Show actual planning reads and keep incomplete research out of confirmation."""

from copy import deepcopy

from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


def research_record(status="ready"):
    return {
        "status": status,
        "summary": "已研究技术需求与评标要求，目录对应相关要求。",
        "unresolved": [],
        "calls": 9,
        "rounds": 2,
        "files": [
            {
                "path": "/private/workspace/0123456789abcdef0123456789abcdef/招标文件.docx",
                "inspected": True,
                "read": True,
                "characters": 24000,
                "source_version": "private-source-hash",
                "readings": [
                    {"label": "第五章 技术需求书", "characters": 19000},
                    {"label": "第三章 评标办法", "characters": 5000},
                ],
            },
            {"path": "旧项目参考.pdf", "inspected": True, "read": False},
            {"path": "补充材料.xlsx", "inspected": False, "read": False},
        ],
    }


async def show_research(task_page, research=None, *, confirmable=True):
    page, _, states, _ = task_page
    directory = {"sections": [{"number": "1", "title": "技术响应方案", "level": 1}]}
    if research is not None:
        directory.update(research=deepcopy(research), confirmable=confirmable)
    states["a"].update(
        active_job=None,
        active_job_status=None,
        directory_review={
            "phase": "directory_review",
            "revision": "research-directory",
            "title": "技术标",
            "directory": directory,
        },
    )
    await page.reload()
    await expect(page.locator("#directory-toc")).to_contain_text("技术响应方案")
    return page


async def test_reading_record_distinguishes_structure_and_original_reads(task_page):
    page = await show_research(task_page, research_record())
    record = page.locator(".directory-research")
    await expect(record.locator("summary")).to_contain_text("已完成目录研究")
    await expect(record).not_to_have_attribute("open", "")
    await record.locator("summary").click()
    await expect(record).to_contain_text("3 份资料，已读取其中 1 份的原文")
    await expect(record).to_contain_text("第五章 技术需求书 · 19,000 字符")
    files = record.locator(".research-files > li")
    await expect(files.nth(0)).to_contain_text("已查看结构 · 已读取原文 · 24,000 字符")
    await expect(files.nth(1)).to_contain_text("已查看结构 · 未读取原文")
    await expect(files.nth(2)).to_contain_text("未查看结构 · 未读取原文")
    await expect(record).to_contain_text("不代表全文读完或理解率")
    displayed = await record.inner_text()
    assert "/private/workspace" not in displayed
    assert "0123456789abcdef" not in displayed
    assert "private-source-hash" not in displayed
    assert task_page[3] == []


async def test_partial_research_is_visible_and_cannot_be_confirmed(task_page):
    research = research_record("partial")
    research.update(
        summary="技术需求已经读取，包件范围尚未明确。",
        unresolved=["请明确本次投标针对包件一还是包件二。"],
    )
    page = await show_research(task_page, research, confirmable=False)
    record = page.locator(".directory-research")
    await expect(record).to_have_attribute("open", "")
    await expect(record).to_contain_text("研究尚未充分")
    await expect(record).to_contain_text("请明确本次投标针对包件一还是包件二")
    await expect(record).to_contain_text("当前目录仍是初稿")
    await expect(page.get_by_role("button", name="确认目录，生成文档")).to_be_disabled()
    await expect(page.get_by_role("button", name="重新生成目录", exact=True)).to_be_enabled()
    assert task_page[3] == []


async def test_without_sources_does_not_claim_sources_were_read(task_page):
    page = await show_research(task_page, {
        "status": "no_sources",
        "summary": "本次没有业务资料，目录仅依据用户需求规划。",
        "unresolved": [], "files": [], "calls": 0, "rounds": 0,
    })
    record = page.locator(".directory-research")
    await expect(record.locator("summary")).to_contain_text("未使用业务资料")
    await record.locator("summary").click()
    await expect(record).to_contain_text("目录仅依据用户需求规划")
    await expect(record.locator(".research-count")).to_have_count(0)
    await expect(record.locator(".research-files")).to_have_count(0)
    await expect(page.get_by_role("button", name="确认目录，生成文档")).to_be_enabled()
    assert task_page[3] == []


async def test_legacy_directory_without_research_remains_usable(task_page):
    page = await show_research(task_page)
    await expect(page.locator(".directory-research")).to_have_count(0)
    await expect(page.get_by_role("button", name="确认目录，生成文档")).to_be_enabled()
    assert task_page[3] == []


async def test_directory_web_record_shows_query_purpose_cache_failures_and_source_links(task_page):
    research = research_record()
    research["web_research"] = {
        "available": True,
        "searches": [
            {"query": "公开认证要求", "purpose": "补充目录的认证范围说明", "status": "completed",
             "cached": False, "source_count": 1, "evidence_ids": ["source-1"]},
            {"query": "公开认证要求", "purpose": "复核合规章节命名", "status": "completed",
             "cached": True, "source_count": 1, "evidence_ids": ["source-1"]},
            {"query": "其他标准", "purpose": "核对其他适用范围", "status": "failed",
             "error_code": "search_failed", "evidence_ids": []},
        ],
        "evidence": [{"evidence_id": "source-1", "title": "官方认证说明", "url": "https://example.org/standard",
                      "retrieved_at": "2026-10-21T09:00:00+00:00"}],
        "findings": [{"text": "目录补充认证适用范围的说明。", "evidence_ids": ["source-1"]}],
    }
    page = await show_research(task_page, research)
    record = page.locator(".directory-research")
    await record.locator("summary").click()
    web = record.get_by_role("region", name="联网补充记录")
    await expect(web).to_contain_text("联网补充 · 3 次查询")
    await expect(web).to_contain_text("目录用途：补充目录的认证范围说明")
    await expect(web).to_contain_text("搜索完成 · 1 个公开来源")
    await expect(web).to_contain_text("已复用缓存 · 1 个公开来源")
    await expect(web).to_contain_text("搜索未成功")
    await expect(web.get_by_role("link", name="官方认证说明").first).to_have_attribute("href", "https://example.org/standard")
    await expect(web.get_by_role("link", name="官方认证说明").first).to_have_attribute("rel", "noopener noreferrer")
    await expect(web).to_contain_text("获取于 2026-10-21T09:00:00+00:00")
    await expect(web).to_contain_text("不能替代上传资料或证明用户项目事实")
    await expect(web).to_contain_text("目录补充认证适用范围的说明")
    assert task_page[3] == []
