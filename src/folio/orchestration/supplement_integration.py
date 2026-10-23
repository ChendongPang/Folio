"""Integrate one supplement at a time, preserving every untouched document block."""

from copy import deepcopy

from pydantic import Field

from folio.document.presentation import validate_document_content
from folio.models import Block, Model, SectionContent
from folio.orchestration.context import gap_material_catalog, project
from folio.orchestration.supplement_prefetch import SupplementSelections
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted

FIGURE_ANCHOR_INSTRUCTION = (
    "current.visuals source_block_ids and after_block_id reference existing body blocks. Do not delete those blocks or replace them with new IDs. Integrate supplemental content by updating the original block_id in place through updates, preserving figure references. A block_id must not occur in both updates and remove_block_ids."
)


class SupplementIntegration(Model):
    updates: list[Block] = Field(default_factory=list, max_length=20)
    remove_block_ids: list[str] = Field(default_factory=list, max_length=20)
    resolved_pending_items: list[str] = Field(default_factory=list, max_length=20)


def validate_checked_patch(current, patch, proposal):
    """A final placement edit cannot rewrite a business proposal after its source check."""
    if proposal.get("disposition") == "record_only":
        return
    approved = proposal["content"].strip()
    old = {block.block_id: block for block in current.blocks}
    for block in patch.updates:
        original = old.get(block.block_id)
        if original is not None and block.text == original.text and block.rows == original.rows:
            continue
        if (
            block.kind != "paragraph"
            or block.text.strip() != approved
            or block.origin != "model_proposal"
        ):
            raise ValueError("Placement cannot change a source-checked supplement")
    result = {bid: block for bid, block in old.items() if bid not in patch.remove_block_ids}
    result.update({block.block_id: block for block in patch.updates})
    if not any(
        block.origin == "model_proposal" and block.text.strip() == approved
        for block in result.values()
    ):
        raise ValueError("Checked supplement was not integrated")


def validate_protected_blocks(current, patch, protected_block_ids=()):
    """A later item cannot revoke another accepted item's exact document blocks."""
    known = {block.block_id: block for block in current.blocks}
    protected = set(protected_block_ids)
    if not protected <= known.keys():
        raise ValueError("Protected supplement block is missing")
    if protected.intersection(patch.remove_block_ids) or any(
        block.block_id in protected and block != known[block.block_id] for block in patch.updates
    ):
        raise ValueError("Supplement cannot change another accepted supplement's protected block")


def exact_proposal_blocks(current, proposals):
    texts = {
        proposal["content"]
        for proposal in proposals
        if proposal.get("disposition", "body") == "body" and not proposal.get("evidence_refresh")
    }
    return {
        block.block_id
        for block in current.blocks
        if block.origin == "model_proposal" and not block.evidence_ids and block.text in texts
    }


def accepted_patch_blocks(current, patch, proposal):
    # A replay may deduplicate an identical addition under an existing block ID.
    # Protect that retained block too; no approximate text comparison is allowed.
    payloads = [block.model_dump(exclude={"block_id"}) for block in patch.updates]
    return exact_proposal_blocks(current, [proposal]) | {
        block.block_id
        for block in current.blocks
        if block.model_dump(exclude={"block_id"}) in payloads
    }


def is_table_completion(existing, added):
    """An exact same-caption table with only blank cells filled is one table.

    This is a rejection guard, not a fuzzy merge: different captions, facts,
    provenance, row order or shape may describe distinct tables and stay intact.
    """
    if (
        existing.kind != "table"
        or added.kind != "table"
        or not existing.text.strip()
        or existing.text != added.text
        or existing.origin != added.origin
        or set(existing.evidence_ids) != set(added.evidence_ids)
        or len(existing.rows) != len(added.rows)
        or len(existing.rows) < 2
        or existing.rows[0] != added.rows[0]
        or any(
            not any(cell.strip() and not cell.strip().isdecimal() for cell in row)
            for row in existing.rows[1:]
        )
    ):
        return False
    filled = False
    for before, after in zip(existing.rows, added.rows, strict=True):
        if len(before) != len(after):
            return False
        for old, new in zip(before, after, strict=True):
            if old.strip() and old != new:
                return False
            if not old.strip() and new.strip():
                filled = True
    return filled


