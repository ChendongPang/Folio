"""Chat waits quietly; explicit document tasks retain their execution record."""

import re

import pytest
from playwright.async_api import expect

pytest_plugins = ("test_workspace_task_navigation",)


async def start_chat(task_page, text="你好，你能做什么任务？"):
    page, _, _, _ = task_page
    await page.locator("#session-list button").filter(has_text="普通会话 B").click()
    await page.locator("#message-input").fill(text)
    await page.locator("#message-input").press("Enter")
    await page.wait_for_function("streams.length === 2")
    return page, page.locator("#messages .assistant-message").last


async def emit(page, event, data):
    await page.evaluate("([event, data]) => streams.at(-1).emit(event, data)", [event, data])


async def test_chat_waiting_animation_survives_routing_and_stops_at_first_text(task_page):
    page, response = await start_chat(task_page)
    await expect(response.locator(".agent-working")).to_be_visible()
    await expect(response.locator(".agent-working-label")).to_have_text("正在思考...")
    await expect(response.locator(".working-orbit i")).to_have_count(3)
    await emit(page, "message.started", {})
    await expect(response.locator(".agent-working-label")).to_have_text("正在思考...")

    for activity_id, text in [
        ("intent", "正在理解你的请求，判断需要执行的操作。"),
        ("reply", "正在整理本次对话与可用资料，准备回答。"),
    ]:
        await emit(page, "message.update", {
            "activity": {"id": activity_id, "text": text, "status": "running"},
            "model_status": "thinking", "elapsed_seconds": 5, "step_elapsed_seconds": 2,
        })
        await expect(response.locator(".agent-working")).to_be_visible()
        await expect(response.locator(".agent-working-label")).to_have_text("正在思考...")
        await expect(response.locator(".activity")).to_have_count(0)
        await expect(response.locator(".message-text")).to_have_count(0)

    # A transport heartbeat or an empty chunk is not actual answer text.
    await emit(page, "message.delta", {"text": ""})
    await expect(response.locator(".agent-working")).to_be_visible()
    await expect(response.locator(".agent-working-label")).to_have_text(
        "正在思考..."
    )
    await emit(page, "message.delta", {"text": "我可以帮你"})
    await expect(response.locator(".message-text")).to_have_text("我可以帮你")
    await expect(response.locator(".agent-working")).to_have_count(0)
    await expect(response.locator(".activity")).to_have_count(0)

    # A delayed progress update must not replace the streamed answer or restore a box.
    await emit(page, "message.update", {
        "activity": {"id": "late", "text": "迟到的回答准备状态", "status": "running"},
        "model_status": "waiting", "elapsed_seconds": 8,
    })
    await emit(page, "message.delta", {"text": "整理资料并生成文档。"})
    answer = "我可以帮你整理资料并生成文档。"
    await expect(response.locator(".message-text")).to_have_text(answer)
    await expect(response.locator(".agent-working")).to_have_count(0)
    await expect(response.locator(".activity")).to_have_count(0)
    await emit(page, "message.completed", {"text": answer})
    await page.evaluate("streams.at(-1).control.close()")
    await expect(page.locator("#message-input")).to_be_enabled()
    await expect(response.locator(".message-text")).to_have_text(answer)
    await expect(response.locator(".agent-working")).to_have_count(0)
    await expect(response.locator(".activity")).to_have_count(0)
    assert task_page[3] == []


