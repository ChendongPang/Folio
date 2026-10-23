"""Current planning contracts stay strict; archived data upgrades at read boundaries."""

import asyncio
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from folio.legacy_upgrade import upgrade_job_input, upgrade_plan, upgrade_proposal
from folio.models import DocumentPlan, JobInput, PlanItem, PlanProposal
from folio.runtime.demo import DemoRuntime


def test_fresh_plans_cannot_send_retired_mapping_fields():
    fields = {"node_id", "title", "level", "instruction", "content_mode"}
    assert set(PlanItem.model_fields) == fields
    item = {"node_id": "orders", "title": "订单", "level": 1, "instruction": "说明订单规则"}
    for field in ("outline_refs", "template_slot_id", "template_table_ref", "mapping_reason"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            PlanItem.model_validate(item | {field: []})
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlanProposal.model_validate({"sections": [item], "unused_template_slots": []})


def test_archived_plans_keep_checkpoint_ids_without_requirement_references():
    old = {
        "sections": [
            {"section_id": sid, "outline_node_id": "shared_requirement", "title": title,
             "level": 1, "display_number": str(index), "instruction": "业务要求",
             "origin": "outline", "outline_refs": ["shared_requirement"],
             "template_slot_id": None, "template_table_ref": None, "mapping_reason": "关联需求",
             "source_heading_paths": [[title]], "expected_table_headers": ["字段", "规则"]}
            for index, (sid, title) in enumerate((("sec_a", "订单"), ("sec_b", "退款")), 1)
        ],
        "planning_method": "llm",
        "unused_template_slots": [],
        "notes": ["原始计划说明"],
    }
    original = deepcopy(old)
    plan = DocumentPlan.model_validate(upgrade_plan(old))
    assert old == original
    assert [s.section_id for s in plan.sections] == ["sec_a", "sec_b"]
    assert [s.node_id for s in plan.sections] == ["sec_a", "sec_b"]
    assert plan.sections[0].source_heading_paths == [["订单"]]
    assert plan.sections[0].expected_table_headers == ["字段", "规则"]
    assert upgrade_plan(plan.model_dump()) == plan.model_dump()


def test_archive_adapter_does_not_hide_unrelated_schema_errors():
    old = {"sections": [{"node_id": "one", "title": "一", "level": 1,
                         "instruction": "要求", "outline_refs": ["o1"], "unknown": True}]}
    with pytest.raises(ValidationError, match="unknown"):
        PlanProposal.model_validate(upgrade_proposal(old))


def test_revising_an_archived_proposal_preserves_existing_section_identity():
    from folio.document.planner import build_plan

    old_proposal = {"sections": [{"node_id": "orders", "title": "订单", "level": 1,
                                  "instruction": "说明规则", "outline_refs": ["o1"],
                                  "mapping_reason": "来自用户要求"}]}
    clean = PlanProposal.model_validate(upgrade_proposal(old_proposal))
    original = build_plan("web_same_conversation", clean)
    legacy = original.model_dump()
    item = legacy["sections"][0]
    item.pop("node_id")
    item["outline_node_id"] = "o1"
    archived = DocumentPlan.model_validate(upgrade_plan(legacy))
    # Revision starts from the archived proposal, not the converted final plan.
    clean.sections[0].instruction += "以及异常处理"
    revised = build_plan("web_same_conversation", clean)
    assert revised.sections[0].section_id == archived.sections[0].section_id


def test_archived_input_recovers_requirements_without_touching_source(tmp_path):
    path = tmp_path / "outline.md"
    path.write_text("# 订单\n说明退款规则。", encoding="utf-8")
    old = {"title": "需求说明", "outline_path": str(path), "template_path": "missing.docx",
           "knowledge_path": str(tmp_path)}
    updated = upgrade_job_input(old)
    request = JobInput.model_validate(updated)
    assert "订单" in request.user_request
    assert "说明退款规则。" in request.user_request
    assert path.read_text(encoding="utf-8") == "# 订单\n说明退款规则。"
    assert "outline_path" in old
    assert not {"template_path", "outline_path"}.intersection(updated)
    with pytest.raises(ValidationError, match="Extra inputs"):
        JobInput.model_validate(old)


def test_existing_confirmed_requirements_take_precedence_over_legacy_file(tmp_path):
    old = {"title": "要求", "user_request": "以确认后的退款范围为准", "outline_path": "/missing",
           "template_path": "/missing", "knowledge_path": str(tmp_path)}
    assert upgrade_job_input(old)["user_request"] == old["user_request"]
    assert upgrade_job_input(old | {"user_request": ""})["user_request"] == ""


def test_demo_plans_have_current_contract():
    runtime = DemoRuntime()
    response = asyncio.run(runtime.complete(
        "plan", {"requirements": "# 订单\n说明下单规则\n## 退款\n说明退款规则"},
        PlanProposal.model_json_schema(), "demo",
    ))
    proposal = PlanProposal.model_validate(json.loads(response.content))
    assert [item.title for item in proposal.sections] == ["订单", "退款"]
    assert [item.level for item in proposal.sections] == [1, 2]
    assert "退款规则" in proposal.sections[-1].instruction


def test_store_new_jobs_need_only_requirements_and_sources(tmp_path):
    from folio.store import Store

    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "rules.md").write_text("订单可在付款前撤销。", encoding="utf-8")
    store = Store(tmp_path / "state")
    job_id = store.create(JobInput(title="订单需求", user_request="说明订单", knowledge_path=str(sources)))
    job = store.get(job_id)
    assert job["input"]["user_request"] == "说明订单"
    inputs = store.root / "jobs" / job_id / "inputs"
    assert (inputs / "knowledge" / "rules.md").is_file()
    assert not (inputs / "template.docx").exists()
    assert not list(inputs.glob("outline.*"))


