import json

import pytest

from folio.document.plan_repair import PlanRepair, apply_plan_repair
from folio.models import PlanProposal


def edits(*items):
    return PlanRepair.model_validate({"edits": items})


def test_edits_preserve_other_nodes_and_original_proposal():
    original = {
        "sections": [{"title": "A", "instruction": None}, {"title": "B"}],
        "notes": ["错标"],
    }
    result = apply_plan_repair(
        original,
        edits(
            {
                "op": "replace",
                "path": "/sections/0/instruction",
                "value_json": json.dumps("s"),
            },
            {"op": "replace", "path": "/notes", "value_json": "[]"},
            {"op": "add", "path": "/sections/-", "value_json": '{"title":"C"}'},
        ),
    )
    assert result["sections"] == [
        {"title": "A", "instruction": "s"},
        {"title": "B"},
        {"title": "C"},
    ]
    assert result["notes"] == []
    assert original["sections"][0]["instruction"] is None
    assert len(original["sections"]) == 2


@pytest.mark.parametrize(
    "path", ["/sections/-1", "/sections/8", "/private", "sections/0", "/sections/0/missing"]
)
def test_invalid_repair_target_is_rejected(path):
    with pytest.raises(ValueError):
        apply_plan_repair(
            {"sections": [{"title": "A"}]},
            edits(
                {"op": "replace", "path": path, "value_json": "null"},
            ),
        )


def test_patch_does_not_bypass_full_proposal_validation():
    result = apply_plan_repair(
        {"sections": [{"title": "A"}]},
        edits(
            {"op": "remove", "path": "/sections/0"},
        ),
    )
    with pytest.raises(ValueError):
        PlanProposal.model_validate(result)


def test_repair_cannot_remove_a_valid_writing_instruction():
    original = {"sections": [{"instruction": "valid"}], "notes": []}
    with pytest.raises(ValueError, match="fields that failed"):
        apply_plan_repair(
            original,
            edits(
                {
                    "op": "remove",
                    "path": "/sections/0/instruction",
                }
            ),
            allowed_paths=["/notes"],
        )
    assert original["sections"][0]["instruction"] == "valid"
