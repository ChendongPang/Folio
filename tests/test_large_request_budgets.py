"""Requests that fit the configured capacity retain full source and output data."""

import json

import pytest

from folio.api.workspace import ConversationDraft, MessageInput, source_material_context
from folio.models import JobInput
from folio.orchestration.engine import Engine
from folio.orchestration.work import BusinessRecord
from folio.runtime.demo import DemoRuntime
from folio.visuals.workflow import VisualSource, visual_context


async def test_business_reader_keeps_more_than_twenty_sources_in_one_fitting_request(store, inputs):
    class Reader(DemoRuntime):
        def __init__(self):
            self.reads = []

        async def measure(self, task, context, schema, char_limit):
            return {"characters": len(json.dumps(context, ensure_ascii=False)), "fits": True}

        async def complete(self, task, context, schema, session):
            if task == "understand_work":
                self.reads.append(context)
            return await super().complete(task, context, schema, session)

    runtime = Reader()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    evidence = [{"evidence_id": f"e{index}", "source_id": "business-rules", "source_version": "v1",
                 "locator": str(index), "text": f"业务规则{index}：交易撤销需要主管审批。"}
                for index in range(35)]
    result = await engine.call("understand_work", {
        "work_task": {"objective": "完整理解交易规则"}, "evidence": evidence,
    }, BusinessRecord)

    assert len(runtime.reads) == 1
    assert [(item["evidence_id"], item["text"]) for item in runtime.reads[0]["evidence"]] == [
        (item["evidence_id"], item["text"]) for item in evidence
    ]
    assert {ref for fact in result.facts for ref in fact.evidence_ids} == {
        item["evidence_id"] for item in evidence
    }


def test_workspace_drafts_messages_and_revision_advice_keep_long_user_requirements(inputs):
    text = "请逐项说明交易的审批、执行、撤销和异常处理规则。\n" * 5000 + "最后要求：保留审计记录。"
    assert 100000 < len(text) < 600000
    draft = ConversationDraft.model_validate({"text": text})
    message = MessageInput.model_validate({"text": text, "action": "revise_directory", "advice": text})
    assert draft.text == message.text == message.advice == text
    job = JobInput(**(inputs | {"user_request": text}), runtime="demo")
    assert job.user_request == text


def test_source_chat_context_keeps_full_navigation_and_text_within_budget(tmp_path):
    path = tmp_path / "business-rules.md"
    body = "\n\n".join(
        f"## 业务章节{index}\n\n" + f"第{index}章要求：交易应先审批，再执行并记录审计日志。" * 20
        for index in range(30)
    ) + "\n\n最后业务要求：拒绝时必须记录理由。"
    path.write_text(body, encoding="utf-8")

    parsed = source_material_context(path, cache_root=tmp_path / "source-cache")

    assert parsed is not None
    assert len(parsed["nodes"]) > 20
    assert len(parsed["excerpt"]) > 6000
    assert len(json.dumps(parsed, ensure_ascii=False)) < 600000
    assert parsed["structure_truncated"] is False
    assert parsed["excerpt_truncated"] is False
    assert {f"业务章节{index}" for index in range(30)} <= {
        node["title"] for node in parsed["nodes"]
    }
    assert "最后业务要求：拒绝时必须记录理由。" in parsed["excerpt"]


def test_visual_source_preserves_source_beyond_the_old_24k_limit():
    # A full SVG with many labelled nodes can legitimately exceed 24K characters.
    lines = ['<svg xmlns="http://www.w3.org/2000/svg">']
    lines.extend(f'<text x="10" y="{index * 20}">审批节点{index}：记录业务处理结果</text>'
                 for index in range(600))
    lines.append("</svg>")
    source = "\n".join(lines)
    assert len(source) > 24000
    assert VisualSource(lines=lines).source == source


async def test_visual_context_uses_actual_capacity_without_the_old_12k_gate():
    context = {
        "section": {"title": "审批流程", "number": "1"},
        "visual": {"figure_id": "approval", "kind": "flowchart", "caption": "审批流程"},
        "source_blocks": [{"block_id": "b1", "text": "录入后审批，拒绝则退回。" * 2000}],
        "origin": "evidence",
    }
    measurements = []

    async def measure(task, request, model):
        measurements.append((task, request, model))
        return {"characters": len(json.dumps(request, ensure_ascii=False)), "fits": True}

    async def call(*args):
        pytest.fail("A measured fitting visual request must not trigger summary/model calls")

    assert 12000 < len(json.dumps(context, ensure_ascii=False)) < 600000
    result = await visual_context(context, call, measure)
    assert result is context
    assert measurements == [("design_ui", context, VisualSource)]
