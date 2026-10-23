import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from folio.document.plan_repair import PlanRepair, apply_plan_repair
from folio.runtime.plan_repair_transport import repair_transport, restore_repair


def context():
    return {
        "rejected_proposal": {
            "sections": [
                {"node_id": "a", "title": "Overview", "level": 1, "instruction": None},
                {"node_id": "b", "title": "Child", "level": 2},
                {"node_id": "c", "title": "Second", "level": 1},
                {"node_id": "d", "title": "Duplicate", "level": 1},
            ]
        },
        "repair_paths": [
            "/sections/0",
            "/sections/1",
            "/sections/3",
            "/sections/0/instruction",
        ],
    }


def test_typed_values_translate_and_deletes_use_original_indices():
    original = context()
    before = deepcopy(original)
    schema = repair_transport("repair_plan", original, PlanRepair.model_json_schema())
    value = {
        "replacements": {"/sections/0/instruction": "t1"},
        "remove_indices": [1, 3],
        "append_sections": [],
    }
    Draft202012Validator(schema).validate(value)
    restored = restore_repair(value)
    repair = PlanRepair.model_validate(restored)
    assert repair.edits[0].value_json == json.dumps("t1")
    assert [e.path for e in repair.edits[1:]] == ["/sections/3", "/sections/1"]
    result = apply_plan_repair(original["rejected_proposal"], repair, original["repair_paths"])
    assert [n["node_id"] for n in result["sections"]] == ["a", "c"]
    assert result["sections"][0]["instruction"] == "t1"
    assert original == before


def test_parent_removal_unknown_fields_and_conflicting_operations_are_rejected():
    schema = repair_transport("repair_plan", context(), PlanRepair.model_json_schema())
    assert not Draft202012Validator(schema).is_valid(
        {"replacements": {}, "remove_indices": [0], "append_sections": []}
    )
    assert not Draft202012Validator(schema).is_valid(
        {
            "replacements": {"/sections/2/title": "changed"},
            "remove_indices": [],
            "append_sections": [],
        }
    )
    assert restore_repair(
        {
            "replacements": {"/sections/1/title": "changed"},
            "remove_indices": [1],
            "append_sections": [],
        }
    ) == {"edits": [{"op": "remove", "path": "/sections/1"}]}
    with pytest.raises(ValueError, match="unique"):
        restore_repair({"replacements": {}, "remove_indices": [1, 1], "append_sections": []})
    with pytest.raises(ValueError, match="at least one edit"):
        restore_repair({"replacements": {}, "remove_indices": [], "append_sections": []})


@pytest.mark.parametrize("mode", ["prompt", "json_schema"])
def test_visible_repair_contract_uses_final_typed_fields(mode):
    from folio.runtime.llm import LLMRuntime

    runtime = LLMRuntime.__new__(LLMRuntime)
    runtime.config = {"structured_output": mode}
    messages, schema, *_ = runtime.request_parts(
        "repair_plan", context(), PlanRepair.model_json_schema()
    )
    system = messages[0]["content"]
    visible = json.JSONDecoder().raw_decode(system.split("JSON Schema:\n", 1)[1])[0]
    assert visible == schema
    assert set(visible["required"]) == {"replacements", "remove_indices", "append_sections"}
    assert "edits" not in visible["properties"]
    assert 'Output structure: {"edits"' not in system
    assert "do not return edits or value_json" in system