def test_store_read_upgrades_archived_json_without_overwriting_it(tmp_path):
    from folio.store import Store

    sources = tmp_path / "sources"
    sources.mkdir()
    store = Store(tmp_path / "state")
    job_id = store.create(JobInput(title="业务需求", user_request="说明业务", knowledge_path=str(sources)))
    old_input = store.get(job_id)["input"] | {"template_path": "removed.docx", "outline_path": "removed.md"}
    old_plan = {"sections": [{"section_id": "sec_checkpoint", "outline_node_id": "o1",
                              "title": "业务", "level": 1, "display_number": "1",
                              "instruction": "说明业务", "outline_refs": ["o1"], "origin": "outline"}],
                "planning_method": "llm", "unused_template_slots": []}
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET input=?,plan=? WHERE id=?",
                           (json.dumps(old_input), json.dumps(old_plan), job_id))
    job = store.get(job_id)
    assert JobInput.model_validate(job["input"]).user_request == "说明业务"
    assert DocumentPlan.model_validate(job["plan"]).sections[0].section_id == "sec_checkpoint"
    with store.connect() as connection:
        row = connection.execute("SELECT input,plan FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert json.loads(row[0]) == old_input
    assert json.loads(row[1]) == old_plan


def test_cli_accepts_direct_requirements_and_needs_no_template(tmp_path, monkeypatch, capsys):
    import sys

    from folio.cli import main
    from folio.store import Store

    sources = tmp_path / "sources"
    sources.mkdir()
    request = tmp_path / "request.txt"
    request.write_text("说明订单及退款业务", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(sys, "argv", ["folio", "--data-dir", str(state), "generate",
                                    "--request-file", str(request), "--knowledge", str(sources),
                                    "--runtime", "demo", "--enqueue"])
    main()
    output = capsys.readouterr().out
    job_id = output.splitlines()[0].split("=", 1)[1]
    job = Store(state).get(job_id)
    assert job["input"]["user_request"] == "说明订单及退款业务"
    assert job["status"] == "queued"


def test_cli_rejects_removed_template_and_ambiguous_requirements(monkeypatch):
    import sys

    from folio.cli import main

    for extra in (["--template", "old.docx"], ["--request-file", "also.txt"]):
        monkeypatch.setattr(sys, "argv", ["folio", "generate", "--request", "业务说明", *extra])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
