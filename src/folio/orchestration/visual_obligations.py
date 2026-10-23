"""Conservative obligations from explicit document-writing instructions, never titles.

No model calls, filesystem access, or engine-state mutation.
"""

import re
from copy import deepcopy
from dataclasses import dataclass

_VISUALS = {
    "flowchart": re.compile(r"流程图|\bflow\s?chart\b|\bprocess diagram\b", re.I),
    "ui": re.compile(
        r"界面(?:示意|原型)?图|界面原型(?:图)?|\b(?:UI|screen|interface) "
        r"(?:mockup|prototype|diagram)\b|\bwireframe\b",
        re.I,
    ),
}
_SENTENCES = re.compile(
    r"[。；;\n]|\.(?=\s|$)|但是|但(?=必须|需要|应|要|提供|绘制|附)|而是|\bbut\b", re.I
)
_NEGATIVE = re.compile(
    r"无需|无须|禁止|省略|略去|删去|不(?:需要|要求|必|应|得|要|用|绘制|提供|包含|包括|附|生成|含)|"
    r"\b(?:no|without|do not|don't|must not|not required|not needed)\b",
    re.I,
)
_OPTIONAL = re.compile(
    r"按需|可选|必要时|如需|如有需要|如有必要|如果|若|视.{0,8}(?:情况|需要|需求)|"
    r"根据(?:实际)?(?:需要|需求)|(?:例[如子]|示例|举例|比如)|"
    r"可(?:以)?(?:按|根据|适当|选择|考虑|提供|绘制|输出|增加|添加|附)|"
    r"\b(?:optional(?:ly)?|if needed|if necessary|when needed|as needed|may)\b",
    re.I,
)
_SOFTWARE_FEATURE = re.compile(
    r"(?:系统|平台|工具|用户|操作员).{0,12}(?:支持|能够|可以|可|具备)|"
    r"\b(?:system|platform|application|user)\b.{0,24}\b(?:supports?|can)\b",
    re.I,
)
_OBJECT_FEATURE = re.compile(
    r"\s*(?:的)?(?:绘制|生成|编辑|查看|查询)?(?:功能|能力|工具|按钮|菜单|服务|接口)|"
    r"\s+(?:editing )?(?:feature|function|tool|button|menu|service|API)\b",
    re.I,
)
_ACTION = re.compile(
    r"(?:绘制|画出|提供|给出|展示|插入|输出|附上?|包括|包含|配上|添加|生成).{0,32}$|"
    r"\b(?:draw|create|generate|provide|include|add|attach|show|need|request)\b.{0,32}$",
    re.I,
)
_AND = re.compile(r"(?:及|以及|和|与)\s*(?:(?:相应|对应|相关)的?)?\s*$|\band\s*$", re.I)
_ALTERNATIVE = re.compile(r"或|任选|任一|二选一|\bor\b", re.I)


def _clauses(instruction):
    for sentence in _SENTENCES.split(instruction or ""):
        optional = feature = False
        for clause in re.split(r"[，,]", sentence):
            optional |= bool(_OPTIONAL.search(clause))
            feature |= bool(_SOFTWARE_FEATURE.search(clause))
            yield clause, optional, feature


def _prohibited_kinds(instruction, *, global_only=False):
    found = set()
    for clause, _, feature in _clauses(instruction):
        negative = _NEGATIVE.search(clause)
        if not negative or feature:
            continue
        if global_only and not re.fullmatch(
            r"\s*(?:(?:请|这次|本次|本文|文档|整个文档|全篇|所有章节|全部)\s*)*|"
            r"\s*(?:(?:please|this time|in this document|for all sections)\s*)*",
            clause[: negative.start()],
            re.I,
        ):
            # A user instruction scoped to another section must not ban this one.
            continue
        if global_only:
            objects = clause[negative.end() :]
            for pattern in _VISUALS.values():
                objects = pattern.sub("<figure>", objects)
            if not re.fullmatch(
                r"\s*(?:绘制|画|提供|生成|附|添加|任何|所有|的|a|any|draw|provide|generate|include|add|\s)*"
                r"<figure>(?:\s*(?:或|以及|及|和|、|/|or|and)\s*<figure>)*\s*",
                objects,
                re.I,
            ):
                continue
        found.update(kind for kind, pattern in _VISUALS.items() if pattern.search(clause))
    return found


