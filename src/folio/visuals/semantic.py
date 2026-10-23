"""Bounded second look at semantic disputes; never guess from positive keywords."""

import re
from typing import Literal

from pydantic import Field, model_validator

from folio.models import Model
from folio.visuals.review import LiveVisualReview, VisualDifference
from folio.visuals.topology import solid_graph, topology_relations


class GroundedDifference(VisualDifference):
    source_block_id: str = Field(min_length=1, max_length=160)
    source_quote: str = Field(min_length=1, max_length=800)
    from_node: str = Field(default="", max_length=160)
    to_node: str = Field(default="", max_length=160)
    description: str = Field(min_length=1, max_length=1200)
    actual_path: list[str] = Field(default_factory=list, max_length=320)


class EdgeSupport(Model):
    edge_id: str = Field(min_length=1, max_length=80)
    basis: Literal["evidence", "model_proposal", "uncertain"]
    source_block_id: str = Field(max_length=160)
    source_quote: str = Field(max_length=800)
    reason: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def grounded_support(self):
        if self.basis != "uncertain" and not (
            self.source_block_id.strip() and self.source_quote.strip()
        ):
            raise ValueError("A supported edge needs a source block and exact quotation")
        return self


class SemanticDecision(Model):
    case_id: str = Field(min_length=1, max_length=80)
    result: Literal["actual_difference", "equivalent", "uncertain"]
    reason: str = Field(min_length=1, max_length=800)
    differences: list[GroundedDifference] = Field(max_length=8)
    checked_relation_ids: list[str] = Field(default_factory=list, max_length=1024)
    edge_support: list[EdgeSupport] = Field(default_factory=list, max_length=1024)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        # Old audit objects remain readable; fresh replies must explicitly state
        # coverage (an empty list for cases without topology relations).
        schema["required"] = list(
            dict.fromkeys([*schema.get("required", []), "checked_relation_ids", "edge_support"])
        )
        return schema

    @model_validator(mode="after")
    def consistent(self):
        if bool(self.differences) != (self.result == "actual_difference"):
            raise ValueError("Only actual_difference supplies grounded differences")
        return self


class SemanticReview(Model):
    checks: list[SemanticDecision] = Field(max_length=10)


def choice_sources(context):
    """Point out explicit outcome alternatives, without interpreting them as AND.

    These are review questions, never automatic violations or keyword waivers.
    Every matched sentence remains intact; the complete sources accompany it.
    """
    result = []
    for block in context.get("source_blocks", []):
        text = (
            block.get("text", "")
            + "\n"
            + "\n".join(" | ".join(row) for row in block.get("rows", []))
        )
        for sentence in re.split(r"(?<=[。；;\n])", text):
            if re.search(r"(?:触发|执行|选择|采取|进行)[^。；;\n]{1,100}或|\b(?:trigger|execute|choose|select|perform|take)\b[^。；;\n]{1,100}\bor\b", sentence, re.I):
                result.append({"source_block_id": block["block_id"], "source_quote": sentence})
    return result


def semantic_cases(review, checked, context, graph_index=None):
    # Old audit objects retain their conservative treatment. Fresh model output
    # uses the explicit live contract and can request independent adjudication.
    disputed = set(checked["problems"] + checked["unverified"])
    cases = (
        [
            {"case_id": f"semantic_{i}", "finding": finding.model_dump()}
            for i, finding in enumerate(review.findings)
            if finding.kind == "semantic" and finding.description in disputed
        ]
        if isinstance(review, LiveVisualReview)
        else []
    )
    if choices := choice_sources(context):
        cases.append({"case_id": "outcome_alternatives", "source_choices": choices})
    if isinstance(review, LiveVisualReview) and (relations := topology_relations(graph_index)):
        # Primary "satisfied" is not evidence that alternate paths were inspected.
        # All junctions share one second request with any existing semantic cases.
        cases.append({"case_id": "topology_paths", "relations": relations})
    return cases


SEMANTIC_CONTRACT = {
    "phase": "semantic_difference_adjudication",
    "instruction": (
        "This is an independent second-stage check of candidate findings; candidate findings are not facts. Return checks with exactly one item per case_id. "
        "Fully compare source_blocks with the current source/graph_index, assessing substantive behavioral differences only within this figure scope. "
        "Each actual_difference must include exact source_quote from the original and exact actual_quote from current figure source. "
        "required_behavior and actual_behavior must describe the specific differing business behaviors and locate real graph nodes. "
        "Use equivalent for no difference or equivalent expression and uncertain when unverified; differences must be empty in both cases. "
        "A positive explanation is not a fault just because an outer field says violation; words such as correct do not excuse actual violations. "
        "For outcome_alternatives, check whether source alternatives were narrowed or strengthened into a single outcome. "
        "For example, warning or termination does not mean terminating every warning. Allow conditional choice; do not require both alternative paths to execute. "
        "For topology_paths, independently check every branch, merge and cycle; do not reuse an initial satisfied verdict as the conclusion. "
        "checked_relation_ids must include every relation_id in this case. Check all inbound and outbound paths, "
        "whether actual object identity, state and explicit source preconditions still hold, and whether paths bypass required checks. "
        "Judge only actual source requirements; do not add ordering, checks or terminal states based on business common knowledge. "
        "Do not mistake a different object or a new transaction for revival of the same transaction. If identity/conditions are not explicit, use uncertain. "
        "Topology indicates locations to check, not violations. Merges and cycles may be valid; general knowledge cannot replace source evidence. "
        "Each actual_difference in this case must also give actual_path, listing in order nodes on a real directed solid-edge path in the current graph "
        "through the branch/merge/cycle under review. Missing locations or incomplete relation_id coverage preclude equivalent. "
        "An equivalent topology_paths result also requires edge_support with exactly one entry per distinct edge_id in relations. "
        "checked_relation_ids alone or a blanket assertion of correctness is insufficient. Check each edge action, condition, destination and state transition. "
        "basis=evidence may cite only origin=evidence body text; basis=model_proposal may cite only "
        "existing origin=model_proposal additions. Quote source_quote exactly and explain support for that particular edge. "
        "Use edge_support=[] for other cases. Check an edge only once even if it appears in several relations. "
        "Absence of contradiction is not evidence: internal approval alone does not support returning to entry on rejection, "
        "and risk blocking alone does not support permanent termination. Generic approval/risk-control actions do not establish their failure destinations. "
        "For unspecified destinations or conditions, set that edge basis=uncertain and explain missing evidence without inferring from common knowledge. "
        "Do not invent a design addition and pretend it came from existing red-highlighted body text. Valid model_proposal sources may support a diagram, "
        "while retaining proposal provenance, not claiming verified business fact. Any uncertain edge requires an overall uncertain result. "
        "Use this checks schema, not the original review field structure."
    ),
}


