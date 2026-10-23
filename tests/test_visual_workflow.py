import asyncio
import json
import struct
import zlib
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError

from folio.document.presentation import normalize_content, validate_document_content
from folio.models import (
    Block,
    DocumentPlan,
    Extraction,
    Section,
    SectionContent,
    SectionState,
    VisualRequest,
)
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.visuals.workflow import VisualReview, VisualSource, build_visuals, load_visuals


def review_result(*, problems):
    from folio.visuals.review import VisualFinding

    return VisualReview(
        problems=problems,
        findings=[
            VisualFinding(
                kind="semantic",
                source_block_id="b1",
                source_quote="录入后审批，拒绝则退回。",
                description=problem,
            )
            for problem in problems
        ],
    )


def png(path):
    def chunk(tag, value):
        return (
            struct.pack("!I", len(value)) + tag + value + struct.pack("!I", zlib.crc32(tag + value))
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


def request(**kwargs):
    return VisualRequest(
        figure_id="v1",
        kind="flowchart",
        caption="审批流程",
        description="录入后审批，拒绝则退回",
        source_block_ids=["b1"],
        after_block_id="b1",
        **kwargs,
    )


def content():
    return SectionContent(
        blocks=[Block(block_id="b1", text="录入后审批，拒绝则退回。", origin="model_proposal")],
        visuals=[request()],
    )


def material():
    section = Section(
        section_id="s1",
        node_id="n1",
        title="审批",
        level=1,
        display_number="1",
        instruction="说明审批并画流程图",
    )
    return DocumentPlan(sections=[section]), [
        SectionState(
            section_id="s1",
            content=content(),
            extraction=Extraction(summary="审批流程"),
            evidence=[],
        )
    ]


def several_figures(count, *, same_identity=False):
    plan, states = material()
    sections, copies = [], []
    for index in range(count):
        section = plan.sections[0].model_copy(deep=True)
        section.section_id = f"s{index + 1}"
        if not same_identity:
            section.display_number = str(index + 1)
        state = states[0].model_copy(deep=True)
        state.section_id = section.section_id
        sections.append(section)
        copies.append(state)
    return DocumentPlan(sections=sections), copies


@pytest.fixture
def fake_renderer(monkeypatch):
    from folio.visuals import render

    calls = []

    async def renderer(source, folder):
        calls.append(source)
        image = folder / "image.png"
        png(image)
        code = folder / "source.txt"
        code.write_text(source)
        return {"image_path": str(image), "source_path": str(code), "width": 1, "height": 1}

    monkeypatch.setattr(render, "render_ui", renderer)
    monkeypatch.setattr(render, "render_flowchart", renderer)
    return calls


def test_visual_contract_and_normalized_block_references():
    bad = content().model_dump()
    bad["visuals"][0]["source_block_ids"] = ["missing"]
    with pytest.raises(ValidationError):
        SectionContent.model_validate(bad)
    value = content()
    value.blocks[0].text = "1. 录入\n2. 审批"
    fixed = normalize_content(value)
    assert fixed.visuals[0].source_block_ids == ["b1", "b1_p1"]
    assert fixed.visuals[0].after_block_id == "b1_p1"
    SectionContent.model_validate(fixed.model_dump())


def test_visual_sources_keep_heading_context_but_require_actual_body():
    from folio.visuals.workflow import _visual_context

    plan, states = material()
    value = states[0].content.model_dump()
    value["blocks"].insert(0, Block(block_id="h1", kind="heading", text="审批步骤").model_dump())
    value["visuals"][0]["source_block_ids"] = ["h1", "b1"]
    normalized = normalize_content(SectionContent.model_validate(value), plan.sections[0])
    SectionContent.model_validate(normalized.model_dump())
    states[0].content = normalized
    context, _, _ = _visual_context(plan.sections[0], states[0], normalized.visuals[0])
    assert [(block["block_id"], block["kind"]) for block in context["source_blocks"]] == [
        ("h1", "heading"),
        ("b1", "paragraph"),
    ]
    for invalid in (["h1"], ["h1", "missing"], ["b1", "missing"]):
        value["visuals"][0]["source_block_ids"] = invalid
        with pytest.raises(ValidationError):
            SectionContent.model_validate(value)


def test_visual_captions_do_not_leak_internal_evidence_ids():
    value = content()
    value.visuals[0].caption = "审批流程（ev_0123456789abcdef）"
    with pytest.raises(ValueError, match="Figure captions"):
        validate_document_content(value)


async def test_visual_cache_reformat_and_stale_detection(tmp_path, fake_renderer):
    plan, states = material()
    calls = []

    async def call(task, context, model):
        calls.append(task)
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    assets = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert assets["s1"][0]["origin"] == "model_proposal"
    assert calls == ["design_flowchart", "validate_visual"]
    assert load_visuals(tmp_path, states) == assets
    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(calls) == 2 and len(fake_renderer) == 1
    states[0].content.blocks[0].text += "由主管审批。"
    with pytest.raises(ValueError, match="version"):
        load_visuals(tmp_path, states)
    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(fake_renderer) == 2
    item = load_visuals(tmp_path, states)["s1"][0]
    Path(item["image_path"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="changed"):
        load_visuals(tmp_path, states)


async def test_four_visual_workflows_share_two_renderer_slots(tmp_path, fake_renderer, monkeypatch):
    from folio.visuals import render

    plan, states = several_figures(6)
    four_started, release = asyncio.Event(), asyncio.Event()
    active = peak = rendering = render_peak = 0
    original_renderer = render.render_flowchart

    async def bounded_renderer(source, directory):
        nonlocal rendering, render_peak
        async with render._render_slots():
            rendering += 1
            render_peak = max(render_peak, rendering)
            try:
                await asyncio.sleep(0.02)
                return await original_renderer(source, directory)
            finally:
                rendering -= 1

    monkeypatch.setattr(render, "render_flowchart", bounded_renderer)

    async def call(task, context, model):
        nonlocal active, peak
        if task == "validate_visual":
            return review_result(problems=[])
        active += 1
        peak = max(peak, active)
        if active == 4:
            four_started.set()
        try:
            await release.wait()
            return VisualSource(lines=["flowchart TD", "A-->B"])
        finally:
            active -= 1

    runner = asyncio.create_task(build_visuals(plan, states, tmp_path, call, lambda _: None))
    await asyncio.wait_for(four_started.wait(), 1)
    assert active == 4
    release.set()
    result = await runner
    assert not result.issues and len(result) == 6
    assert peak == 4 and active == 0
    assert render_peak == 2 and rendering == 0
    assert len(fake_renderer) == 6


async def test_identical_figures_share_one_serial_audit_and_asset(tmp_path, fake_renderer):
    plan, states = several_figures(2, same_identity=True)
    calls = []

    async def call(task, context, model):
        calls.append(task)
        await asyncio.sleep(0.02)
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert not result.issues and set(result) == {"s1", "s2"}
    assert calls == ["design_flowchart", "validate_visual"]
    assert len(fake_renderer) == 1
    assert result["s1"][0]["image_path"] == result["s2"][0]["image_path"]
    assert len(list((tmp_path / "visuals").glob("*/attempt-1.json"))) == 1
    assert load_visuals(tmp_path, states) == result


async def test_visual_cancellation_drains_active_and_queued_workers(tmp_path, fake_renderer):
    plan, states = several_figures(6)
    four_started = asyncio.Event()
    active = 0
    drained = []

    async def call(task, context, model):
        nonlocal active
        active += 1
        if active == 4:
            four_started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            active -= 1
            drained.append(context["section"]["number"])

    runner = asyncio.create_task(build_visuals(plan, states, tmp_path, call, lambda _: None))
    await asyncio.wait_for(four_started.wait(), 1)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert active == 0 and len(drained) == 4
    assert not fake_renderer and not (tmp_path / "visuals.json").exists()
    assert not list((tmp_path / "visuals").glob("*/asset.json"))
    await asyncio.sleep(0.02)
    assert active == 0 and len(drained) == 4


async def test_visual_semantic_repair_is_bounded(tmp_path, fake_renderer):
    plan, states = material()
    calls = []

    async def call(task, context, model):
        calls.append((task, context))
        return (
            review_result(problems=["拒绝分支丢失"])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(result.issues) == 1 and "content validation" in result.issues[0]["reason"]
    assert len(calls) == 6 and len(fake_renderer) == 3
    assert calls[2][1]["repair_feedback"] == "拒绝分支丢失"
    assert calls[4][1]["repair_feedback"] == "拒绝分支丢失"
    assert len(list((tmp_path / "visuals").glob("*/review-[0-9]*.json"))) == 3
    assert load_visuals(tmp_path, states).issues == result.issues


async def test_new_second_review_problem_is_fixed_on_third_round_with_all_feedback(
    tmp_path, fake_renderer
):
    plan, states = material()
    calls = []
    rounds = 0

    async def call(task, context, model):
        nonlocal rounds
        calls.append((task, context))
        if task == "validate_visual":
            return review_result(
                problems=(
                    ["缺少管理会计及批量报表"]
                    if rounds == 1
                    else ["应先授信校验再判断通过与否"]
                    if rounds == 2
                    else []
                )
            )
        rounds += 1
        return VisualSource(lines=["flowchart TD", f"A-->B{rounds}"])

    assets = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    design = [context for task, context in calls if task == "design_flowchart"]
    assert len(design) == len(fake_renderer) == 3
    assert "repair_feedback" not in design[0]
    assert design[1]["repair_feedback"] == "缺少管理会计及批量报表"
    assert design[2]["repair_feedback"] == "缺少管理会计及批量报表；应先授信校验再判断通过与否"
    assert design[2]["previous_source"] == "flowchart TD\nA-->B2"
    assert Path(assets["s1"][0]["source_path"]).read_text() == "flowchart TD\nA-->B3"
    assert load_visuals(tmp_path, states) == assets


async def test_render_errors_share_three_attempt_bound_and_keep_all_feedback(tmp_path, monkeypatch):
    from folio.visuals import render

    plan, states = material()
    requests = []
    renders = 0

    async def invalid_renderer(source, folder):
        nonlocal renders
        renders += 1
        raise ValueError(f"具体语法问题{renders}")

    async def call(task, context, model):
        assert task == "design_flowchart"
        requests.append(context)
        return VisualSource(lines=["flowchart TD", "A-->B"])

    monkeypatch.setattr(render, "render_flowchart", invalid_renderer)
    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    reason = result.issues[0]["reason"]
    assert "Rendering figure" in reason
    assert renders == len(requests) == 3
    assert requests[2]["repair_feedback"] == "具体语法问题1；具体语法问题2"
    assert "具体语法问题3" in reason
    assert all(f"具体语法问题{i}" not in reason for i in (1, 2))
    assert not list((tmp_path / "visuals").glob("*/asset.json"))


async def test_final_semantic_error_reports_only_current_problems_but_keeps_history(
    tmp_path, fake_renderer
):
    plan, states = material()
    design = []
    reviews = 0

    async def call(task, context, model):
        nonlocal reviews
        if task == "validate_visual":
            reviews += 1
            return review_result(problems=[f"本轮未解决问题{reviews}"])
        design.append(context)
        return VisualSource(lines=["flowchart TD", f"A-->B{len(design)}"])

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    reason = result.issues[0]["reason"]
    assert len(design) == reviews == 3
    assert design[2]["repair_feedback"] == "本轮未解决问题1；本轮未解决问题2"
    assert "本轮未解决问题3" in reason
    assert all(f"本轮未解决问题{i}" not in reason for i in (1, 2))
    directory = next((tmp_path / "visuals").iterdir())
    assert [
        json.loads((directory / f"review-{i}.json").read_text())["problems"] for i in (1, 2, 3)
    ] == [[f"本轮未解决问题{i}"] for i in (1, 2, 3)]


@pytest.mark.parametrize("kind", ["flowchart", "ui"])
async def test_visual_design_and_review_share_scope_and_keep_complete_source(
    tmp_path, fake_renderer, kind
):
    plan, states = material()
    states[0].content.visuals[0].kind = kind
    states[0].content.blocks[
        0
    ].text += "界面提供额度展示、提示弹窗、查询筛选及导出，流程按约定角色逐级审批。"
    original = states[0].content.model_dump()
    calls = []

    async def call(task, context, model):
        calls.append((task, context))
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert [task for task, _ in calls] == [
        "design_flowchart" if kind == "flowchart" else "design_ui",
        "validate_visual",
    ]
    design, review = [context for _, context in calls]
    assert design["visual_scope"] == review["visual_scope"]
    scope = design["visual_scope"]
    assert scope["version"] == 2 and scope["kind"] == kind
    assert scope["purpose"] == states[0].content.visuals[0].description
    assert (
        "actions, sequence and key conditional branches" if kind == "flowchart" else "controls, groups, states and actions"
    ) in scope["required"]
    assert "not a sentence-by-sentence drawing checklist" in scope["source_policy"]
    expected = [block.model_dump(exclude={"evidence_ids"}) for block in states[0].content.blocks]
    assert design["source_blocks"] == review["source_blocks"] == expected
    assert states[0].content.model_dump() == original


async def test_resume_keeps_completed_figure_when_another_exhausts_revisions(
    tmp_path, fake_renderer
):
    plan, states = material()
    states[0].content.visuals.append(
        request().model_copy(update={"figure_id": "v2", "caption": "另一张流程图"})
    )
    calls = []

    async def first_call(task, context, model):
        figure = context["visual"]["figure_id"]
        calls.append((figure, task))
        if task == "validate_visual":
            return review_result(problems=["仍缺业务分支"] if figure == "v2" else [])
        return VisualSource(lines=["flowchart TD", f"A-->B_{figure}"])

    partial = await build_visuals(plan, states, tmp_path, first_call, lambda _: None)
    assert len(partial.issues) == 1 and partial.issues[0]["figure_id"] == "v2"
    assert calls.count(("v1", "design_flowchart")) == 1
    assert calls.count(("v2", "design_flowchart")) == 3
    assert len(list((tmp_path / "visuals").glob("*/asset.json"))) == 1
    assert load_visuals(tmp_path, states).issues == partial.issues
    rendered = len(fake_renderer)
    # Emulate an asset created before the scope contract existed. Its original
    # source context and asset identity must remain valid and need no new calls.
    completed_asset = next((tmp_path / "visuals").glob("*/asset.json"))
    completed_bytes = completed_asset.read_bytes()
    completed_context_path = completed_asset.parent / "source-context.json"
    legacy_context = json.loads(completed_context_path.read_text())
    legacy_context.pop("visual_scope")
    completed_context_path.write_text(json.dumps(legacy_context, ensure_ascii=False))
    resumed = []

    async def resume_call(task, context, model):
        assert context["visual"]["figure_id"] == "v2", "Completed figure must use its cache"
        assert context["visual_scope"]["version"] == 2
        resumed.append((task, context))
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->Fixed"])
        )

    events = []
    assets = await build_visuals(plan, states, tmp_path, resume_call, events.append)
    assert len(assets["s1"]) == 2
    assert len(fake_renderer) == rendered + 1
    assert any("已恢复配图" in event for event in events)
    assert load_visuals(tmp_path, states) == assets
    assert [task for task, _ in resumed] == ["design_flowchart", "validate_visual"]
    assert resumed[0][1]["visual_scope"] == resumed[1][1]["visual_scope"]
    assert completed_asset.read_bytes() == completed_bytes
    assert "visual_scope" not in json.loads(completed_context_path.read_text())


async def fail_visual_three_times(plan, states, folder):
    reviews = 0
    designs = []

    async def call(task, context, model):
        nonlocal reviews
        if task == "validate_visual":
            reviews += 1
            return review_result(problems=[f"旧轮具体问题{reviews}"])
        designs.append(context)
        return VisualSource(lines=["flowchart TD", f"A-->Old{len(designs)}"])

    result = await build_visuals(plan, states, folder, call, lambda _: None)
    assert len(result.issues) == 1
    assert reviews == len(designs) == 3
    return next((folder / "visuals").iterdir())


async def test_failed_visual_resume_uses_last_source_all_feedback_and_three_call_bound(
    tmp_path, fake_renderer
):
    plan, states = material()
    directory = await fail_visual_three_times(plan, states, tmp_path)
    original_records = {
        path.name: path.read_bytes()
        for pattern in ("attempt-*.json", "review-*.json")
        for path in directory.glob(pattern)
    }
    resumed = []
    review_count = 0

    async def call(task, context, model):
        nonlocal review_count
        if task == "validate_visual":
            review_count += 1
            return review_result(problems=[f"新轮具体问题{review_count}"])
        resumed.append(context)
        return VisualSource(lines=["flowchart TD", f"A-->New{len(resumed)}"])

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    reason = result.issues[0]["reason"]
    assert len(resumed) == review_count == 3
    assert len(fake_renderer) == 6
    assert resumed[0]["previous_source"] == "flowchart TD\nA-->Old3"
    assert all(f"旧轮具体问题{i}" in resumed[0]["repair_feedback"] for i in (1, 2, 3))
    assert resumed[1]["previous_source"] == "flowchart TD\nA-->New1"
    assert "新轮具体问题1" in resumed[1]["repair_feedback"]
    assert all(f"旧轮具体问题{i}" in resumed[2]["repair_feedback"] for i in (1, 2, 3))
    assert "新轮具体问题3" in reason
    assert "旧轮具体问题" not in reason
    assert not list((tmp_path / "visuals").glob("*/asset.json"))
    assert {path.name for path in directory.glob("attempt-*.json")} == {
        f"attempt-{number}.json" for number in range(1, 7)
    }
    assert all((directory / name).read_bytes() == value for name, value in original_records.items())


async def test_changed_figure_identity_does_not_reuse_failed_source(tmp_path, fake_renderer):
    plan, states = material()
    old_directory = await fail_visual_three_times(plan, states, tmp_path)
    old_source = (old_directory / "attempt-3.json").read_bytes()
    states[0].content.blocks[0].text += "新版业务明确规定只能由主管逐级审批。"
    calls = []

    async def call(task, context, model):
        calls.append((task, context))
        if task == "validate_visual":
            return review_result(problems=[])
        assert "previous_source" not in context
        assert not context.get("repair_feedback")
        return VisualSource(lines=["flowchart TD", "A-->Fresh"])

    assets = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(calls) == 2
    assert len(list((tmp_path / "visuals").iterdir())) == 2
    assert (
        old_directory / "asset.json" != Path(assets["s1"][0]["source_path"]).parent / "asset.json"
    )
    assert (old_directory / "attempt-3.json").read_bytes() == old_source


@pytest.mark.parametrize(
    "damaged", ["{broken", "[]", '{"source": null}', '{"source": 123}', '{"source": ""}']
)
async def test_all_damaged_attempts_are_safely_ignored(tmp_path, fake_renderer, damaged):
    plan, states = material()
    directory = await fail_visual_three_times(plan, states, tmp_path)
    for attempt in directory.glob("attempt-*.json"):
        attempt.write_text(damaged)
    calls = []

    async def call(task, context, model):
        calls.append((task, context))
        if task == "validate_visual":
            return review_result(problems=[])
        assert not context.get("previous_source")
        return VisualSource(lines=["flowchart TD", "A-->Recovered"])

    result = await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(calls) == 2 and result["s1"][0]["figure_id"] == "v1"


async def test_damaged_latest_attempt_falls_back_to_highest_valid_source(tmp_path, fake_renderer):
    plan, states = material()
    directory = await fail_visual_three_times(plan, states, tmp_path)
    (directory / "attempt-4.json").write_text("{broken")
    design = []

    async def call(task, context, model):
        if task == "validate_visual":
            return review_result(problems=[])
        design.append(context)
        return VisualSource(lines=["flowchart TD", "A-->Recovered"])

    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(design) == 1
    assert design[0]["previous_source"] == "flowchart TD\nA-->Old3"
    assert "旧轮具体问题3" in design[0]["repair_feedback"]


async def test_no_visuals_do_not_call_tools(tmp_path):
    async def call(*args):
        raise AssertionError("no model call expected")

    assert await build_visuals(DocumentPlan(sections=[]), [], tmp_path, call, lambda _: None) == {}


async def test_engine_exports_requested_figures_and_reformats_without_model(
    store, demo_job, tmp_path, fake_renderer
):
    from folio.document.reformat import reformat_job

    # This export fixture intentionally requests a figure in each section.
    from folio.models import JobInput

    request_input = store.get(demo_job)["input"]
    request_input["user_request"] = (
        "# 外汇交易\n提供交易总览流程图。\n## 交易撤销\n提供交易撤销审批流程图。\n"
    )
    demo_job = store.create(JobInput.model_validate(request_input))

    class Runtime(DemoRuntime):
        async def complete(self, task, context, schema, session_key):
            if task == "design_flowchart":
                return RuntimeResult(json.dumps({"lines": ["flowchart TD", "A-->B"]}))
            if task == "validate_visual":
                return RuntimeResult(json.dumps({"problems": []}))
            result = await super().complete(task, context, schema, session_key)
            if task in {"generate", "repair"}:
                value = json.loads(result.content)
                block = value["blocks"][0]["block_id"]
                visual = request().model_dump() | {
                    "source_block_ids": [block],
                    "after_block_id": block,
                }
                value["visuals"] = [visual]
                return RuntimeResult(json.dumps(value))
            return result

    job = await Engine(store, Runtime()).run(demo_job)
    assert job["status"] == "draft"
    assert len(Document(job["artifact"]).inline_shapes) == 2
    folder = store.root / "jobs" / demo_job
    manifest = json.loads((folder / "visuals.json").read_text())
    assert len(manifest["sections"]) == 2
    before = len(fake_renderer)
    result = reformat_job(store, demo_job, tmp_path / "reformat")
    assert len(Document(result["artifact"]).inline_shapes) == 2
    assert result["model_calls"] == 0 and len(fake_renderer) == before


async def test_bad_cache_metadata_is_rebuilt(tmp_path, fake_renderer):
    plan, states = material()

    async def call(task, context, model):
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    cache = next((tmp_path / "visuals").glob("*/asset.json"))
    cache.write_text("null")
    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(fake_renderer) == 2
    value = json.loads(cache.read_text())
    value["caption"] = "错误图题"
    cache.write_text(json.dumps(value))
    await build_visuals(plan, states, tmp_path, call, lambda _: None)
    assert len(fake_renderer) == 3


async def test_oversized_visual_sources_are_read_in_bounded_pages(tmp_path, fake_renderer):
    from folio.visuals.workflow import VisualMaterial

    plan, states = material()
    states[0].content.blocks = [
        Block(block_id=f"b{i}", text="业务审批规则。" * 850) for i in range(5)
    ]
    states[0].content.visuals[0].source_block_ids = [f"b{i}" for i in range(5)]
    calls = []

    async def measure(task, context, model):
        return {"fits": len(json.dumps(context, ensure_ascii=False)) < 18000}

    async def call(task, context, model):
        assert (await measure(task, context, model))["fits"]
        calls.append((task, context))
        if task == "read_visual_materials":
            return VisualMaterial(requirements=["业务审批规则"])
        return (
            review_result(problems=[])
            if task == "validate_visual"
            else VisualSource(lines=["flowchart TD", "A-->B"])
        )

    await build_visuals(plan, states, tmp_path, call, lambda _: None, measure)
    reads = [c for t, c in calls if t == "read_visual_materials"]
    seen = "".join(part["text"] for c in reads for part in c["source_parts"])
    assert seen == "".join(b.text for b in states[0].content.blocks)
    assert next(c for t, c in calls if t == "design_flowchart")["source_requirements"] == [
        "业务审批规则"
    ]
    assert "source_blocks" not in next(c for t, c in calls if t == "validate_visual")
    scopes = [c["visual_scope"] for t, c in calls if t in {"design_flowchart", "validate_visual"}]
    assert scopes[0] == scopes[1] and scopes[0]["version"] == 2
    archived = json.loads(next((tmp_path / "visuals").glob("*/source-context.json")).read_text())
    assert archived["source_blocks"] == [
        block.model_dump(exclude={"evidence_ids"}) for block in states[0].content.blocks
    ]
