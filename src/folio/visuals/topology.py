"""Locate flow junctions for semantic review, without assigning business meaning."""

from collections import defaultdict

from folio.models import digest
from folio.visuals.review import _graph


def solid_graph(index):
    parsed = _graph(index)
    return (parsed[0], parsed[1]) if parsed else (set(), [])


def _components(nodes, edges):
    """Iterative SCC traversal avoids recursion limits and enumerating all paths."""
    forward, reverse = defaultdict(set), defaultdict(set)
    for left, right, _ in edges:
        forward[left].add(right)
        reverse[right].add(left)
    visited, order = set(), []
    for node in sorted(nodes):
        pending = [(node, False)]
        while pending:
            current, exiting = pending.pop()
            if exiting:
                order.append(current)
            elif current not in visited:
                visited.add(current)
                pending.append((current, True))
                pending.extend((child, False) for child in sorted(forward[current], reverse=True))
    visited.clear()
    for node in reversed(order):
        if node in visited:
            continue
        members, pending = set(), [node]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            members.add(current)
            pending.extend(reverse[current] - visited)
        if len(members) > 1 or node in forward[node]:
            yield members


def topology_relations(index):
    """A fork/merge/cycle is a question, not evidence of an invalid process."""
    nodes, edges = solid_graph(index)
    edges = sorted(set(edges))
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for edge in edges:
        outgoing[edge[0]].append(edge)
        incoming[edge[1]].append(edge)
    relations = []

    def add(kind, members, paths):
        paths = sorted(set(paths))
        members = sorted(members)
        relations.append(
            {
                "relation_id": "top_" + digest(str([kind, members, paths])),
                "kind": kind,
                "nodes": members,
                "edges": [
                    {
                        "edge_id": "edge_" + digest(str([left, right, condition])),
                        "from_node": left,
                        "to_node": right,
                        "condition": condition,
                    }
                    for left, right, condition in paths
                ],
            }
        )

    for node in sorted(nodes):
        if len(outgoing[node]) > 1:
            add("branch", [node], outgoing[node])
        if len(incoming[node]) > 1:
            add("merge", [node], incoming[node])
    for members in _components(nodes, edges):
        add("cycle", members, [e for e in edges if e[0] in members and e[1] in members])
    return relations