@dataclass(frozen=True)
class VisualObligation:
    section_id: str
    kind: str
    instruction: str


def instruction_visual_kinds(instruction):
    """Recognize positive commands only; a diagram mention alone is not a request."""
    found = set()
    for clause, optional, feature in _clauses(instruction):
        if optional or feature or _NEGATIVE.search(clause):
            continue
        if (
            _ALTERNATIVE.search(clause)
            and sum(bool(pattern.search(clause)) for pattern in _VISUALS.values()) > 1
        ):
            continue
        for kind, pattern in _VISUALS.items():
            for mention in pattern.finditer(clause):
                if _OBJECT_FEATURE.match(clause[mention.end() :]):
                    continue
                prefix = clause[: mention.start()]
                if _ACTION.search(prefix) or _AND.search(prefix):
                    found.add(kind)
    return tuple(kind for kind in _VISUALS if kind in found)


def _delegable_kind(instruction, kind):
    """Only a generic parent figure instruction can be fulfilled by its children.

    Unknown scope, e.g. an independent overview, remains the parent's own request.
    """
    generic_prefix = re.compile(
        r"\s*(?:请|应|须|必须)?\s*(?:提供|包括|包含|附上?|绘制|生成|添加)"
        r"\s*(?:(?:相应|对应|相关)的?)?\s*|"
        r"\s*(?:please\s+)?(?:provide|include|attach|draw|generate)\s+(?:a\s+)?",
        re.I,
    )
    generic_suffix = re.compile(
        r"\s*(?:(?:和|及)(?:详细)?说明)?\s*|\s*(?:and explanation)?\s*", re.I
    )
    matched = False
    for clause, optional, feature in _clauses(instruction):
        if optional or feature or _NEGATIVE.search(clause):
            continue
        for mention in _VISUALS[kind].finditer(clause):
            if not (
                _ACTION.search(clause[: mention.start()]) or _AND.search(clause[: mention.start()])
            ):
                continue
            matched = True
            if not generic_prefix.fullmatch(
                clause[: mention.start()]
            ) or not generic_suffix.fullmatch(clause[mention.end() :]):
                return False
    return matched


def explicit_visual_obligations(sections):
    return [
        VisualObligation(section["section_id"], kind, section.get("instruction", ""))
        for section in sections
        for kind in instruction_visual_kinds(section.get("instruction", ""))
    ]


def descendants(sections, section_id):
    children = {}
    for section in sections:
        children.setdefault(section.get("parent_id"), []).append(section["section_id"])
    result, pending = set(), list(children.get(section_id, []))
    while pending:
        child = pending.pop()
        if child == section_id or child in result:
            continue
        result.add(child)
        pending.extend(children.get(child, []))
    return result


def required_visual_kinds(section_id, sections):
    """Explicit child requests own the actual figure; do not duplicate it in parents."""
    obligations = explicit_visual_obligations(sections)
    owned = {item.kind for item in obligations if item.section_id == section_id}
    below = descendants(sections, section_id)
    instruction = next(s.get("instruction", "") for s in sections if s["section_id"] == section_id)
    delegated = {
        item.kind
        for item in obligations
        if item.section_id in below and _delegable_kind(instruction, item.kind)
    }
    return tuple(kind for kind in _VISUALS if kind in owned - delegated)


