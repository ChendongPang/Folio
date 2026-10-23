"""A research model cannot omit a separately established full-coverage scope."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from native_calls import native_call, research_call
from PIL import Image

from folio.models import JobInput, Limits
from folio.orchestration.engine import Engine
from folio.research.artifact_coverage import CoverageManager
from folio.research.contracts import ResearchConfig
from folio.research.coverage_obligations import establish_coverage
from folio.research.ledger import ResearchLedger
from folio.research.native_tools import native_result
from folio.research.workspace import WorkspaceManager
from folio.runtime.demo import DemoRuntime


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_ARTIFACT_KEY_PATH", str(tmp_path / "host" / "key"))
    source = tmp_path / "sources"
    source.mkdir()
    (source / "fields.txt").write_text("字段A：必填\n字段B：选填", encoding="utf-8")
    Image.new("RGB", (10, 10), "white").save(source / "scan.png")
    workspace = WorkspaceManager(source, tmp_path / "task")
    ledger = ResearchLedger(tmp_path / "ledger.sqlite")
    session = ledger.load_session("task", "round1") | {"round_id": "round1"}
    value = SimpleNamespace(
        workspace=workspace,
        coverage=CoverageManager(workspace.task_root, workspace),
        ledger=ledger,
        session=session,
        task=SimpleNamespace(task_id="task"),
        engine=SimpleNamespace(
            request=SimpleNamespace(requirements=None, user_request="完整列出资料中的每一字段")
        ),
        decisions=[],
        calls=[],
    )

    async def model(action, context, schema):
        value.calls.append((action, context))
        decision = value.decisions.pop(0)
        if isinstance(decision, BaseException):
            raise decision
        name = (
            "inspect_coverage_sources"
            if decision.get("structure_requests") or decision.get("catalog_requests")
            else "submit_coverage"
        )
        result = native_call(name, decision)
        return schema.model_validate(native_result(action, result, context, schema.model_json_schema()))

    value.model = model
    return value


def assignment():
    return {
        "task": {"task_id": "task", "objective": "字段设计"},
        "sections": [{"section_id": "s1", "instruction": "完整定义所有字段"}],
    }


def decision(source="fields.txt", locator=""):
    return {
        "required_scopes": [
            {"source_id": source, "locator_prefix": locator, "reason": "正式要求逐条定义资料字段"}
        ],
        "unresolved_scopes": [],
        "reason": "独立核对实际写作职责",
    }


async def test_required_scopes_are_materialized_and_resume_reuses_contract(tools):
    tools.decisions.append(decision())
    first = await establish_coverage(tools, assignment())
    assert first["status"] == "established" and first["required_scope_count"] == 1
    assert len(tools.session["required_coverage_ids"]) == 1
    assert tools.session["coverage_ids"] == tools.session["required_coverage_ids"]
    cid = tools.session["coverage_ids"][0]
    assert tools.coverage.report(cid)["total"] == 2
    tools.session = tools.ledger.load_session("task", "round1")
    assert await establish_coverage(tools, assignment()) == first
    assert len(tools.calls) == 1


async def test_complete_formal_requirements_and_real_catalog_reach_reviewer(tools):
    request = tools.engine.request
    request.user_request = "保留全部原始要求。" * 2000
    tools.decisions.append(
        {"required_scopes": [], "unresolved_scopes": [], "reason": "本任务只需概述，无逐项覆盖要求"}
    )
    result = await establish_coverage(tools, assignment())
    assert result["required_scope_count"] == 0 and result["unresolved_count"] == 0
    _, context = tools.calls[0]
    assert context["user_request"] == request.user_request
    assert {entry["source_id"] for entry in context["source_catalog"]} == {"fields.txt", "scan.png"}
    assert context["sections"] == assignment()["sections"]


@pytest.mark.parametrize(
    "source,locator,reason",
    [
        ("missing.txt", "", "absent from the current input snapshot"),
        ("scan.png", "", "unread or unverified content"),
        ("fields.txt", "line 900", "no enumerable source entries"),
    ],
)
async def test_missing_or_unreadable_required_scope_stays_unresolved(
    tools, source, locator, reason
):
    tools.decisions.append(decision(source, locator))
    result = await establish_coverage(tools, assignment())
    assert result["unresolved_count"] == 1 and reason in result["unresolved_scopes"][0]
    assert tools.session["coverage_unresolved"]
    assert tools.session["required_coverage_ids"] == []


async def test_review_failure_is_durable_unresolved_and_new_inputs_can_retry(tools):
    tools.decisions.append(ValueError("invalid structured result"))
    failed = await establish_coverage(tools, assignment())
    assert failed["status"] == "unavailable" and failed["unresolved_count"] == 1
    assert await establish_coverage(tools, assignment()) == failed
    assert len(tools.calls) == 1
    tools.decisions.append(decision())
    tools.engine.request.user_request += "；新增正式要求：核对全部字段。"
    recovered = await establish_coverage(tools, assignment())
    assert recovered["status"] == "established" and not recovered["unresolved_count"]
    assert len(tools.calls) == 2


async def test_cancellation_does_not_persist_a_successful_contract(tools):
    tools.decisions.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await establish_coverage(tools, assignment())
    assert not tools.session.get("coverage_contract")


class OmitCoverageRuntime(DemoRuntime):
    """The independent scope reviewer requires coverage; researcher omits it."""

    def __init__(self):
        self.actions = []

    async def complete(self, task, context, schema, session_key):
        self.actions.append(task)
        if task == "coverage_obligations":
            source = context["source_catalog"][0]["source_id"]
            return native_call("submit_coverage", decision(source))
        if task == "research_step":
            if context["step"] == 0:
                return research_call("fs.read", {"path": context["catalog"]["files"][0]["path"], "limit": 1},
                                     notes="只读取一个来源条目")
            return research_call("finish", missing_information=[], notes="研究模型省略清单和资料读取",
                                 research_notes=self.research_notes(context))
        return await super().complete(task, context, schema, session_key)


async def test_finish_without_creating_required_scope_cannot_pass_engine(store, inputs):
    (Path(inputs["knowledge_path"]) / "rules.md").write_text(
        "外汇交易由交易员录入。\n交易撤销必须由主管审批。", encoding="utf-8"
    )
    runtime = OmitCoverageRuntime()
    job_id = store.create(
        JobInput(**inputs, runtime="demo", research=ResearchConfig(), limits=Limits(max_repairs=0))
    )
    job = await Engine(store, runtime).run(job_id)
    assert "coverage_obligations" in runtime.actions
    assert job["status"] == "needs_confirmation" and not job["snapshot"]["review_passed"]
    assert any(
        issue["key"].startswith("research-coverage:")
        for issue in job["snapshot"]["blocking_issues"]
    )
    assert all(
        work.research["required_coverage_ids"] for work in store.work_states(job_id).values()
    )


async def test_existing_job_without_research_keeps_legacy_path(store, inputs):
    runtime = OmitCoverageRuntime()
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)
    assert job["status"] == "draft"
    assert "coverage_obligations" not in runtime.actions and "research_step" not in runtime.actions


async def test_independent_reviewer_can_require_exact_chapter_from_shared_structure(tools):
    workspace = tools.workspace
    (workspace.source_root / "manual.md").write_text(
        "# 接口\n全部输入字段\n## 字段\namount 必填\n# 审计\n审计周期", encoding="utf-8"
    )
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    tools.coverage = CoverageManager(workspace.task_root, tools.workspace)
    chapter = tools.workspace.inspect("manual.md")["structure"][0]
    tools.decisions.append({
        "required_scopes": [{"source_id": "manual.md", "node_id": chapter["node_id"],
                             "reason": "完整定义接口章节中的全部字段"}],
        "unresolved_scopes": [], "reason": "根据真实章节目录限定任务职责",
    })
    summary = await establish_coverage(tools, assignment())
    assert summary["required_scopes"][0]["node_id"] == chapter["node_id"]
    context = tools.calls[0][1]
    catalog = next(item for item in context["source_catalog"] if item["source_id"] == "manual.md")
    assert any(node["node_id"] == chapter["node_id"] for node in catalog["structure"])
    scope = tools.coverage._load(tools.session["required_coverage_ids"][0])["scope"]
    assert [item["text"] for item in scope["items"]] == ["# 接口", "全部输入字段", "## 字段", "amount 必填"]
    assert await establish_coverage(tools, assignment()) == summary
    assert len(tools.calls) == 1


def long_manual(tools):
    workspace = tools.workspace
    (workspace.source_root / "manual.md").write_text(
        "\n".join(f"# 第 {number} 章\n字段 {number} 必填" for number in range(1, 26)), encoding="utf-8"
    )
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    tools.coverage = CoverageManager(workspace.task_root, tools.workspace)
    return tools.workspace.inspect("manual.md")["structure"][-1]


async def test_scope_reviewer_can_navigate_to_chapter_beyond_initial_preview(tools):
    chapter = long_manual(tools)
    tools.decisions.extend([
        {"structure_requests": [{"source_id": "manual.md", "offset": 20}],
         "reason": "需要查阅后续章节目录"},
        {"required_scopes": [{"source_id": "manual.md", "node_id": chapter["node_id"],
                              "reason": "正式要求完整覆盖第 25 章"}],
         "reason": "已通过后续目录定位"},
    ])
    result = await establish_coverage(tools, assignment())
    assert not result["unresolved_scopes"]
    assert len(tools.calls) == 2
    pages = tools.calls[1][1]["structure_pages"]
    assert pages[0]["next_offset"] is None
    assert any(node["node_id"] == chapter["node_id"] for node in pages[0]["structure"])
    assert tools.calls[0][1]["sections"] == tools.calls[1][1]["sections"]
    observed = tools.calls[1][1]["observations"][0]
    assert observed["tool_call"]["id"] == observed["request_id"]
    assert observed["tool_call"]["function"]["name"] == "inspect_coverage_sources"
    assert observed["result"]["pages"] == pages


async def test_coverage_navigation_resume_keeps_native_call_identity(tools, monkeypatch):
    from folio.research import coverage_obligations

    chapter = long_manual(tools)
    tools.decisions.extend([
        {"structure_requests": [{"source_id": "manual.md", "offset": 20}],
         "reason": "查找后续章节"},
        {"required_scopes": [{"source_id": "manual.md", "node_id": chapter["node_id"],
                              "reason": "正式职责要求全量覆盖该章"}], "reason": "已核定范围"},
    ])
    original = coverage_obligations._local
    interrupted = False

    async def interrupt_navigation(function, *args, **kwargs):
        nonlocal interrupted
        if function is coverage_obligations._structure_page and len(args) > 2 and args[2] == 20 and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(coverage_obligations, "_local", interrupt_navigation)
    with pytest.raises(asyncio.CancelledError):
        await establish_coverage(tools, assignment())
    tools.session = tools.ledger.load_session("task", "round1")
    saved = next(iter(tools.session["coverage_review_pending"].values()))
    assert saved["tool_call"]["function"]["name"] == "inspect_coverage_sources"
    result = await establish_coverage(tools, assignment())
    assert not result["unresolved_count"] and len(tools.calls) == 2
    assert tools.calls[1][1]["observations"][0]["tool_call"] == saved["tool_call"]


async def test_unresolved_scope_reconsiders_new_discovery_but_not_ordinary_resume(tools):
    chapter = long_manual(tools)
    tools.decisions.append({"unresolved_scopes": ["第 25 章尚未定位"], "reason": "初始目录不足"})
    initial = await establish_coverage(tools, assignment())
    assert await establish_coverage(tools, assignment()) == initial
    assert len(tools.calls) == 1
    record = tools.coverage.create("manual.md", node_id=chapter["node_id"])
    tools.session.setdefault("coverage_ids", []).append(record["coverage_id"])
    tools.decisions.append({"required_scopes": [
        {"source_id": "manual.md", "node_id": chapter["node_id"], "reason": "新位置符合正式职责"}
    ], "reason": "独立确认后续找到的范围"})
    result = await establish_coverage(tools, assignment())
    assert not result["unresolved_scopes"]
    assert result["input_hash"] == initial["input_hash"]
    assert tools.session["coverage_contract"]["reconsiderations"] == 1
    assert len(tools.calls) == 2


async def test_directory_navigation_has_durable_bounded_allowance(tools):
    long_manual(tools)
    tools.decisions.extend([
        {"structure_requests": [{"source_id": "manual.md", "query": "第 25 章"}],
         "reason": "继续查目录"} for _ in range(3)
    ])
    result = await establish_coverage(tools, assignment())
    assert result["unresolved_count"] == 1
    assert len(tools.calls) == 3
    assert tools.calls[1][1]["structure_pages"][0]["total"] == 1
    tools.session = tools.ledger.load_session("task", "round1")
    assert await establish_coverage(tools, assignment()) == result
    assert len(tools.calls) == 3


async def test_many_duties_are_partitioned_without_dropping_formal_requirements(tools):
    async def capacity(action, context, schema):
        return {"fits": len(json.dumps(context, ensure_ascii=False)) < 16000}

    tools.engine.request_capacity = capacity
    tools.engine.request.user_request = "完整落实每个章节的职责，不可遗漏字段。"
    responsibilities = [{"section_id": f"s{number}", "instruction": "字段职责。" * 600}
                        for number in range(8)]
    tools.decisions.extend([decision() for _ in range(4)])
    result = await establish_coverage(tools, assignment() | {"sections": responsibilities})
    assert not result["unresolved_count"]
    seen = [section["section_id"] for _, context in tools.calls for section in context["sections"]]
    assert seen == [section["section_id"] for section in responsibilities]
    assert len(tools.calls) == 4
    assert all(context["user_request"] == tools.engine.request.user_request for _, context in tools.calls)


async def test_single_oversized_formal_requirement_is_explicitly_unresolved(tools):
    async def capacity(action, context, schema):
        return {"fits": len(json.dumps(context, ensure_ascii=False)) < 1000}

    tools.engine.request_capacity = capacity
    tools.engine.request.user_request = "正式要求不可删减。" * 200
    result = await establish_coverage(tools, assignment())
    assert result["status"] == "unavailable"
    assert 'capacity' in result["unresolved_scopes"][0]
    assert tools.calls == []


async def test_forged_source_location_does_not_trigger_scope_reconsideration(tools):
    from folio.models import Evidence

    tools.decisions.append({"unresolved_scopes": ["尚未定位"], "reason": "资料位置不足"})
    initial = await establish_coverage(tools, assignment())
    tools.evidence = {"forged": Evidence(evidence_id="forged", source_id="fields.txt",
                                        source_version=tools.workspace._manifest["fields.txt"]["version"],
                                        locator="invented chapter", text="invented")}
    assert await establish_coverage(tools, assignment()) == initial
    assert len(tools.calls) == 1


async def test_full_catalog_is_available_initially_and_query_still_works(tools):
    workspace = tools.workspace
    for number in range(35):
        (workspace.source_root / f"manual-{number:02}.txt").write_text("审批字段", encoding="utf-8")
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    tools.coverage = CoverageManager(workspace.task_root, tools.workspace)
    tools.decisions.extend([
        {"catalog_requests": [{"query": "manual-34"}], "reason": "查找指定文件"},
        decision("manual-34.txt"),
    ])
    result = await establish_coverage(tools, assignment())
    assert not result["unresolved_count"]
    assert tools.calls[0][1]["source_catalog_next_offset"] is None
    assert any(item["source_id"] == "manual-34.txt" for item in tools.calls[0][1]["source_catalog"])
    catalog = tools.calls[1][1]["structure_pages"][0]
    assert catalog["catalog"][0]["source_id"] == "manual-34.txt"


async def test_navigation_cannot_erase_previously_confirmed_required_scope(tools):
    chapter = long_manual(tools)
    first = decision()
    first["structure_requests"] = [{"source_id": "manual.md", "offset": 20}]
    tools.decisions.extend([first, {
        "required_scopes": [{"source_id": "manual.md", "node_id": chapter["node_id"],
                             "reason": "后续确认第 25 章职责"}],
        "reason": "模型在最终回复省略了先前已确认的字段文件",
    }])
    result = await establish_coverage(tools, assignment())
    assert result["required_scope_count"] == 2
    assert {scope["source_id"] for scope in result["required_scopes"]} == {"fields.txt", "manual.md"}
    previous = tools.calls[1][1]["previous_scope_decision"]
    assert previous["required_scopes"][0]["source_id"] == "fields.txt"
    assert len(tools.session["required_coverage_ids"]) == 2


@pytest.mark.parametrize("manual,legacy", [(False, False), (False, True), (True, False)])
async def test_reenumeration_supersedes_only_automatic_scope_ids(tools, manual, legacy):
    from test_source_structure import pdf

    from folio.research.extraction_review import ExtractionReviewManager

    workspace = tools.workspace
    pdf(workspace.source_root / "native.pdf", pages=1)
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    tools.coverage = CoverageManager(workspace.task_root, tools.workspace)
    tools.decisions.append(decision("native.pdf"))
    initial = await establish_coverage(tools, assignment())
    old_id = tools.session["required_coverage_ids"][0]
    if legacy:
        tools.session.pop("coverage_auto_scopes")
    if manual:
        tools.session["manual_coverage_ids"] = [old_id]
    other_manual = tools.coverage.create("fields.txt")["coverage_id"]
    tools.session["coverage_ids"].append(other_manual)
    tools.session.setdefault("manual_coverage_ids", []).append(other_manual)
    image = workspace.task_root / "artifacts" / "verified-native.png"
    Image.new("RGB", (20, 20), "white").save(image)
    manager = ExtractionReviewManager(workspace.task_root, tools.workspace)
    prepared = manager.prepare_visual("native.pdf", 1, "Page 1 body", page_count=1)
    manager.record_visual(prepared, review_key="independent-visual-review", supported=True,
                          complete=True, reason="Complete exact original page text",
                          image_paths=["artifacts/verified-native.png"])
    updated = await establish_coverage(tools, assignment())
    new_id = tools.session["required_coverage_ids"][0]
    assert new_id != old_id
    assert updated["input_hash"] == initial["input_hash"]
    assert updated["required_scope_count"] == 1
    assert len(tools.calls) == 1
    assert new_id in tools.session["coverage_ids"]
    assert other_manual in tools.session["coverage_ids"]
    assert (old_id in tools.session["coverage_ids"]) is manual
    assert tools.coverage._load(old_id)["coverage_id"] == old_id  # Signed historical record is retained.
    if not manual:
        assert tools.session["coverage_supersessions"][-1]["previous"] == old_id


async def test_confirmed_scope_history_is_bounded_by_formal_input_identity(tools):
    identities = []
    for number in range(10):
        tools.decisions.append({"reason": "本轮正式职责无需全量覆盖"})
        tools.engine.request.user_request = f"正式任务变更 {number}"
        summary = await establish_coverage(tools, assignment())
        identities.append(summary["input_hash"])
    assert len(tools.calls) == 10
    assert set(tools.session["coverage_confirmed_scopes"]) == set(identities[-8:])
    assert all(key.split(":")[0] in identities[-8:] for key in tools.session["coverage_review_attempts"])


def test_confirmed_scope_limit_fails_explicitly_without_discarding_known_duties():
    from folio.research.coverage_obligations import _merge_scopes

    confirmed = [{"source_id": f"source-{index}", "reason": "正式职责"} for index in range(200)]
    with pytest.raises(ValueError, match='200-item capacity'):
        _merge_scopes(confirmed, [{"source_id": "overflow", "reason": "额外职责"}])
    assert len(confirmed) == 200
    assert confirmed[0]["source_id"] == "source-0"


async def test_full_structure_reaches_first_review_without_twenty_node_page(tools):
    workspace = tools.workspace
    (workspace.source_root / "manual.md").write_text(
        "\n".join(f"# Section {number}\nRequirement {number}" for number in range(62)), encoding="utf-8"
    )
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    tools.decisions.append({"reason": "All section locations are already available"})
    await establish_coverage(tools, assignment())
    catalog = next(item for item in tools.calls[0][1]["source_catalog"] if item["source_id"] == "manual.md")
    assert len(catalog["structure"]) == 62
    assert not catalog["structure_truncated"]
    assert catalog["structure_next_offset"] is None
    assert catalog["structure"][-1]["title"] == "Section 61"


@pytest.mark.parametrize("selector", ["node_id", "structure_id", "structure_node", "locator_prefix"])
def test_structure_navigation_accepts_exact_logged_node_selectors(tools, selector):
    from folio.research.coverage_obligations import (
        StructureRequest,
        _structure_page,
        normalize_coverage_arguments,
    )

    workspace = tools.workspace
    (workspace.source_root / "manual.md").write_text(
        "# Procurement\nRequired\n## Intelligent agents\nDetails\n# Pricing\nCost", encoding="utf-8"
    )
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    nodes = tools.workspace.inspect("manual.md")["structure"]
    requested = {"source_id": "manual.md", selector: nodes[0]["node_id"]}
    normalized = normalize_coverage_arguments({"structure_requests": [requested]})
    request = StructureRequest.model_validate(normalized["structure_requests"][0])
    page = _structure_page(tools.workspace, **request.model_dump())
    assert [node["title"] for node in page["structure"]] == ["Procurement", "Intelligent agents"]
    assert page["next_offset"] is None


def test_structure_navigation_validates_source_selector_and_offset(tools):
    from folio.research.coverage_obligations import StructureRequest, _structure_page

    with pytest.raises(ValueError, match="ambiguous"):
        _structure_page(tools.workspace, None)
    with pytest.raises(ValueError, match="snapshot"):
        _structure_page(tools.workspace, "missing.txt")
    with pytest.raises(ValueError):
        _structure_page(tools.workspace, "fields.txt", node_id="N999")
    with pytest.raises(ValueError, match="matches no"):
        _structure_page(tools.workspace, "fields.txt", locator_prefix="line 999")
    with pytest.raises(ValueError, match="exceeds"):
        _structure_page(tools.workspace, "fields.txt", offset=999)
    with pytest.raises(ValueError, match="Conflicting"):
        StructureRequest.model_validate({"node_id": "N001", "structure_id": "N002"})
    with pytest.raises(ValueError, match="Choose"):
        StructureRequest.model_validate({"node_id": "N001", "locator_prefix": "line 1"})
    with pytest.raises(ValueError):
        StructureRequest.model_validate({"source_id": "fields.txt", "unsupported": "discard me"})


async def test_logged_structure_alias_passes_native_tool_validation(tools):
    chapter = long_manual(tools)
    tools.decisions.extend([
        {"structure_requests": [{"source_id": "manual.md", "structure_node": chapter["node_id"]}],
         "reason": "Inspect the exact selected section"},
        {"required_scopes": [{"source_id": "manual.md", "node_id": chapter["node_id"],
                              "reason": "Complete coverage of the assigned chapter"}],
         "reason": "The section is now verified"},
    ])
    result = await establish_coverage(tools, assignment())
    assert result["status"] == "established"
    assert not result["unresolved_count"]
    assert len(tools.calls) == 2
    observation = tools.calls[1][1]["observations"][0]
    assert observation["result"]["pages"][0]["structure"][0]["node_id"] == chapter["node_id"]
    assert "structure_node" in observation["tool_call"]["function"]["arguments"]


def test_single_source_navigation_infers_omitted_source_and_honors_query(tools):
    from folio.research.coverage_obligations import _structure_page

    workspace = tools.workspace
    (workspace.source_root / "scan.png").unlink()
    (workspace.source_root / "fields.txt").unlink()
    (workspace.source_root / "manual.md").write_text("# First\nA\n# Second\nB", encoding="utf-8")
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)
    page = _structure_page(tools.workspace, None, query="Second")
    assert page["source_id"] == "manual.md"
    assert [node["title"] for node in page["structure"]] == ["Second"]
    with pytest.raises(ValueError, match="exceeds"):
        _structure_page(tools.workspace, None, offset=20, query="Second")


async def test_only_actual_capacity_pressure_paginates_structure(tools):
    workspace = tools.workspace
    (workspace.source_root / "manual.md").write_text(
        "\n".join(f"# Section {number} {'long title ' * 30}\nRequirement" for number in range(62)),
        encoding="utf-8",
    )
    tools.workspace = WorkspaceManager(workspace.source_root, workspace.task_root)

    async def capacity(action, context, schema):
        return {"fits": len(json.dumps(context, ensure_ascii=False)) <= 10000}

    tools.engine.request_capacity = capacity
    tools.decisions.append({"reason": "No exhaustive scope required for this summary"})
    await establish_coverage(tools, assignment())
    context = tools.calls[0][1]
    assert len(json.dumps(context, ensure_ascii=False)) <= 10000
    catalog = next(item for item in context["source_catalog"] if item["source_id"] == "manual.md")
    assert 0 < len(catalog["structure"]) < 62
    assert catalog["structure_next_offset"] == len(catalog["structure"])
    assert catalog["structure_total"] == 62
    assert catalog["structure_truncated"]


async def test_repair_hints_and_more_read_text_do_not_restart_scope_review(tools):
    from folio.models import Evidence

    tools.decisions.append({"unresolved_scopes": ["The required additional source has not been supplied"],
                            "reason": "Formal scope remains unresolved until new materials arrive"})
    first = await establish_coverage(tools, assignment())
    evidence = tools.workspace.read("fields.txt")["evidence"]
    tools.evidence = {item["evidence_id"]: Evidence.model_validate(item) for item in evidence}
    second = await establish_coverage(tools, assignment() | {
        "writing_gaps": ["Company qualifications need user input"], "instruction": "Resolve current writing gaps",
    })
    assert first == second
    assert len(tools.calls) == 1


async def test_legacy_current_contract_migrates_without_another_review(tools):
    from folio.models import digest

    tools.decisions.append(decision())
    first = await establish_coverage(tools, assignment())
    formal_hash = first["input_hash"]
    legacy_hash = digest(json.dumps(assignment() | {
        "requirements": None, "user_request": tools.engine.request.user_request,
        "source_snapshot_id": tools.workspace.source_snapshot_id,
        "writing_gaps": [], "instruction": "",
    }, ensure_ascii=False, sort_keys=True))
    tools.session["coverage_contract"]["input_hash"] = legacy_hash
    tools.session["coverage_contract"]["version"] = 2
    tools.session["coverage_confirmed_scopes"][legacy_hash] = (
        tools.session["coverage_confirmed_scopes"].pop(formal_hash)
    )
    assert (await establish_coverage(tools, assignment()))["input_hash"] == formal_hash
    assert len(tools.calls) == 1
    assert legacy_hash not in tools.session["coverage_confirmed_scopes"]


async def test_failed_v2_navigation_retries_once_without_erasing_confirmed_scopes(tools):
    from folio.models import digest

    legacy_hash = digest(json.dumps(assignment() | {
        "requirements": None, "user_request": tools.engine.request.user_request,
        "source_snapshot_id": tools.workspace.source_snapshot_id,
        "writing_gaps": [], "instruction": "",
    }, ensure_ascii=False, sort_keys=True))
    scope = decision()["required_scopes"][0]
    tools.session.update(
        coverage_contract={
            "version": 2, "input_hash": legacy_hash, "status": "unavailable",
            "required_scopes": [scope], "unresolved_scopes": ["Legacy structure parameter validation failed"],
        },
        coverage_confirmed_scopes={legacy_hash: [scope]},
        coverage_review_attempts={legacy_hash + ":0:0": 3},
    )
    tools.decisions.append({"reason": "Full structure is available; the established obligation remains valid"})
    result = await establish_coverage(tools, assignment())
    assert result["status"] == "established"
    assert result["required_scope_count"] == 1
    assert tools.session["coverage_contract"]["version"] == 3
    assert await establish_coverage(tools, assignment()) == result
    assert len(tools.calls) == 1


async def test_failed_v3_review_is_not_retried_by_restart(tools):
    tools.decisions.append(ValueError("Current provider still failed"))
    result = await establish_coverage(tools, assignment())
    assert tools.session["coverage_contract"]["version"] == 3
    tools.session = tools.ledger.load_session("task", "round1")
    assert await establish_coverage(tools, assignment()) == result
    assert len(tools.calls) == 1