def reconcile_semantics(result, cases, checked, context, source, graph_index):
    expected = {case["case_id"]: case for case in cases}
    if len(result.checks) != len(expected) or {c.case_id for c in result.checks} != set(expected):
        raise ValueError("Semantic review must cover every disputed case exactly once")
    sources = {
        b["block_id"]: b.get("text", "")
        + "\n"
        + "\n".join(" | ".join(r) for r in b.get("rows", []))
        for b in context.get("source_blocks", [])
    }
    source_origins = {
        b["block_id"]: b.get("origin", "evidence") for b in context.get("source_blocks", [])
    }
    nodes = {n["id"] for n in (graph_index or {}).get("nodes", [])}
    _, edges = solid_graph(graph_index)
    directed_edges = {(left, right) for left, right, _ in edges}
    output = {key: list(values) for key, values in checked.items()}
    for decision in result.checks:
        case = expected[decision.case_id]
        description = case.get("finding", {}).get("description")
        relations = case.get("relations", [])
        if relations:
            expected_ids = {r["relation_id"] for r in relations}
            if set(decision.checked_relation_ids) != expected_ids or len(
                decision.checked_relation_ids
            ) != len(expected_ids):
                output["unverified"].append(
                    "拓扑语义复核未逐项覆盖全部分支、合并点及回路，尚未确认。"
                )
                continue
            if decision.result == "equivalent":
                edges_by_id = {
                    edge["edge_id"]: edge for relation in relations for edge in relation["edges"]
                }
                support_ids = [support.edge_id for support in decision.edge_support]
                if set(support_ids) != set(edges_by_id) or len(support_ids) != len(edges_by_id):
                    output["unverified"].append(
                        "拓扑语义复核缺少逐条路径的来源说明，不能仅凭equivalent认定全部分支有依据。"
                    )
                    continue
                uncertain = []
                for support in decision.edge_support:
                    edge = edges_by_id[support.edge_id]
                    if (
                        support.basis == "uncertain"
                        or support.source_quote not in sources.get(support.source_block_id, "")
                        or support.basis != source_origins.get(support.source_block_id)
                    ):
                        uncertain.append(
                            f"路径 {edge['from_node']} → {edge['to_node']}"
                            f"（{edge['condition'] or '无条件标签'}）来源尚未确认：{support.reason}"
                        )
                if uncertain:
                    output["unverified"].extend(uncertain)
                    continue
        # No unanchored or equal-sided assertion is accepted as an actual fault.
        for difference in decision.differences:
            anchors = nodes.intersection({difference.from_node, difference.to_node})
            if (
                difference.source_quote not in sources.get(difference.source_block_id, "")
                or difference.actual_quote not in source
                or difference.required_behavior.strip() == difference.actual_behavior.strip()
                or (
                    graph_index is not None
                    and not any(
                        re.search(r"(?<!\w)" + re.escape(node) + r"(?!\w)", difference.actual_quote)
                        for node in anchors
                    )
                )
            ):
                raise ValueError("Semantic difference is not grounded in current source and graph")
            if relations:
                path = difference.actual_path
                relation_nodes = {node for relation in relations for node in relation["nodes"]}
                if (
                    len(path) < 2
                    or not set(path) <= nodes
                    or not relation_nodes.intersection(path)
                    or any(
                        (left, right) not in directed_edges
                        for left, right in zip(path, path[1:], strict=False)
                    )
                    or not anchors.intersection(path)
                ):
                    raise ValueError(
                        "Topology difference requires a real directed path through its reviewed junction"
                    )
        if description:
            for field in ("problems", "unverified"):
                output[field] = [value for value in output[field] if value != description]
        if decision.result == "actual_difference":
            output["problems"].extend(d.description for d in decision.differences)
        elif decision.result == "uncertain":
            output["unverified"].append(decision.reason)
        else:
            output["dismissed"].append(
                {"case": case, "reason": decision.reason, "method": "independent_semantic_check"}
            )
    return output