async def test_english_directory_progress_localizes_spinner_and_source_labels(task_page):
    page, _, _, errors = task_page
    await page.locator("#language-select").select_option("en")
    page, response = await start_chat(task_page, "Process this source and prepare the outline.")
    updates = [
        ("intent", "正在理解你的请求，判断需要执行的操作。"),
        ("list", "正在准备资料清单和结构索引，随后研究相关原文再规划目录。"),
        ("prepare", "正在准备资料目录与写作要求。"),
        ("catalog", "正在整理上传资料的文件清单和内部结构，准备选择需要阅读的内容。"),
        ("inspect", "正在查看《DECC.pdf》的内部结构，定位需要阅读的章节或页面。本步目的：Locate proposal instructions."),
        ("read", "正在读取《DECC.pdf》的正文，核对目录需要覆盖的要求。本步目的：Read the complete RFP."),
        ("read-result", "已取得《DECC.pdf》的正文原文，本批 5687 字符（第 3 页；第 1—1 段 / 共 1 段），正在归纳本批原文，提取目录需要覆盖的要求。"),
        ("inspect-result", "已获取《DECC.pdf》的 3 条内部结构信息，正在判断需要阅读的内容。"),
        ("list-again", "正在浏览资料文件清单，查找与本次文档有关的文件。本步目的：Check the source catalog for page-extraction warnings."),
        ("research-check", "正在核对资料研究结果：检查目录要求是否引用了已读原文、资料来源是否一致。"),
        ("plan-wait", "正在等待模型返回完整目录（第 1 轮），尚未生成正文。"),
        ("plan-returned", "模型已返回目录，正在核对标题层级、节点用途和重复章节。"),
        ("coverage", "正在对照已读取的资料要求，检查目录是否有明确的写作安排。"),
        ("render", "目录校验通过，共 61 个节点，正在排版 Word 预览。"),
        ("supplement", "正在将补充内容整合到《Migration Execution and Validation》。"),
        ("supplement-select", "《Migration Execution and Validation》第 1/4 项：正在筛选相关约束和必要原文。"),
        ("supplement-done", "《Migration Execution and Validation》补充内容已整合 1/4 项。"),
        ("focused", "“Itemized Implementation Estimate”局部检查完成：2 项需处理的问题。"),
        ("retry-failed", "《2.9.1 Content Inventory and Disposition》提取事实要点第 1 次未通过：模型返回的内容未通过必要的数据结构或内容校验。随后已发起重试。"),
        ("retry-started", "《2.9.1 Content Inventory and Disposition》正在进行第 2 次提取事实要点，修正上次未完成的结果。"),
        ("future-step", "新增但尚未登记翻译的中文步骤。"),
    ]
    for activity_id, text in updates:
        await emit(page, "directory.update", {
            "activity": {"id": activity_id, "text": text, "status": "running"},
            "elapsed_seconds": 15,
            "_translations": {"新增但尚未登记翻译的中文步骤。": "A newly supported research step."},
        })

    activity_text = await response.locator(".activity").inner_text()
    assert not re.search(r"[\u3400-\u9fff]", activity_text), activity_text
    assert "Inspecting the structure of “DECC.pdf”" in activity_text
    assert "Reading the body of “DECC.pdf”" in activity_text
    assert "Read 5687 characters from the body of “DECC.pdf”" in activity_text
    assert "Inspected 3 structure items in “DECC.pdf”" in activity_text
    assert errors == []


async def test_english_directory_failure_message_is_localized(task_page):
    page, _, _, errors = task_page
    await page.locator("#language-select").select_option("en")
    page, response = await start_chat(task_page, "Prepare the document outline.")
    await emit(page, "message.failed", {
        "text": "目录规划未完成，未启动正文生成。模型未正常完成回答，请检查连接或稍后重试。",
        "diagnostics": {"stage": "directory_planning", "code": "model_error"},
    })

    await expect(response.locator(".message-text")).to_have_text(
        "Outline planning did not finish, so body generation was not started. "
        "The model did not complete its response. Check the connection or try again later."
    )
    assert errors == []


async def test_body_task_displays_completed_usage_totals(task_page):
    page, _, _, errors = task_page
    await page.locator("#language-select").select_option("en")
    page, response = await start_chat(task_page, "Generate the confirmed document.")
    await emit(page, "work.update", {
        "text": "Task “Technical Volume” completed.",
        "task_usage": [{
            "task_id": "technical_volume",
            "title": "Technical Volume",
            "elapsed_ms": 3100,
            "started_at": "2026-10-23T00:00:00Z",
            "finished_at": "2026-10-23T00:00:03.100Z",
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "model_calls": 4,
        }],
    })

    usage = response.locator(".task-usage")
    await expect(usage.locator("summary")).to_contain_text("3s · 1,500 Token")
    await expect(usage).to_contain_text("Technical Volume")
    await expect(usage).to_contain_text("Input tokens: 1,200")
    await expect(usage).to_contain_text("Output tokens: 300")
    assert errors == []


