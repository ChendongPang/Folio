import json

import pytest

from folio.document.directory_revision import (
    DirectoryRevision,
    apply_directory_revision,
    directory_index,
)
from folio.models import PlanItem, PlanProposal


def existing():
    return PlanProposal(
        sections=[
            PlanItem(
                node_id=nid,
                title=title,
                level=level,
                instruction="保留原写作要求",
            )
            for nid, title, level in [
                ("a", "功能", 1),
                ("a1", "原功能", 2),
                ("b", "附录", 1),
                ("b1", "待归类业务", 2),
                ("child", "业务详情", 3),
                ("b2", "参考", 2),
            ]
        ]
    )


def change(*operations):
    return DirectoryRevision(operations=list(operations), summary="根据建议调整")


def test_move_subtree_preserves_content_references_and_unrelated_nodes():
    old = existing()
    snapshot = old.model_dump()
    revised = apply_directory_revision(
        old,
        change(
            {"op": "move", "node_id": "b1", "parent_id": "a1"},
        ),
    )
    assert old.model_dump() == snapshot
    assert [s.node_id for s in revised.sections] == ["a", "a1", "b1", "child", "b", "b2"]
    for item in revised.sections:
        before = next(s for s in old.sections if s.node_id == item.node_id)
        assert item.model_dump(exclude={"level"}) == before.model_dump(exclude={"level"})
    indexed = {r["node_id"]: r for r in directory_index(revised)}
    assert indexed["b1"]["number"] == "1.1.1"
    assert indexed["child"]["number"] == "1.1.1.1"
    assert indexed["b2"]["number"] == "2.1"
    assert indexed["b1"]["parent_id"] == "a1"


def test_ordered_moves_use_stable_ids_and_before_sibling():
    revised = apply_directory_revision(
        existing(),
        change(
            {"op": "move", "node_id": "b1", "parent_id": "a", "before_id": "a1"},
            {"op": "move", "node_id": "b2", "parent_id": "a", "before_id": "a1"},
        ),
    )
    assert [s.node_id for s in revised.sections] == ["a", "b1", "child", "b2", "a1", "b"]


@pytest.mark.parametrize(
    "operation,message",
    [
        ({"op": "move", "node_id": "b", "parent_id": "child"}, "descendants"),
        ({"op": "move", "node_id": "missing", "parent_id": "a"}, "Unknown"),
        ({"op": "move", "node_id": "b1", "parent_id": "missing"}, "Unknown"),
        ({"op": "move", "node_id": "b1", "parent_id": "a", "before_id": "b2"}, "before_id"),
        ({"op": "move", "node_id": "b2", "parent_id": "b"}, "did not change the directory"),
    ],
)
def test_invalid_or_noop_moves_never_modify_original(operation, message):
    old = existing()
    snapshot = old.model_dump()
    with pytest.raises(ValueError, match=message):
        apply_directory_revision(old, change(operation))
    assert old.model_dump() == snapshot


def test_explicit_insert_update_remove_and_reference_translation():
    old = existing()
    revised = apply_directory_revision(
        old,
        change(
            {
                "op": "update",
                "node_id": "b2",
                "field": "title",
                "value_json": json.dumps("保留资料"),
            },
            {
                "op": "insert",
                "parent_id": "a",
                "node": {
                    "node_id": "new",
                    "title": "新增项",
                    "level": 1,
                    "instruction": "按用户要求描述业务",
                },
            },
            {"op": "remove", "node_id": "b1"},
        ),
    )
    indexed = {s.node_id: s for s in revised.sections}
    assert indexed["b2"].title == "保留资料"
    assert indexed["new"].level == 2
    assert "b1" not in indexed and "child" not in indexed
