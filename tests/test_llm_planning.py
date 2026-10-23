"""Directory generation validates output structure, not obsolete input mappings."""

import json

import pytest

from folio.document.plan_repair import DirectoryValidationError
from folio.document.planner import build_plan, planning_context
from folio.document.prepare import prepare_with_model
from folio.models import Limits, PlanItem, PlanProposal
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import PlanningContextTooLarge


def proposal():
    return PlanProposal(
        document_title="交易需求说明书",
        sections=[
            PlanItem(
                node_id="business",
                title="重新组织的业务需求",
                level=1,
                instruction="说明交易角色、输入、处理与异常。",
            ),
            PlanItem(
                node_id="approval",
                title="审批规则",
                level=2,
                instruction="说明交易撤销的审批要求。",
            ),
        ]
    )


def test_model_controls_titles_hierarchy_and_stable_identity():
    chosen = proposal()
    first = build_plan("doc", chosen)
    assert first == build_plan("doc", chosen)
    assert first.document_title == "交易需求说明书"
    assert [s.title for s in first.sections] == [s.title for s in chosen.sections]
    assert first.sections[1].parent_id == first.sections[0].section_id
    assert first.sections[1].display_number == "1.1"
    assert first.sections[1].heading_path == ["重新组织的业务需求", "审批规则"]
    assert first.sections[0].node_id == "business"


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("level", 2, "start at level 1"),
        ("instruction", "", "writing instruction"),
        ("title", " ", "title cannot be blank"),
    ],
)
def test_invalid_structure_is_rejected_without_rule_rewriting(field, value, message):
    chosen = proposal()
    setattr(chosen.sections[0], field, value)
    before = chosen.model_dump()
    with pytest.raises(DirectoryValidationError, match=message):
        build_plan("doc", chosen)
    assert chosen.model_dump() == before


def test_duplicate_titles_and_empty_containers_have_precise_repair_paths():
    chosen = proposal()
    chosen.sections[1].title = chosen.sections[0].title
    chosen.sections[1].level = 1
    chosen.sections[1].content_mode = "container"
    with pytest.raises(DirectoryValidationError) as caught:
        build_plan("doc", chosen)
    assert "Duplicate sibling" in str(caught.value)
    assert "container must have children" in str(caught.value)
    assert "/sections/1/title" in caught.value.repair_paths
    assert "/sections/1/content_mode" in caught.value.repair_paths


def test_context_keeps_requirements_and_source_catalog_without_synthetic_references():
    request = "需要完整保留的写作说明。" * 1000
    catalog = [
        {
            "source_id": "file-1",
            "name": "业务资料.xlsx",
            "headings": ["交易"],
            "excerpt": "金额与币种",
            "format": "xlsx",
        }
    ]
    assert planning_context(request, catalog) == {
        "requirements": request,
        "source_catalog": catalog,
    }


async def test_generation_entry_uses_model_topology(store, inputs):
    class Runtime(DemoRuntime):
        async def complete(self, task, context, schema, session):
            if task == "plan":
                assert context["requirements"] == inputs["user_request"]
                assert "outline" not in context and "template" not in context
                return RuntimeResult(proposal().model_dump_json())
            return await super().complete(task, context, schema, session)

    from folio.models import JobInput

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    result = await Engine(store, Runtime()).run(job_id)
    assert [s["title"] for s in result["plan"]["sections"]] == [
        s.title for s in proposal().sections
    ]


async def test_preview_repairs_only_failed_fields_and_keeps_full_context(inputs, tmp_path):
    calls = []

    class Runtime:
        async def complete(self, task, context, schema, session):
            calls.append((task, json.loads(json.dumps(context))))
            if task == "repair_plan":
                return RuntimeResult(
                    json.dumps(
                        {
                            "edits": [
                                {
                                    "op": "replace",
                                    "path": "/sections/0/instruction",
                                    "value_json": json.dumps(proposal().sections[0].instruction),
                                }
                            ]
                        }
                    )
                )
            chosen = proposal()
            chosen.sections[0].instruction = ""
            return RuntimeResult(chosen.model_dump_json())

    result = await prepare_with_model(
        inputs["user_request"], [], tmp_path / "preview", runtime=Runtime()
    )
    assert result["status"] == "layout_preview"
    assert result["title"] == "交易需求说明书"
    assert result["task_usage"][0]["task_id"] == "directory_planning"
    assert result["task_usage"][0]["model_calls"] >= 1
    assert result["task_usage"][0]["elapsed_ms"] >= 0
    assert [c[0] for c in calls] == ["plan", "repair_plan"]
    assert calls[1][1]["requirements"] == inputs["user_request"]
    assert calls[1][1]["rejected_proposal"]["sections"][0]["instruction"] == ""
    assert (tmp_path / "preview" / "planning-proposal.json").exists()


