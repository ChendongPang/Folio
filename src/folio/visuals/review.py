"""Ground visual diagnoses in quoted sources and the actual Mermaid parser graph.

Graph checks can refute a claim about an absent connection. They cannot decide
whether a business constraint applies: that remains an explicit model diagnosis.
Unsupported syntax or missing anchors never becomes permission to pass a figure.
"""

import html
import re
from typing import Literal

from pydantic import Field, model_validator

from folio.models import Model


class VisualFinding(Model):
    verdict: Literal["violation", "satisfied", "advisory"] = Field(
        default="violation",
        description="violation=the figure actually violates an explicit source requirement; satisfied=already met; advisory=optional advice. "
        "Legacy records default to violation. Check structural issues such as missing nodes/paths against the actual graph; satisfied/advisory cannot waive them.",
    )
    kind: Literal[
        "missing_path",
        "forbidden_path",
        "missing_edge",
        "forbidden_edge",
        "missing_node",
        "semantic",
    ]
    source_block_id: str = Field(min_length=1, max_length=160)
    source_quote: str = Field(min_length=1, max_length=800)
    from_node: str = Field(default="", max_length=160)
    to_node: str = Field(default="", max_length=160)
    condition: str = Field(default="", max_length=400)
    description: str = Field(min_length=1, max_length=1200)


class VisualReview(Model):
    # Kept for reading pre-contract audit files. New diagnoses use findings.
    problems: list[str] = Field(default_factory=list, max_length=8)
    findings: list[VisualFinding] = Field(default_factory=list, max_length=8)


class VisualDifference(Model):
    required_behavior: str = Field(min_length=1, max_length=500)
    actual_behavior: str = Field(min_length=1, max_length=500)
    actual_quote: str = Field(
        min_length=1, max_length=800, description="Exact quotation of the noncompliant text or statement from the current figure source"
    )


class LiveVisualFinding(VisualFinding):
    # Compatibility defaults belong only to stored legacy records. The live
    # model must choose explicitly; omitted classifications cannot become faults.
    verdict: Literal["violation", "satisfied", "advisory"]
    difference: VisualDifference | None = Field(
        description="Required actual difference for semantic violations; null for correct/equivalent explanations and structural checks"
    )

    @model_validator(mode="after")
    def actual_difference(self):
        if self.kind == "semantic" and self.verdict == "violation":
            if self.difference is None:
                raise ValueError("Semantic violations require expected and actual behavior")
            if _label(self.difference.required_behavior) == _label(self.difference.actual_behavior):
                raise ValueError("Identical behavior is not a semantic difference")
        return self


class LiveVisualReview(VisualReview):
    findings: list[LiveVisualFinding] = Field(default_factory=list, max_length=8)


REVIEW_CONTRACT = {
    "version": 6,
    "instruction": (
        "problems remains for compatibility; fresh reviews must return problems=[] and place actual issues in findings. "
        "For each item, supply source_block_id and an exact source_quote, and locate from_node/to_node in the source. "
        "missing_path/forbidden_path describe directed reachability; missing_edge/forbidden_edge describe direct edges. "
        "Put branch conditions in condition. Use missing_node only for an explicitly missing node ID; otherwise use semantic. "
        "graph_index is the read-only graph actually parsed by the renderer; do not report an existing path as missing. "
        "Do not equate no document-matching requirement with no approval requirement; independent configuration implies neither mandatory approval nor approval exemption. "
        "Keep unlocatable references unresolved; do not force edits as if they were confirmed faults. "
        "Use verdict=violation only for actual violations within this figure visual_scope. "
        "Always provide verdict explicitly. Semantic violations also require difference: required_behavior states the original requirement, "
        "actual_behavior states the different behavior shown, and actual_quote quotes the current source exactly to locate it. "
        "Set difference=null for structural checks, satisfied items or optional advice. Identical or equivalent behaviors are not differences. "
        "Use satisfied for correctly represented semantic checks and advisory for optional aesthetic or minor terminology suggestions. "
        "Both semantic categories still need original evidence and a real from_node or to_node to be verified. "
        "Use visual.description to determine flow boundaries; do not require every prerequisite or other subprocess from supporting materials to appear again. "
        "Combined nodes may represent multiple actions; independent optional actions need not connect. Do not demand direct edges where directed reachability suffices. "
        "Check missing nodes or branches using the appropriate structural kind; do not disguise them as advisory/satisfied to waive them. "
        "Preserve source alternatives: for example, warning or termination must not become automatic termination for every warning. "
        "Do not turn optional, possible or conditional outcomes into one mandatory action, or turn alternatives into mandatory parallel execution. "
        "Absence of contradiction is not evidence: generic approval does not support returning on failure, and generic risk blocking does not support process termination. "
        "Keep unsupported key branches unresolved; do not use satisfied to approve invented business logic through common knowledge."
    ),
}


