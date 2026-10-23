"""Interrupted directory research has a separate, revision-bound resume action."""

import json

import pytest
from playwright.async_api import expect
from test_directory_research_ui import research_record, show_research

pytest_plugins = ("test_workspace_task_navigation",)


@pytest.mark.parametrize("reason", ["model_timeout", "time_limit"])
async def test_restored_timeout_draft_can_resume_without_confirming_or_regenerating(task_page, reason):
    page, _, states, errors = task_page
    states["a"]["files"] = [{"id": "source-a", "name": "招标文件.docx", "size": 12000, "role": "source"}]
    research = research_record("partial") | {"termination_reason": reason}
    page = await show_research(task_page, research, confirmable=False)
    button = page.get_by_role("button", name="从中断处继续研究", exact=True)
    await expect(button).to_be_enabled()
    await expect(page.get_by_role("button", name="确认目录，生成文档", exact=True)).to_be_disabled()
    await button.click()
    await page.wait_for_function("streams.length === 1")
    request = json.loads(await page.evaluate("streams[0].options.body"))
    assert request["action"] == "resume_directory_research"
    assert request["selection_revision"] == "research-directory"
    assert request["text"] == "从中断处继续资料研究"
    # Session-owned attachments remain authoritative; resuming cannot replace them.
    assert "files" not in request
    await expect(button).to_be_disabled()
    await page.evaluate("""() => streams[0].emit('directory.review', {
      phase: 'directory_review', revision: 'completed-directory', title: '技术标',
      directory: {sections: [{number: '1', title: '技术响应方案', level: 1}], confirmable: true,
        research: {status: 'ready', summary: '研究完成', files: [], calls: 10, rounds: 2}}
    })""")
    await page.evaluate("streams[0].control.close()")
    reviews = page.locator('.directory-review')
    await expect(reviews).to_have_count(2)
    await expect(reviews.first.get_by_role('button', name='从中断处继续研究', exact=True)).to_be_disabled()
    await expect(reviews.last.get_by_role('button', name='从中断处继续研究', exact=True)).to_have_count(0)
    await expect(reviews.last.get_by_role('button', name='确认目录，生成文档', exact=True)).to_be_enabled()
    assert errors == []


@pytest.mark.parametrize("reason,confirmable", [(None, False), ("tool_error", False), ("call_limit", False), ("model_timeout", True)])
async def test_resume_is_not_offered_for_other_incomplete_or_confirmable_drafts(task_page, reason, confirmable):
    research = research_record("ready" if confirmable else "partial") | {"termination_reason": reason}
    page = await show_research(task_page, research, confirmable=confirmable)
    await expect(page.get_by_role('button', name='从中断处继续研究', exact=True)).to_have_count(0)
    assert task_page[3] == []