async def test_explicit_user_title_overrides_model_title(inputs, tmp_path):
    class Runtime:
        async def complete(self, *args):
            return RuntimeResult(proposal().model_dump_json())

    result = await prepare_with_model(
        inputs["user_request"],
        [],
        tmp_path / "preview",
        runtime=Runtime(),
        title="Fallback title",
        context_extra={"document_title_requirement": "User supplied title"},
    )
    assert result["title"] == "User supplied title"
    plan = json.loads((tmp_path / "preview" / "document-plan.json").read_text())
    assert plan["document_title"] == "User supplied title"


async def test_budget_failure_never_truncates_or_splits_requirement(inputs, tmp_path):
    inputs["user_request"] = "需要保留的完整说明" * 1000

    class Runtime:
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            raise AssertionError("Oversize context must fail before model call")

    runtime = Runtime()
    with pytest.raises(PlanningContextTooLarge):
        await prepare_with_model(
            inputs["user_request"],
            [],
            tmp_path / "preview",
            runtime=runtime,
            limits=Limits(max_planning_prompt_chars=4000),
        )
    assert runtime.calls == 0
    saved = json.loads((tmp_path / "preview" / "planning-input.json").read_text())
    assert saved["requirements"] == inputs["user_request"]


async def test_truncation_retries_with_full_sources_and_adjusted_output_budget(inputs, tmp_path):
    from folio.runtime.errors import ModelCallFailed

    class Runtime:
        def __init__(self):
            self.calls = []
            self.adjusted = False

        async def planning_retry_budget(self, task, context, schema, char_limit):
            assert task == "plan" and context["concise_plan_retry"]
            self.adjusted = True
            return {"previous_output_tokens": 16384, "retry_output_tokens": 24576}

        async def complete(self, task, context, schema, session):
            self.calls.append(context)
            if len(self.calls) == 1:
                raise ModelCallFailed("finish_reason=length", "length_limit")
            return RuntimeResult(proposal().model_dump_json())

    runtime, progress = Runtime(), []
    catalog = [{"source_id": "one", "name": "rules.md", "excerpt": "完整规则" * 100}]
    await prepare_with_model(
        inputs["user_request"],
        catalog,
        tmp_path / "preview",
        runtime=runtime,
        on_progress=progress.append,
    )
    first, second = runtime.calls
    assert second["concise_plan_retry"] and runtime.adjusted
    assert first["requirements"] == second["requirements"] == inputs["user_request"]
    assert first["source_catalog"] == second["source_catalog"] == catalog
    assert any("输出达到长度上限" in line for line in progress)
    attempts = json.loads((tmp_path / "preview/planning-attempts.json").read_text())
    assert attempts[0]["retry_budget"]["retry_output_tokens"] == 24576
    assert attempts[1]["validated"]


@pytest.mark.parametrize("makes_progress", [True, False])
async def test_extra_sparse_repair_requires_actual_reduction_in_errors(
    inputs, tmp_path, makes_progress
):
    broken = proposal()
    broken.sections = [
        broken.sections[0].model_copy(update={"node_id": node}) for node in ("a", "b", "c")
    ]
    broken.sections[0].instruction = ""

    class Runtime:
        def __init__(self):
            self.calls = []

        async def complete(self, task, current, schema, session):
            self.calls.append(task)
            if task == "plan":
                return RuntimeResult(broken.model_dump_json())
            if not makes_progress:
                edits = [{"op": "replace", "path": "/sections/0/instruction", "value_json": '""'}]
            elif len(self.calls) == 2:
                edits = [
                    {
                        "op": "replace",
                        "path": "/sections/0/instruction",
                        "value_json": '"说明业务"',
                    },
                    {"op": "remove", "path": "/sections/2"},
                ]
            else:
                edits = [{"op": "remove", "path": "/sections/1"}]
            return RuntimeResult(json.dumps({"edits": edits}))

    runtime = Runtime()
    operation = prepare_with_model(
        inputs["user_request"], [], tmp_path / "preview", runtime=runtime
    )
    if makes_progress:
        await operation
        assert runtime.calls == ["plan", "repair_plan", "repair_plan"]
    else:
        with pytest.raises(DirectoryValidationError):
            await operation
        assert runtime.calls == ["plan", "repair_plan"]


@pytest.mark.parametrize(
    "field,value,message,path",
    [
        ("node_id", "business", "Duplicate output node ID", "/sections/1/node_id"),
        ("level", 3, "cannot skip levels", "/sections/1/level"),
    ],
)
def test_node_identity_and_parent_levels_are_validated(field, value, message, path):
    chosen = proposal()
    setattr(chosen.sections[1], field, value)
    with pytest.raises(DirectoryValidationError, match=message) as caught:
        build_plan("doc", chosen)
    assert path in caught.value.repair_paths