def _permitted_kinds(instruction):
    """An explicit optional figure may be chosen, but a UI feature is not a picture."""
    found = set()
    for clause, _, feature in _clauses(instruction):
        if feature or _NEGATIVE.search(clause):
            continue
        for kind, pattern in _VISUALS.items():
            for mention in pattern.finditer(clause):
                if not _OBJECT_FEATURE.match(clause[mention.end() :]):
                    # Unknown grammar with an explicit figure name is allowed,
                    # never upgraded into a mandatory obligation.
                    found.add(kind)
    return found


def figure_policy(section_id, sections, user_request=""):
    """Allow only confirmed figure instructions; retain their optional conditions.

    Ancestor instructions can authorize a figure in their subtree. An explicitly
    delegated parent obligation does not authorize another copy in that parent.
    A separate optional parent request remains available with its original text.
    The policy restricts new generation; it never edits already saved figures.
    """
    definitions = {section["section_id"]: section for section in sections}
    node = definitions[section_id]
    instructions, visited = [], set()
    current = node
    while current and current["section_id"] not in visited:
        visited.add(current["section_id"])
        instructions.append(current.get("instruction", ""))
        current = definitions.get(current.get("parent_id"))
    instructions.append(user_request)
    allowed, conditional = set(), []
    for instruction in instructions:
        allowed.update(_permitted_kinds(instruction))
        for sentence in _SENTENCES.split(instruction or ""):
            if _permitted_kinds(sentence) and (
                _OPTIONAL.search(sentence) or _ALTERNATIVE.search(sentence)
            ):
                conditional.append(sentence.strip())
    owned = set(instruction_visual_kinds(node.get("instruction", "")))
    below = descendants(sections, section_id)
    obligations = explicit_visual_obligations(sections)
    delegated = [
        {
            "kind": kind,
            "section_ids": sorted(
                {o.section_id for o in obligations if o.kind == kind and o.section_id in below}
            ),
        }
        for kind in _VISUALS
        if kind in owned
        and _delegable_kind(node.get("instruction", ""), kind)
        and any(o.kind == kind and o.section_id in below for o in obligations)
    ]
    optional_owned = set()
    for sentence in _SENTENCES.split(node.get("instruction", "")):
        if _OPTIONAL.search(sentence):
            optional_owned.update(_permitted_kinds(sentence))
    allowed.difference_update(d["kind"] for d in delegated if d["kind"] not in optional_owned)
    # Explicit user-wide prohibitions override section instructions. Ambiguous
    # section-scoped prose is kept as guidance, not widened into a global ban.
    prohibited = _prohibited_kinds(node.get("instruction", "")) | _prohibited_kinds(
        user_request, global_only=True
    )
    required = tuple(k for k in required_visual_kinds(section_id, sections) if k not in prohibited)
    allowed.update(required)
    allowed.difference_update(prohibited)
    return {
        "allowed_visual_kinds": [kind for kind in _VISUALS if kind in allowed],
        "required_visual_kinds": list(required),
        "prohibited_visual_kinds": [kind for kind in _VISUALS if kind in prohibited],
        "delegated_to_children": delegated,
        "conditional_instructions": list(dict.fromkeys(conditional)),
    }


def missing_visual_obligations(sections, states, user_request=""):
    """Final check uses actual saved visuals across each obligation's own subtree."""
    present = {
        state["section_id"]: {v["kind"] for v in state.get("content", {}).get("visuals", [])}
        for state in states
    }
    definitions = {s["section_id"]: s for s in sections}
    prohibited = _prohibited_kinds(user_request, global_only=True)
    return [
        item
        for item in explicit_visual_obligations(sections)
        if item.kind not in prohibited
        if not any(
            item.kind in present.get(sid, set())
            for sid in {item.section_id}
            | (
                descendants(sections, item.section_id)
                if _delegable_kind(definitions[item.section_id].get("instruction", ""), item.kind)
                else set()
            )
        )
    ]


