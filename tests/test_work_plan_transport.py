import json

import httpx
import jsonschema
import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.orchestration.work import WorkPlan
from folio.runtime.llm import LLMRuntime
from folio.runtime.work_plan_transport import restore_work_plan, work_plan_payload


def context():
    return {
        "sections": [
            {
                "section_id": "sec_long_" + str(i),
                "number": str(i + 1),
                "heading_path": ["业务需求", str(i)],
                "instruction": "需要正文",
            }
            for i in range(48)
        ],
        "execution_budget": {"max_sections_per_task": 8, "minimum_tasks": 6},
    }


def proposal():
    return {
        "tasks": [
            {
                "task_id": "t" + str(i),
                "title": "业务" + str(i),
                "node_refs": ["n" + str(j + 1) for j in range(i * 8, (i + 1) * 8)],
                "objective": "同一业务问题",
                "questions": ["规则是什么？"],
                "depends_on": [],
                "completion_criteria": ["有依据且完整"],
                "rationale": "按业务关联分组",
            }
            for i in range(6)
        ],
    }


def test_group_schema_bounds_size_and_restores_complete_canonical_ownership():
    original = context()
    snapshot = json.dumps(original)
    wire, schema, refs = work_plan_payload(original, WorkPlan.model_json_schema())
    assert json.dumps(original) == snapshot
    field = schema["$defs"]["WorkTask"]["properties"]["node_refs"]
    assert field["maxItems"] == 8 and field["minItems"] == 1
    assert field["items"]["enum"] == ["n" + str(i + 1) for i in range(48)]
    assert "section_ids" not in schema["$defs"]["WorkTask"]["properties"]
    jsonschema.validate(proposal(), schema)
    result = WorkPlan.model_validate(restore_work_plan(proposal(), refs))
    assert len(result.tasks) == 6 and all(len(t.section_ids) == 8 for t in result.tasks)
    assert [sid for t in result.tasks for sid in t.section_ids] == [
        s["section_id"] for s in original["sections"]
    ]
    assert wire["sections"][0]["section_id"] == "n1"
    assert "node_refs" not in result.model_dump_json()


def test_wire_selects_node_ownership_before_describing_the_task_without_mutating_schema():
    original = WorkPlan.model_json_schema()
    snapshot = json.dumps(original)
    _, schema, _ = work_plan_payload(context(), original)
    task = schema["$defs"]["WorkTask"]
    assert list(task["properties"]) == [
        "task_id",
        "node_refs",
        "title",
        "objective",
        "questions",
        "completion_criteria",
        "depends_on",
        "rationale",
    ]
    assert task["required"][:2] == ["task_id", "node_refs"]
    assert json.dumps(original) == snapshot


def test_noncontiguous_node_groups_preserve_exact_ownership():
    source = context()
    source["sections"] = source["sections"][:4]
    source["execution_budget"] = {"max_sections_per_task": 3, "minimum_tasks": 2}
    _, schema, refs = work_plan_payload(source, WorkPlan.model_json_schema())
    data = {
        "tasks": [
            proposal()["tasks"][0] | {"node_refs": ["n3", "n1"]},
            proposal()["tasks"][1] | {"node_refs": ["n4", "n2"]},
        ]
    }
    jsonschema.validate(data, schema)
    restored = WorkPlan.model_validate(restore_work_plan(data, refs))
    assert restored.tasks[0].section_ids == ["sec_long_0", "sec_long_2"]
    assert restored.tasks[1].section_ids == ["sec_long_1", "sec_long_3"]


def test_nodes_first_schema_preserves_fixed_tasks_and_missing_node_retry_aliases():
    source = context()
    _, _, original_refs = work_plan_payload(source, WorkPlan.model_json_schema())
    fixed = restore_work_plan(proposal(), original_refs)["tasks"]
    fixed[0]["section_ids"].remove("sec_long_0")
    retry = source | {
        "sections": [source["sections"][0]],
        "fixed_work_tasks": fixed,
        "execution_budget": {"max_sections_per_task": 8, "minimum_tasks": 1},
    }
    wire, schema, refs = work_plan_payload(retry, WorkPlan.model_json_schema())
    task = schema["$defs"]["WorkTask"]
    assert list(task["properties"])[:3] == ["task_id", "node_refs", "title"]
    assert task["properties"]["node_refs"]["items"]["enum"] == ["n1"]
    assert not set(t["task_id"] for t in fixed) & set(task["properties"]["task_id"]["enum"])
    assert wire["fixed_work_tasks"] == fixed
    data = {
        "tasks": [
            proposal()["tasks"][0] | {"task_id": "t6", "node_refs": ["n1"], "depends_on": ["t0"]}
        ]
    }
    jsonschema.validate(data, schema)
    restored = WorkPlan.model_validate(restore_work_plan(data, refs, fixed))
    assert [t.model_dump() for t in restored.tasks[:-1]] == [
        WorkPlan.model_validate({"tasks": [t]}).tasks[0].model_dump() for t in fixed
    ]
    assert restored.tasks[-1].section_ids == ["sec_long_0"]
    assert restored.tasks[-1].depends_on == ["t0"]


def test_schema_rejects_oversized_group_and_retry_retains_previous_grouping():
    original = context()
    _, schema, refs = work_plan_payload(original, WorkPlan.model_json_schema())
    oversized = proposal()
    oversized["tasks"][0]["node_refs"].append("n9")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(oversized, schema)
    too_few = proposal()
    too_few["tasks"].pop()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(too_few, schema)
    # Duplicate/coverage checks stay deterministic; deployed decoder has no uniqueItems.
    assert "uniqueItems" not in json.dumps(schema)
    original["previous_invalid_work_plan"] = restore_work_plan(proposal(), refs)
    original["format_feedback"] = "sec_long_0 需要修改归属"
    wire, _, _ = work_plan_payload(original, WorkPlan.model_json_schema())
    assert wire["previous_invalid_work_plan"] == proposal()
    assert wire["format_feedback"] == "n1 需要修改归属"


