"""The confirmed node contract and source applicability travel with each review.

These views locate responsibilities and original passages. They do not classify
business terminology, waive findings, or infer facts from a document's title.
"""

import json
from copy import deepcopy

from folio.models import DrillDown
from folio.orchestration.visual_obligations import figure_policy

_NODE_FIELDS = {
    "section_id",
    "title",
    "heading_path",
    "instruction",
    "parent_id",
    "level",
    "content_mode",
    "expected_table_headers",
    "required_tables",
    "template_section_id",
    "allow_subheadings",
}


def _dump(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def section_review_scope(section_id, sections, states=(), user_request=""):
    """Same obligations for writer, local validation and final adjudication.

    Saved child requests establish *where* a diagram is assigned, not that its
    business semantics have already passed review. Missing unsaved children are not
    silently declared complete while generation is still running.
    """
    nodes = [_dump(value) for value in sections]
    definitions = {node["section_id"]: node for node in nodes}
    node = definitions[section_id]
    policy = figure_policy(section_id, nodes, user_request=user_request)
    saved = {_dump(state)["section_id"]: _dump(state) for state in states}
    children = [n for n in nodes if n.get("parent_id") == section_id]
    ancestors, parent, seen = [], node.get("parent_id"), {section_id}
    while parent in definitions and parent not in seen:
        seen.add(parent)
        ancestor = definitions[parent]
        ancestors.append({k: v for k, v in ancestor.items() if k in _NODE_FIELDS})
        parent = ancestor.get("parent_id")
    delegated = []
    for assignment in policy["delegated_to_children"]:
        for sid in assignment["section_ids"]:
            state = saved.get(sid)
            visuals = state.get("content", {}).get("visuals", []) if state else []
            delegated.append(
                {
                    "kind": assignment["kind"],
                    "section_id": sid,
                    "definition": {k: v for k, v in definitions[sid].items() if k in _NODE_FIELDS},
                    "state": "saved" if state else "not_saved_yet",
                    "revision": state.get("revision") if state else None,
                    "visual_requests": [
                        deepcopy(v) for v in visuals if v.get("kind") == assignment["kind"]
                    ],
                    "semantic_validation": "not_established_by_request_presence",
                }
            )
    return {
        "section_id": section_id,
        "definition": {k: v for k, v in node.items() if k in _NODE_FIELDS},
        "ancestors": list(reversed(ancestors)),
        "child_sections": [
            {k: v for k, v in child.items() if k in _NODE_FIELDS} for child in children
        ],
        "figure_policy": policy,
        "delegated_visuals": delegated,
        "responsibility": "parent_overview_and_shared_rules" if children else "node_instruction",
    }


def attach_review_scopes(context, sections, states=(), user_request=""):
    """Attach only request-local nodes; never the entire document body."""
    nodes = list(sections)
    known = {_dump(node)["section_id"] for node in nodes}
    ids = {context.get("section", {}).get("section_id")}
    ids.update(section.get("section_id") for section in context.get("sections", []))
    if not (ids & known):
        ids.update(section.get("section_id") for section in context.get("definitions", []))
    ids.update(context.get("finding", {}).get("section_ids", []))
    selected = [node for node in nodes if _dump(node)["section_id"] in ids]
    if not selected:
        return context
    return context | {
        "scope_contract": 1,
        "review_scopes": [
            section_review_scope(_dump(node)["section_id"], nodes, states, user_request)
            for node in selected
        ],
    }


def scoped_review_fields(context, section_ids=None):
    scopes = context.get("review_scopes", [])
    if section_ids is not None:
        scopes = [s for s in scopes if s["section_id"] in section_ids]
    return (
        {"scope_contract": context["scope_contract"], "review_scopes": deepcopy(scopes)}
        if context.get("scope_contract")
        else {}
    )


def applicability_targets(context):
    """Explicit fact/provenance candidates require located support, not a slogan."""
    finding = context.get("finding", {})
    if not context.get("scope_contract") or finding.get("kind") not in {
        "unsupported",
        "conflict",
        "coverage",
    }:
        return []
    explicit = {(r["section_id"], r["block_id"]) for r in finding.get("block_refs", [])}
    result = []
    for section in context.get("sections", []):
        for block in section.get("content", {}).get("blocks", []):
            key = (section["section_id"], block["block_id"])
            if (
                key in explicit
                and block.get("origin", "evidence") == "evidence"
                and block.get("kind", "paragraph") != "heading"
            ):
                result.append(key)
    return result


def applicability_schema(model, schema, context):
    if model is not DrillDown:
        return schema
    required = [
        key
        for key in schema.get("required", [])
        if key not in {"applicability_checks", "repair_targets"}
    ]
    if context.get("scope_contract"):
        required.append("repair_targets")
    schema["required"] = required
    repair = schema["$defs"]["ReviewRepairTarget"]
    repair["required"] = list(repair["properties"])
    repair["properties"]["section_id"]["enum"] = list(
        dict.fromkeys(context.get("finding", {}).get("section_ids", []))
    )
    targets = applicability_targets(context)
    if not targets:
        return schema
    schema["required"].append("applicability_checks")
    field = schema["properties"]["applicability_checks"]
    field.update(minItems=len(targets), maxItems=max(len(targets), 12))
    fields = schema["$defs"]["SourceApplicabilityCheck"]["properties"]
    fields["section_id"]["enum"] = list(dict.fromkeys(sid for sid, _ in targets))
    fields["block_id"]["enum"] = list(dict.fromkeys(bid for _, bid in targets))
    schema["$defs"]["SourceApplicabilityCheck"]["required"] = list(fields)
    return schema


def _original_sources(context):
    sources, fragments = {}, {}
    for item in context.get("original_materials", []):
        if item["kind"] != "evidence":
            continue
        value = item["value"]
        if "serialized_fragment" in value:
            fragments.setdefault(value["parent_ref"], []).append(value)
        elif "evidence_id" in value:
            sources[value["evidence_id"]] = value
    for pieces in fragments.values():
        pieces.sort(key=lambda p: p["start"])
        cursor = 0
        for piece in pieces:
            if piece["start"] != cursor:
                break
            cursor = piece["end"]
        else:
            if cursor == pieces[-1]["total_characters"]:
                value = json.loads("".join(p["serialized_fragment"] for p in pieces))
                sources[value["evidence_id"]] = value
    return sources


def _validate_original_quote(quote, source, *, index, field, evidence_id):
    # PDF wrapping and paragraph spacing may differ in an otherwise continuous
    # excerpt. Keep every non-whitespace character, including extraction defects;
    # guessing that a NUL means a dash could silently change a number or condition.
    normalized = " ".join(quote.split())
    if source is not None and normalized and normalized in " ".join(source["text"].split()):
        return
    location = json.dumps(evidence_id[:80], ensure_ascii=False)
    problem = (
        "must quote a continuous excerpt from the supplied complete original"
        if source is not None
        else "has no supplied complete original; select an evidence_id in original_materials"
    )
    raise ValueError(
        f"applicability_checks[{index}].{field} "
        f"(original_materials evidence_id={location}): {problem}; "
        "only whitespace differences are allowed. Do not join passages with ellipses "
        "or change numbers, subjects, punctuation, or extraction control characters."
    )


def validate_applicability(parsed, context):
    """Check traceable support and prevent reference/test results becoming approval.

    No keyword catalogue guesses the business domain. Semantic matching remains the
    reviewer's job, but its subject, source role, and quotes must be explicit and its
    verdict cannot claim certainty while its own checks say otherwise.
    """
    if not isinstance(parsed, DrillDown):
        return
    validate_repair_scope(parsed, context)
    targets = set(applicability_targets(context))
    if not targets:
        return
    checked = {(c.section_id, c.block_id) for c in parsed.applicability_checks}
    if checked != targets:
        raise ValueError(
            "Source applicability must cover exactly the finding's factual target blocks"
        )
    blocks = {
        (s["section_id"], b["block_id"]): b
        for s in context["sections"]
        for b in s.get("content", {}).get("blocks", [])
    }
    originals = _original_sources(context)
    uncertain = False
    invalid_support = False
    for index, check in enumerate(parsed.applicability_checks):
        block = blocks[check.section_id, check.block_id]
        bodies = [block.get("text", "")]
        if block.get("kind") == "table":
            # The table's title/description is delivered too. Match each actual
            # field independently so a quote cannot splice the caption into rows.
            bodies.append(json.dumps(block.get("rows", []), ensure_ascii=False))
        if not check.target_quote.strip() or not any(check.target_quote in body for body in bodies):
            raise ValueError("Applicability target_quote must quote the actual disputed body block")
        if not check.target_subject.strip() or not check.scope_reason.strip():
            raise ValueError(
                "Applicability must state target subject and explain scope, not only compare numbers"
            )
        if check.evidence_id:
            source = originals.get(check.evidence_id)
            for field in ("source_quote", "source_subject_quote"):
                _validate_original_quote(
                    getattr(check, field),
                    source,
                    index=index,
                    field=field,
                    evidence_id=check.evidence_id,
                )
        elif check.source_quote or check.source_subject_quote or check.applicability == "direct":
            raise ValueError("No source cannot establish direct support")
        if (
            check.applicability == "direct"
            and check.target_role in {"project_requirement", "project_description"}
            and check.source_role in {"product_capability", "test_result", "external_reference"}
        ):
            adoption = originals.get(check.adoption_evidence_id)
            if (
                adoption is None
                or adoption.get("metadata", {}).get("role") != "project_requirement"
                or not check.adoption_quote.strip()
                or check.adoption_quote not in adoption["text"]
            ):
                raise ValueError(
                    f"applicability_checks[{index}].target_role/adoption_evidence_id: "
                    "Product capability, benchmark or external reference cannot prove project adoption. "
                    f"target_role={check.target_role} denotes the user's own delivery project. "
                    "If the statement only reports third-party products, projects, or market facts, "
                    "classify it as source_reference and still verify the source subject and conditions. "
                    "If it asserts the user's own project requirements or adoption, keep its project role "
                    "and cite explicit project material or mark uncertain/reference_only; "
                    "do not relabel an actual adoption claim to bypass this check."
                )
        if check.adoption_evidence_id or check.adoption_quote:
            adoption = originals.get(check.adoption_evidence_id)
            if (
                adoption is None
                or not check.adoption_quote.strip()
                or check.adoption_quote not in adoption["text"]
            ):
                raise ValueError("Project adoption quote must have a supplied original")
        uncertain |= check.applicability == "uncertain"
        invalid_support |= check.applicability in {"reference_only", "conflict", "unsupported"}
    if uncertain and not parsed.needs_confirmation:
        raise ValueError(
            "Uncertain subject/adoption must remain needs_confirmation, not be dismissed as a false positive"
        )
    if invalid_support and not parsed.confirmed and not parsed.needs_confirmation:
        raise ValueError(
            "A finding cannot be dismissed while its own applicability check shows unsupported or mismatched facts"
        )


def validate_repair_scope(parsed, context):
    """A confirmed repair must not add a child's assigned figure to its parent.

    The model identifies the repair component in the same response. The guard uses
    the actual plan policy, never guesses from a finding key or prose keywords.
    """
    if not context.get("scope_contract"):
        return
    scopes = {s["section_id"]: s for s in context.get("review_scopes", [])}
    if parsed.repair_targets and (not parsed.confirmed or parsed.needs_confirmation):
        raise ValueError("Only a confirmed actionable finding may request structured repairs")
    if not parsed.confirmed or parsed.needs_confirmation:
        return
    delegated = {
        sid
        for sid in parsed.affected_section_ids
        if scopes.get(sid, {}).get("figure_policy", {}).get("delegated_to_children")
    }
    described = {target.section_id for target in parsed.repair_targets}
    if not delegated <= described:
        raise ValueError(
            "A repair of a node with delegated child obligations must identify repair_targets; "
            "do not confirm an unlocated parent diagram repair"
        )
    contents = {s["section_id"]: s.get("content", {}) for s in context.get("sections", [])}
    for target in parsed.repair_targets:
        if target.section_id not in parsed.affected_section_ids:
            raise ValueError("Structured repair target must name an affected section")
        if target.component != "visuals":
            if target.visual_kind:
                raise ValueError("Body/extraction repair cannot disguise a visual change")
            continue
        if not target.visual_kind:
            raise ValueError("Visual repair must identify its actual figure kind")
        policy = scopes.get(target.section_id, {}).get("figure_policy")
        if policy is None:
            raise ValueError("Visual repair must use a supplied confirmed node policy")
        if target.action == "remove":
            continue
        allowed = target.visual_kind in policy["allowed_visual_kinds"]
        retained = (
            target.action == "update"
            and any(
                v.get("kind") == target.visual_kind
                for v in contents.get(target.section_id, {}).get("visuals", [])
            )
            and target.visual_kind not in policy.get("prohibited_visual_kinds", [])
        )
        if not allowed and not retained:
            raise ValueError(
                "Visual repair conflicts with confirmed figure_policy: the figure belongs to "
                "a delegated child or is outside this node's allowed kinds; reassess the finding, "
                "do not ask the parent to add it"
            )