def _label(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<br\s*/?>", " ", value))).strip()


def _graph(index):
    if not isinstance(index, dict) or index.get("complete") is not True:
        return None
    try:
        nodes = {item["id"] for item in index["nodes"]}
        edges = []
        complete = True
        for edge in index["edges"]:
            start, end = edge["from_node"], edge["to_node"]
            if start not in nodes or end not in nodes:
                complete = False
                continue
            # Annotation, invisible and undirected links cannot prove business flow.
            if edge.get("stroke", "normal") not in {"normal", "thick"}:
                complete = False
                continue
            kind = edge.get("type")
            if kind not in {"arrow_point", "double_arrow_point"}:
                complete = False
                continue
            edges.append((start, end, _label(edge.get("label", ""))))
            if kind == "double_arrow_point":
                edges.append((end, start, _label(edge.get("label", ""))))
        return nodes, edges, complete
    except (KeyError, TypeError, AttributeError):
        return None


def _reachable(start, end, edges):
    pending, seen = [start], set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        for left, right, _ in edges:
            if left == node:
                if right == end:
                    return True
                pending.append(right)
    return False


def assess_review(review, context, graph_index):
    """Return unresolved diagnostics and auditable, deterministically refuted claims."""
    source_text = {
        block["block_id"]: block.get("text", "")
        + "\n"
        + "\n".join(" | ".join(row) for row in block.get("rows", []))
        for block in context.get("source_blocks", [])
    }
    graph = _graph(graph_index)
    problems, dismissed, unverified = [], [], []
    for finding in review.findings:
        quote = finding.source_quote
        if quote not in source_text.get(finding.source_block_id, ""):
            unverified.append(finding.description)
            continue
        if finding.kind == "semantic" and finding.verdict != "violation":
            # Positive/optional commentary is not a repair instruction. Require
            # an actual graph anchor; unlocated assertions cannot pass a figure.
            if graph and {finding.from_node, finding.to_node}.intersection(graph[0]):
                dismissed.append(
                    {
                        "finding": finding.model_dump(),
                        "reason": "语义核对已满足或仅为可选建议，非业务违例",
                    }
                )
            else:
                unverified.append(finding.description)
            continue
        refuted = False
        if finding.kind != "semantic" and graph:
            nodes, edges, complete = graph
            start, end = finding.from_node, finding.to_node
            if finding.kind == "missing_node":
                if not start:
                    unverified.append(finding.description)
                    continue
                refuted = start in nodes
            elif start in nodes and end in nodes:
                if finding.kind.endswith("edge"):
                    exists = any(
                        left == start
                        and right == end
                        and (not finding.condition or label == _label(finding.condition))
                        for left, right, label in edges
                    )
                elif finding.condition:
                    # Conditional reachability requires semantic path interpretation.
                    unverified.append(finding.description)
                    continue
                else:
                    exists = _reachable(start, end, edges)
                # A known solid path proves existence even when other edges
                # are annotations or unsupported. Absence needs a complete
                # directed graph: never discard a possible counterexample.
                if not exists and not complete:
                    unverified.append(finding.description)
                    continue
                refuted = (finding.kind.startswith("missing") and exists) or (
                    finding.kind.startswith("forbidden") and not exists and complete
                )
            else:
                unverified.append(finding.description)
                continue
        elif finding.kind != "semantic":
            unverified.append(finding.description)
            continue
        if refuted:
            dismissed.append({"finding": finding.model_dump(), "reason": "实际图结构与该指控不符"})
        else:
            problems.append(finding.description)
    descriptions = {finding.description for finding in review.findings}
    unverified.extend(problem for problem in review.problems if problem not in descriptions)
    # Unanchored legacy diagnoses are retained, never silently treated as passed.
    return {
        "problems": list(dict.fromkeys(problems)),
        "unverified": list(dict.fromkeys(unverified)),
        "dismissed": dismissed,
    }