def visual_obligation_schema(schema, required_kinds, allowed_kinds=None, existing_visuals=()):
    """Simple decoder-compatible shape; validate_required_visuals enforces every kind.

    Do not use JSON Schema contains (not supported by all compatible decoders),
    or constrain all items to one kind (which would forbid useful additional figures).
    """
    required = set(required_kinds) & _VISUALS.keys()
    existing = {(v["figure_id"], v["kind"]) for v in existing_visuals}
    if not required and allowed_kinds is None and not existing:
        return schema
    result = deepcopy(schema)
    field = result["properties"]["visuals"]
    if allowed_kinds is not None:
        allowed = set(allowed_kinds) & _VISUALS.keys()
        if not required <= allowed:
            raise ValueError("Required figure types must remain allowed")
        possible = allowed | {kind for _, kind in existing}
        if possible:
            visual = result["$defs"]["VisualRequest"]["properties"]["kind"]
            visual["enum"] = sorted(possible)
            if not allowed:
                result["$defs"]["VisualRequest"]["properties"]["figure_id"]["enum"] = sorted(
                    {figure_id for figure_id, _ in existing}
                )
        else:
            field["maxItems"] = 0
        field["description"] = (
            "Add only figure kinds explicitly permitted by the writing requirements. Existing current.visuals may be retained or revised under the same figure_id+kind; do not delete an existing figure because no new figure was requested, or use its kind as permission to add others. Return [] when neither new nor existing figures are allowed."
        )
    minimum = len(existing) + len(required - {kind for _, kind in existing})
    if minimum:
        field["minItems"] = max(field.get("minItems", 0), minimum)
        result["required"] = list(dict.fromkeys([*result.get("required", []), "visuals"]))
    if not required:
        return result
    field["description"] = (
        "This section explicitly requires figures. Provide each of the following kinds in visuals: "
        + ", ".join(sorted(required))
        + ". Drawing advice in the body is not a substitute for figure requests; figure content and citations must still be based on this section's body."
    )
    result["required"] = list(dict.fromkeys([*result.get("required", []), "visuals"]))
    return result


def validate_required_visuals(content, required_kinds, allowed_kinds=None, existing_visuals=()):
    """Additional shape check only. Existing full-text/evidence validation stays intact."""
    required = set(required_kinds) & _VISUALS.keys()
    present = {v["kind"] for v in content.get("visuals", [])}
    existing = {(v["figure_id"], v["kind"]) for v in existing_visuals}
    if allowed_kinds is not None:
        unexpected = [
            v
            for v in content.get("visuals", [])
            if v["kind"] not in allowed_kinds and (v["figure_id"], v["kind"]) not in existing
        ]
        if unexpected:
            raise ValueError(
                "Figure type is outside confirmed writing instructions: "
                + ", ".join(sorted({v["kind"] for v in unexpected}))
            )
    missing_existing = existing - {
        (v.get("figure_id"), v["kind"]) for v in content.get("visuals", [])
    }
    if missing_existing:
        raise ValueError(
            "Existing figures must be preserved during repair: "
            + ", ".join(sorted(figure_id for figure_id, _ in missing_existing))
        )
    missing = sorted(required - present)
    if missing:
        raise ValueError("Explicit section figure requirement missing: " + ", ".join(missing))


def visual_obligation_findings(sections, states, user_request=""):
    """An empty directory container cannot be repaired by rewriting its own body."""
    from folio.models import Finding

    definitions = {section["section_id"]: section for section in sections}
    result = []
    for item in missing_visual_obligations(sections, states, user_request=user_request):
        container = definitions[item.section_id].get("content_mode") == "container"
        result.append(
            Finding(
                kind="pending" if container else "coverage",
                key="required-visual:" + item.kind,
                description=(
                    'An outline container requires a figure, but no child section owns it. Confirm the body node responsible: '
                    if container
                    else 'An explicitly required figure has not been generated: '
                )
                + item.instruction,
                section_ids=[item.section_id],
                rule="coverage",
            )
        )
    return result