def test_transport_preserves_real_output_dependencies_without_title_based_pruning():
    _, _, refs = work_plan_payload(context(), WorkPlan.model_json_schema())
    data = proposal()
    data["tasks"][0]["title"] = "前台需求"
    data["tasks"][1].update(
        title="清算需求与追溯汇总",
        depends_on=["t0"],
        rationale="汇总必须使用t0新分配的需求编号，原材料没有这些新增编号。",
    )
    restored = WorkPlan.model_validate(restore_work_plan(data, refs))
    assert restored.tasks[1].depends_on == ["t0"]
    assert restored.tasks[1].rationale == data["tasks"][1]["rationale"]
    assert restored.tasks[0].depends_on == []


@pytest.mark.parametrize(
    "bad", ["missing", "extra", "duplicate_task", "duplicate_node", "empty", "wrong_type"]
)
def test_bad_ownership_is_rejected_without_assigning_nodes_by_rules(bad):
    _, _, refs = work_plan_payload(context(), WorkPlan.model_json_schema())
    data = proposal()
    if bad == "missing":
        data["tasks"][0]["node_refs"].pop()
    elif bad == "extra":
        data["tasks"][0]["node_refs"][0] = "n49"
    elif bad == "duplicate_task":
        data["tasks"].append(data["tasks"][0])
    elif bad == "duplicate_node":
        data["tasks"][0]["node_refs"][0] = "n9"
    elif bad == "empty":
        data["tasks"][0]["node_refs"] = []
    else:
        data["tasks"][0]["node_refs"] = "n1"
    with pytest.raises(ValueError):
        restore_work_plan(data, refs)


async def test_real_transport_sends_bounded_groups_and_measures_actual_messages(tmp_path):
    settings = ModelSettingsStore(tmp_path)
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            structured_output="json_schema",
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    source = context()
    schema = WorkPlan.model_json_schema()

    def handler(request):
        data = json.loads(request.content)
        wire = json.loads(data["messages"][1]["content"])
        assert wire["sections"][0]["section_id"] == "n1"
        actual = data["response_format"]["json_schema"]["schema"]
        visible = json.JSONDecoder().raw_decode(
            data["messages"][0]["content"].split("JSON Schema:\n", 1)[1]
        )[0]
        assert visible == actual
        assert list(visible["$defs"]["WorkTask"]["properties"])[:3] == [
            "task_id",
            "node_refs",
            "title",
        ]
        assert list(actual["$defs"]["WorkTask"]["properties"])[:3] == [
            "task_id",
            "node_refs",
            "title",
        ]
        assert actual["$defs"]["WorkTask"]["properties"]["node_refs"]["maxItems"] == 8
        dependency = actual["$defs"]["WorkTask"]["properties"]["depends_on"]
        assert "block execution" in dependency["description"]
        assert "cannot be obtained directly from sources" in dependency["description"]
        system = data["messages"][0]["content"]
        assert "It is not an ordering arrow in a business process" in system
        assert "never remove real dependencies merely to meet a concurrency target" in system
        assert runtime.prompt_chars("work_plan", source, schema) == sum(
            len(m["content"]) for m in data["messages"]
        )
        response = {
            "choices": [{"delta": {"content": json.dumps(proposal())}, "finish_reason": "stop"}]
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.complete("work_plan", source, schema, "test")
        restored = WorkPlan.model_validate_json(result.content)
        assert restored.tasks[0].section_ids[0] == "sec_long_0"
    finally:
        await runtime.aclose()


async def test_incomplete_grouping_is_carried_into_engine_retry(store, inputs):
    from folio.models import JobInput
    from folio.orchestration.engine import Engine
    from folio.runtime.base import RuntimeResult
    from folio.runtime.demo import DemoRuntime
    from folio.runtime.work_plan_transport import WorkPlanFormatError

    class Planning(DemoRuntime):
        calls = 0

        async def complete(self, task, current, schema, session):
            self.calls += 1
            _, _, refs = work_plan_payload(current, schema)
            if self.calls == 1:
                partial = proposal()
                partial["tasks"][0]["node_refs"].remove("n1")
                with pytest.raises(WorkPlanFormatError) as failure:
                    restore_work_plan(partial, refs)
                raise failure.value
            assert len(current["sections"]) == 1
            assert current["sections"][0]["section_id"] == "sec_long_0"
            assert len(current["fixed_work_tasks"]) == 6
            assert "previous_invalid_work_plan" not in current
            patch = {"tasks": [proposal()["tasks"][0] | {"task_id": "t6", "node_refs": ["n1"]}]}
            return RuntimeResult(
                json.dumps(restore_work_plan(patch, refs, current["fixed_work_tasks"]))
            )

    runtime = Planning()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    from types import SimpleNamespace

    engine.plan = SimpleNamespace(
        sections=[
            SimpleNamespace(section_id=s["section_id"], content_mode="generate")
            for s in context()["sections"]
        ]
    )
    result = await engine._call("work_plan", context(), WorkPlan)
    assert runtime.calls == 2
    assert len({sid for task in result.tasks for sid in task.section_ids}) == 48
    rejected = list((store.root / "jobs" / engine.job_id / "contexts/rejected").glob("*.json"))
    assert len(rejected) == 1