def apply_integration(
    current, patch, evidence_ids, *, model_supplement=True, protected_block_ids=()
):
    """Existing block IDs replace in place; new IDs append. Everything else is immutable."""
    validate_protected_blocks(current, patch, protected_block_ids)
    known = {block.block_id: block for block in current.blocks}
    updates = {block.block_id: block for block in patch.updates}
    removed = set(patch.remove_block_ids)
    if len(updates) != len(patch.updates) or len(removed) != len(patch.remove_block_ids):
        raise ValueError("Duplicate supplement edit target")
    if not removed <= known.keys() or removed & updates.keys():
        raise ValueError("Unknown or overlapping supplement edit target")
    figure_anchors = {
        block_id
        for visual in current.visuals
        for block_id in [*visual.source_block_ids, visual.after_block_id]
    }
    if anchored_removals := removed & figure_anchors:
        raise ValueError(
            "Supplement cannot remove figure source or insertion blocks: "
            + ", ".join(sorted(anchored_removals))
            + "; update the existing block in place and keep its block_id"
        )
    # A supplement is not authority to delete established facts or table structure.
    if any(known[bid].origin != "model_proposal" or known[bid].kind == "table" for bid in removed):
        raise ValueError("Supplement cannot remove established document content")
    for block in patch.updates:
        if not set(block.evidence_ids) <= set(evidence_ids):
            raise ValueError("Supplement uses unknown evidence")
        old = known.get(block.block_id)
        if old is None:
            duplicate = next(
                (table for table in known.values() if is_table_completion(table, block)), None
            )
            if duplicate is not None:
                raise ValueError(
                    "Supplement must fill the existing table block "
                    + duplicate.block_id
                    + "; do not append another copy of the same table"
                )
        same_content = old is not None and all(
            getattr(old, field) == getattr(block, field)
            for field in ("kind", "text", "rows", "heading_level")
        )
        if old is not None and old.origin == "evidence" and not same_content:
            raise ValueError("Supplement cannot rewrite established facts; add a separate block")
        if (
            old is not None
            and old.evidence_ids
            and old.origin == "evidence"
            and block.origin != "evidence"
        ):
            raise ValueError("Supplement cannot reclassify cited facts as model content")
        if block.origin == "model_proposal" and block.evidence_ids:
            raise ValueError("Model supplement cannot claim evidence provenance")
        if model_supplement and (not same_content or (old and old.origin == "model_proposal")):
            if block.origin != "model_proposal":
                raise ValueError("New supplement content must retain model provenance")
        if not model_supplement and block.origin == "evidence" and not block.evidence_ids:
            raise ValueError("Retrieved additions must cite original evidence")
        if (
            old is not None
            and old.kind == "table"
            and (block.kind != "table" or block.rows[0] != old.rows[0])
        ):
            raise ValueError("Supplement cannot remove an existing table header")
    if not set(patch.resolved_pending_items) <= set(current.pending_items):
        raise ValueError("Supplement resolved an unknown pending item")
    blocks = [updates.get(b.block_id, b) for b in current.blocks if b.block_id not in removed]
    for block in patch.updates:
        if block.block_id in known:
            continue
        # Recovery may already contain exactly this model supplement. Its new
        # output ID does not make identical prose/provenance new document content.
        if block.origin == "model_proposal" and any(
            old.model_dump(exclude={"block_id"}) == block.model_dump(exclude={"block_id"})
            for old in blocks
        ):
            continue
        blocks.append(block)
    candidate = SectionContent(
        blocks=blocks,
        visuals=current.visuals,
        pending_items=[p for p in current.pending_items if p not in patch.resolved_pending_items],
        record_only_pending=current.record_only_pending,
    )
    validate_document_content(candidate, evidence_ids)
    return candidate


