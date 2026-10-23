"""Apply model-selected directory edits by stable node ID, preserving untouched content."""

import json
from typing import Annotated, Literal

from pydantic import Field

from folio.models import Model, PlanItem, PlanProposal


class MoveNode(Model):
    op: Literal["move"]
    node_id: str
    parent_id: str | None = None
    before_id: str | None = Field(default=None, description="Sibling ID under the destination parent; null appends at the end.")


class InsertNode(Model):
    op: Literal["insert"]
    node: PlanItem
    parent_id: str | None = None
    before_id: str | None = None


class RemoveNode(Model):
    op: Literal["remove"]
    node_id: str


class UpdateNode(Model):
    op: Literal["update"]
    node_id: str
    field: Literal[
        "title",
        "instruction",
        "content_mode",
    ]
    value_json: str = Field(description='JSON-encoded new field value; for renaming, e.g. "New title".')


class DirectoryRevision(Model):
    operations: list[
        Annotated[MoveNode | InsertNode | RemoveNode | UpdateNode, Field(discriminator="op")]
    ] = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)


def directory_index(proposal: PlanProposal):
    """Use the same preorder numbering as the rendered document, not model inference."""
    rows, stack, counts = [], [], [0] * 6
    for item in proposal.sections:
        if (not rows and item.level != 1) or (rows and item.level > rows[-1]["level"] + 1):
            raise ValueError("Existing directory has invalid levels")
        while stack and stack[-1].level >= item.level:
            stack.pop()
        counts[item.level - 1] += 1
        counts[item.level :] = [0] * (6 - item.level)
        rows.append(
            {
                "node_id": item.node_id,
                "number": ".".join(map(str, counts[: item.level])),
                "title": item.title,
                "level": item.level,
                "parent_id": stack[-1].node_id if stack else None,
            }
        )
        stack.append(item)
    if len({row["node_id"] for row in rows}) != len(rows):
        raise ValueError("Existing directory has duplicate node IDs")
    return rows


def apply_directory_revision(proposal: PlanProposal, revision: DirectoryRevision) -> PlanProposal:
    original = proposal.model_dump()
    items = {item.node_id: item.model_copy(deep=True) for item in proposal.sections}
    parents, children = {}, {None: []}
    for row in directory_index(proposal):
        nid, parent = row["node_id"], row["parent_id"]
        parents[nid] = parent
        children.setdefault(parent, []).append(nid)
        children.setdefault(nid, [])

    def require(nid):
        if nid not in items:
            raise ValueError(f"Unknown directory node ID: {nid}")

    def descendants(nid):
        result = [nid]
        for child in children[nid]:
            result.extend(descendants(child))
        return result

    for op in revision.operations:
        if op.op in {"move", "insert"}:
            nid = op.node.node_id if op.op == "insert" else op.node_id
            if op.parent_id is not None:
                require(op.parent_id)
            if op.op == "move":
                require(nid)
                if op.parent_id in descendants(nid):
                    raise ValueError("Cannot move a node into itself or its descendants")
            elif nid in items:
                raise ValueError(f"Inserted node ID already exists: {nid}")
            target = children[op.parent_id]
            if op.before_id is not None and (op.before_id not in target or op.before_id == nid):
                raise ValueError("before_id must be another child of the destination parent")
            if op.op == "move":
                children[parents[nid]].remove(nid)
            else:
                items[nid] = op.node.model_copy(deep=True)
                children[nid] = []
            parents[nid] = op.parent_id
            index = target.index(op.before_id) if op.before_id is not None else len(target)
            target.insert(index, nid)
        elif op.op == "remove":
            require(op.node_id)
            children[parents[op.node_id]].remove(op.node_id)
            for nid in descendants(op.node_id):
                del items[nid], parents[nid], children[nid]
        else:
            require(op.node_id)
            values = items[op.node_id].model_dump()
            values[op.field] = json.loads(op.value_json)
            items[op.node_id] = PlanItem.model_validate(values)

    sections = []

    def flatten(parent, level):
        for nid in children[parent]:
            sections.append(PlanItem.model_validate(items[nid].model_dump() | {"level": level}))
            flatten(nid, level + 1)

    flatten(None, 1)
    result = PlanProposal.model_validate(
        original | {"sections": [s.model_dump() for s in sections]}
    )
    if result.model_dump() == original:
        raise ValueError("The operations did not change the directory. Return actual edits required by the requested revision.")
    return result