async def test_body_failure_explains_validation_limit_in_english(task_page):
    page, _, _, errors = task_page
    await page.locator("#language-select").select_option("en")
    page, response = await start_chat(task_page, "Generate the confirmed document.")
    await emit(page, "job.failed", {
        "text": (
            "《2.10 Analytics and Search Optimization》的提取事实要点未完成："
            "模型返回的内容未通过必要的数据结构或内容校验。已尝试 2 次。"
            "已保存 25/41 个正文章节，已有内容和检索进度保留。"
        ),
        "failure": {
            "stage_label": "正文生成与章节核验",
            "action_label": "提取事实要点",
            "task_title": "Analytics, Training and Ongoing Operations",
            "section_labels": ["2.10 Analytics and Search Optimization"],
            "attempts": 2,
            "saved_sections": 25,
            "total_sections": 41,
            "code": "invalid_output",
            "reason": "模型返回的内容未通过必要的数据结构或内容校验。",
            "progress_note": "已保存 25/41 个正文章节，已有内容和检索进度保留。",
        },
    })

    await expect(response.locator(".message-text")).to_contain_text(
        "Extract facts did not complete for “2.10 Analytics and Search Optimization”"
    )
    await expect(response.locator(".failure-reason")).to_have_text(
        "The model output did not pass the required structure or content validation."
    )
    await expect(response.locator(".failure-fields").first).to_contain_text(
        "Body generation and section verification · Extract facts"
    )
    assert errors == []


@pytest.mark.parametrize("interrupted", [False, True])
async def test_saved_chat_history_does_not_restore_a_task_box(task_page, interrupted):
    page, _, states, errors = task_page
    states["a"].update(active_job=None, active_job_status=None)
    states["a"]["messages"] = [
        {"role": "user", "content": "你好，你能做什么任务？"},
        {
            "role": "assistant", "content": "我可以帮你整理资料并生成文档。",
            "activity_label": "处理过程", "interrupted": interrupted,
            "activities": [
                {"id": "intent", "text": "已理解请求", "status": "completed"},
                {"id": "reply", "text": "正在准备回答", "status": "running" if interrupted else "completed"},
            ],
        },
    ]
    await page.reload()
    response = page.locator("#messages .assistant-message").last
    await expect(response).to_contain_text("我可以帮你整理资料并生成文档。")
    if interrupted:
        await expect(response).to_contain_text("此回答未完整结束")
    await expect(response.locator(".activity")).to_have_count(0)
    await expect(response.locator(".agent-working")).to_have_count(0)
    await expect(page.locator("#message-input")).to_be_enabled()
    assert errors == []


@pytest.mark.parametrize("event", ["style.update", "directory.update"])
async def test_document_task_progress_is_above_its_response(task_page, event):
    page, response = await start_chat(task_page, "请根据资料处理文档")
    await emit(page, "message.update", {
        "activity": {"id": "intent", "text": "已确定处理文档", "status": "completed"},
    })
    await expect(response.locator(".activity")).to_have_count(0)
    await emit(page, event, {
        "activity": {"id": "task", "text": "正在阅读资料并执行文档任务", "status": "running"},
        "model_status": "thinking", "elapsed_seconds": 3,
    })
    activity = response.locator(".activity")
    await expect(activity).to_be_visible()
    await expect(activity.locator(".activity-item")).to_have_count(2)
    await expect(activity.locator(".activity-wait")).to_be_visible()
    await expect(response.locator(".agent-working")).to_have_count(0)

    if event == "style.update":
        # Receiving the actual explanation ends the previous analysis step even
        # when an older backend has not sent a separate style.completed event.
        await emit(page, "message.delta", {"text": "示例采用正式、客观的写法。"})
        await expect(response.locator(".message-text")).to_have_text("示例采用正式、客观的写法。")
        await expect(activity).not_to_have_attribute("open", "")
        await expect(activity.locator(".activity-wait")).to_have_count(0)
        await expect(activity.locator(".is-current")).to_have_count(0)
        await expect(activity.locator(".activity-check")).to_have_count(2)
        await expect(response.locator(".agent-working")).to_have_count(0)
    else:
        await emit(page, "directory.review", {
            "phase": "directory_review", "revision": "new-directory", "title": "文档",
            "reason": "目录已生成，请确认章节安排。",
            "directory": {"sections": [{"number": "1", "title": "项目概述", "level": 1}]},
        })
        await expect(response.locator(".message-text")).to_have_text("目录已生成，请确认章节安排。")
    assert await activity.evaluate("""element => {
      const text = element.parentElement.querySelector('.message-text');
      return Boolean(text && element.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    assert task_page[3] == []