def retain_pending(current, proposal, reason):
    label = proposal.get("gap_description") or proposal.get("content", "")[:80]
    message = f"补充内容尚待确认：{label}。{reason}"
    if any(message in item for item in current.pending_items):
        return current
    pending = list(dict.fromkeys([*current.pending_items, message]))
    # Keep every existing item; combine the added explanation when the schema is full.
    if len(pending) > 50:
        pending = pending[:49] + ["；".join(pending[49:])]
    return current.model_copy(update={"pending_items": pending})


def integration_source(source, proposal, description):
    """The immutable selection inputs; document state is attached only at consumption."""
    item = project(
        "repair",
        {
            key: value
            for key, value in source.items()
            if key not in {"current", "protected_block_ids", "existing_evidence_ids"}
        }
        | {
            "model_proposals": [] if proposal.get("evidence_refresh") else [proposal],
            "integration_item": proposal
            | ({"gap_description": description} if description else {}),
            "integration_policy": 2,
            "required_project_evidence_ids": [],
            "repair_instruction": "Integrate only the single supplement in integration_item. Preserve unrelated blocks unchanged and add no business design beyond that proposal. protected_block_ids belong to other accepted supplements: do not edit or delete them, and omit them from updates when unchanged. Preserve the red model-proposal provenance of business supplements; record_only must not create business body content."
            + FIGURE_ANCHOR_INSTRUCTION,
        },
    )
    item.pop("integration_gaps", None)
    return item


