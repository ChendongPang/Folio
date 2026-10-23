"""Validate a generated document directory and derive stable section metadata."""

from folio.document.plan_repair import DirectoryValidationError
from folio.models import DocumentPlan, PlanProposal, Section, digest


def planning_context(requirements: str, source_catalog: list[dict]) -> dict:
    """Provide the writing request and prepared source metadata directly to planning."""
    return {"requirements": requirements, "source_catalog": source_catalog}


def build_plan(document_id: str, proposal: PlanProposal, *, template=None) -> DocumentPlan:
    """Check directory structure, without inventing source coverage requirements."""
    errors, repair_paths = [], set()
    stack, siblings, ids = [], {}, {}
    for index, item in enumerate(proposal.sections):
        base = f"/sections/{index}"
        if item.node_id in ids:
            errors.append(f"Duplicate output node ID: path={base}/node_id, node={item.node_id}")
            repair_paths.add(base + "/node_id")
        ids[item.node_id] = index
        if not item.title.strip():
            errors.append(f"Output title cannot be blank: path={base}/title")
            repair_paths.add(base + "/title")
        if (index == 0 and item.level != 1) or (
            index and item.level > proposal.sections[index - 1].level + 1
        ):
            errors.append(
                f"Output directory must start at level 1 and cannot skip levels: path={base}/level"
            )
            repair_paths.add(base + "/level")
        if item.content_mode == "generate" and not item.instruction.strip():
            errors.append(
                f"Content node needs a writing instruction: path={base}/instruction, title={item.title}"
            )
            repair_paths.add(base + "/instruction")
        while stack and proposal.sections[stack[-1]].level >= item.level:
            stack.pop()
        sibling = (stack[-1] if stack else None, item.title.strip().casefold())
        if sibling in siblings:
            first = siblings[sibling]
            details = []
            for position in (first, index):
                node = proposal.sections[position]
                end = position + 1
                while end < len(proposal.sections) and proposal.sections[end].level > node.level:
                    end += 1
                details.append(
                    {"index": position, "node_id": node.node_id, "descendants": end - position - 1}
                )
            errors.append(
                f"Duplicate sibling title: indices=[{first},{index}], title={item.title!r}; "
                f"nodes={details}; "
                "Merge duplicate coverage or distinguish titles based on actual business scope; do not rename arbitrarily to pass validation. "
                "Retain unique writing requirements and children, deleting only duplicates; prefer retaining an existing complete subtree. "
                "Keep the first node title, position and subtree; only merge its writing requirements and adjust later duplicates."
            )
            repair_paths.add(f"/sections/{first}/instruction")
            repair_paths.add(base)
            repair_paths.update(base + "/" + field for field in ("title", "instruction"))
        else:
            siblings[sibling] = index
        stack.append(index)
        if item.content_mode == "container" and (
            index + 1 == len(proposal.sections) or proposal.sections[index + 1].level <= item.level
        ):
            errors.append(
                f"A directory container must have children: path={base}, title={item.title!r}; "
                "Change this leaf to generate and fill instruction. Continue planning the directory only; do not write the body."
            )
            repair_paths.update({base + "/content_mode", base + "/instruction"})
    if errors:
        raise DirectoryValidationError("\n".join(errors), repair_paths)

    stack, counts, sections = [], [0] * 6, []
    for item in proposal.sections:
        while stack and stack[-1].level >= item.level:
            stack.pop()
        parent = stack[-1] if stack else None
        counts[item.level - 1] += 1
        counts[item.level :] = [0] * (6 - item.level)
        section = Section(
            section_id="sec_" + digest(f"{document_id}:{item.node_id}"),
            node_id=item.node_id,
            title=item.title,
            level=item.level,
            instruction=item.instruction,
            display_number=".".join(map(str, counts[: item.level])),
            parent_id=parent.section_id if parent else None,
            heading_path=([*parent.heading_path] if parent else []) + [item.title],
            content_mode=item.content_mode,
        )
        sections.append(section)
        stack.append(section)
    document_title = " ".join(proposal.document_title.split()).strip() or None
    plan = DocumentPlan(
        document_title=document_title,
        sections=sections,
        notes=proposal.notes,
    )
    if template is not None:
        from folio.document.template_contract import bind_template

        try:
            bind_template(plan, template)
        except ValueError as exc:
            # A missing fixed chapter can require a subtree edit; retry the full
            # proposal instead of guessing which unrelated fields may be changed.
            raise DirectoryValidationError(str(exc), []) from exc
    return plan