async def integrate_supplements(engine, source, proposals, *, supported_gaps=()):
    from folio.orchestration.supplement_conflicts import (
        SupplementCheckBudget,
        check_supplement_conflicts,
    )

    current = SectionContent.model_validate(source["current"])
    section = source["section"]
    original_evidence_ids = {e["evidence_id"] for e in source.get("evidence", [])}
    from folio.models import digest

    gap_descriptions = {digest(text): text for text in source.get("integration_gaps", [])}
    items = list(proposals) + [
        {
            "gap_id": digest(gap),
            "gap_description": gap,
            "content": gap,
            "section_ids": [section["section_id"]],
            "disposition": "body",
            "evidence_refresh": True,
        }
        for gap in supported_gaps
    ]
    if not items:
        return current
    initial = current
    protection_proposals = [*source.get("model_proposals", []), *items]
    accepted_blocks = set(source.get("protected_block_ids", []))
    check_budget = None
    engine.store.work_event(engine.job_id, f"正在将补充内容整合到《{section['title']}》。")
    selection_sources = (
        (index, integration_source(source, proposal, gap_descriptions.get(proposal["gap_id"])))
        for index, proposal in enumerate(items, 1)
        if proposal.get("disposition") != "record_only"
    )
    async with SupplementSelections(engine, selection_sources) as selections:
        for index, proposal in enumerate(items, 1):
            description = gap_descriptions.get(proposal["gap_id"])
            if proposal.get("disposition") == "record_only":
                # This is already a durable background record. It authorizes no body
                # edits, so selecting sources and asking an editor is both wasteful
                # and a source of invented metadata. Existing pending items remain.
                engine.archive_context(
                    "supplement_integrations",
                    {"gap_id": proposal["gap_id"], "mode": "record_only", "proposal": proposal},
                )
                engine.store.work_event(
                    engine.job_id,
                    f"《{section['title']}》第 {index}/{len(items)} 项已保存为待确认记录。",
                )
                continue
            protected = accepted_blocks | exact_proposal_blocks(
                initial,
                [other for other in protection_proposals if other["gap_id"] != proposal["gap_id"]],
            ).intersection(block.block_id for block in current.blocks)
            # Scope before selection: shared peer drafts and unrelated proposals never
            # enter this item's request. Full source material remains in the archive.
            item = integration_source(source, proposal, description) | {
                "current": current.model_dump(),
                "protected_block_ids": sorted(protected),
                "existing_evidence_ids": sorted(
                    {eid for b in current.blocks for eid in b.evidence_ids}
                ),
            }
            source_ref = engine.archive_context("supplement_sources", item)
            stage = 'Source selection'
            try:
                engine.store.work_event(
                    engine.job_id,
                    f"《{section['title']}》第 {index}/{len(items)} 项：正在筛选相关约束和必要原文。",
                )
                # Always select for THIS proposal, even if a large context would fit.
                item |= await selections.get(index)
                item.pop("known_gaps", None)
                item.pop("task_sections", None)
                item.pop("child_sections", None)
                schema = SupplementIntegration
                stage = 'Integration capacity check'
                capacity = await engine.measure_request("integrate_supplement", item, schema)
                provider_failure = None
                if capacity["fits"]:
                    try:
                        stage = 'Body integration'
                        engine.store.work_event(
                            engine.job_id,
                            f"正在将补充内容整合到《{section['title']}》（第 {index}/{len(items)} 项）。",
                        )
                        patch = await engine._call("integrate_supplement", item, schema)
                    except ModelCallFailed as exc:
                        if exc.code not in {"context_limit", "length_limit"}:
                            raise
                        provider_failure = exc
                    else:
                        if proposal.get("disposition") == "record_only" and (
                            patch.updates or patch.remove_block_ids
                        ):
                            raise ValueError("Record-only supplement cannot edit the document body")
                        current = apply_integration(
                            current,
                            patch,
                            original_evidence_ids,
                            model_supplement=not proposal.get("evidence_refresh"),
                            protected_block_ids=protected,
                        )
                        accepted_blocks.update(accepted_patch_blocks(current, patch, proposal))
                        engine.archive_context(
                            "supplement_integrations",
                            {
                                "source_ref": source_ref,
                                "gap_id": proposal["gap_id"],
                                "mode": "direct",
                                "capacity": capacity,
                                "selected_evidence_ids": [
                                    e["evidence_id"] for e in item.get("evidence", [])
                                ],
                                "patch": patch.model_dump(),
                                "result": current.model_dump(),
                            },
                        )
                        engine.store.work_event(
                            engine.job_id,
                            f"《{section['title']}》补充内容已整合 {index}/{len(items)} 项。",
                        )
                        continue

                # This item is a research question, not an existing model proposal.
                # Never pretend a question was conflict-checked as drafted content.
                if proposal.get("evidence_refresh"):
                    raise ValueError("Retrieved evidence integration exceeds capacity")

                engine.store.work_event(
                    engine.job_id,
                    f"《{section['title']}》第 {index} 项资料超过单次容量，"
                    "正在分批核对资料与补充内容是否冲突。",
                )
                materials = gap_material_catalog(item)
                stage = 'Batched conflict review'
                if check_budget is None:
                    check_budget = SupplementCheckBudget()
                checked = await check_supplement_conflicts(
                    engine, section, [proposal], materials, budget=check_budget
                )
                if checked.status != "checked":
                    current = retain_pending(current, proposal, checked.reason)
                    engine.archive_context(
                        "supplement_integrations",
                        {
                            "source_ref": source_ref,
                            "gap_id": proposal["gap_id"],
                            "mode": "pending",
                            "stage": stage,
                            "reason": checked.reason,
                            "check_calls": checked.calls,
                        },
                    )
                    engine.store.work_event(
                        engine.job_id, f"《{section['title']}》本项冲突核对未完成，保留原文待确认。"
                    )
                    continue
                # The original evidence is retained in SectionState for subsequent
                # extract/validate. This edit only uses checked supplements and exact
                # source quotations, never promotes a working note to new evidence.
                evidence = []
                by_ref = {m["context_ref"]: m for m in materials}
                quotes = {}
                for constraint in checked.constraints:
                    original = by_ref.get(constraint["source_ref"], {})
                    if original.get("kind") == "evidence":
                        value = original["value"]
                        quotes.setdefault(value["evidence_id"], (value, []))[1].append(
                            constraint["quote"]
                        )
                for original, parts in quotes.values():
                    evidence.append(
                        {
                            **deepcopy(original),
                            "text": "\n".join(dict.fromkeys(parts)),
                            "excerpt_only": True,
                        }
                    )
                compact = {
                    "section": section,
                    "current": current.model_dump(),
                    "integration_item": checked.proposals[0]
                    | ({"gap_description": description} if description else {}),
                    "model_proposals": checked.proposals,
                    "evidence": evidence,
                    "checked_constraints": checked.constraints,
                    "supplement_conflicts_checked": True,
                    "integration_policy": 2,
                    "protected_block_ids": sorted(protected),
                    "existing_evidence_ids": item["existing_evidence_ids"],
                    "model_completion_allowed": source.get("model_completion_allowed", False),
                    "repair_instruction": "Original sources have been checked in batches. Integrate only this checked supplement into the current body, retaining provenance on unchanged content. Do not add or rewrite business requirements beyond the proposal. protected_block_ids belong to other accepted supplements and cannot be edited or deleted; omit unchanged blocks from updates. checked_constraints are source-backed review records, not new evidence. evidence.text contains only necessary excerpts; later validation will read the full originals."
                    + FIGURE_ANCHOR_INSTRUCTION,
                }
                capacity = await engine.measure_request("integrate_supplement", compact, schema)
                if not capacity["fits"]:
                    raise ValueError("Checked supplement edits still exceed capacity")
                # Use the smaller, changed request after any runtime refusal too.
                stage = 'Body integration after verification'
                engine.store.work_event(
                    engine.job_id,
                    f"正在将补充内容整合到《{section['title']}》（第 {index}/{len(items)} 项，资料已分批核对）。",
                )
                patch = await engine._call("integrate_supplement", compact, schema)
                if proposal.get("disposition") == "record_only" and (
                    patch.updates or patch.remove_block_ids
                ):
                    raise ValueError("Record-only supplement cannot edit the document body")
                validate_checked_patch(current, patch, checked.proposals[0])
                candidate = apply_integration(
                    current, patch, original_evidence_ids, protected_block_ids=protected
                )
                if checked.proposals[0] != proposal:
                    from folio.orchestration.supplement_records import (
                        persist_corrected_supplement,
                    )

                    if not await persist_corrected_supplement(
                        engine, source, proposal, checked.proposals[0]
                    ):
                        raise ValueError(
                            "Supplement changed concurrently; preserve the latest saved version"
                        )
                engine.archive_context(
                    "supplement_integrations",
                    {
                        "source_ref": source_ref,
                        "gap_id": proposal["gap_id"],
                        "mode": "checked_batches",
                        "capacity": capacity,
                        "provider_failure": getattr(provider_failure, "code", None),
                        "checked": checked.proposals,
                        "constraints": checked.constraints,
                        "patch": patch.model_dump(),
                        "result": candidate.model_dump(),
                    },
                )
                current = candidate
                accepted_blocks.update(accepted_patch_blocks(current, patch, checked.proposals[0]))
                engine.store.work_event(
                    engine.job_id,
                    f"《{section['title']}》第 {index}/{len(items)} 项已完成分批核对并整合。",
                )
            except (ModelCallFailed, ValueError, TimeoutError, TaskRetriesExhausted) as exc:
                current = retain_pending(current, proposal, f"本项{stage}未完成，已有正文已保留。")
                engine.archive_context(
                    "supplement_integrations",
                    {
                        "source_ref": source_ref,
                        "gap_id": proposal["gap_id"],
                        "mode": "pending",
                        "stage": stage,
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "code", None),
                    },
                )
                engine.store.work_event(
                    engine.job_id, f"《{section['title']}》本项补充内容未采用，保留原文待确认。"
                )
    return current
