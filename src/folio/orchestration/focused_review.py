"""Problem-scoped review: plan relationships, read originals, combine bounded notes.

The source ledger is immutable. No child review inherits the task's source bundle.
Notes locate candidate problems; the existing drill-down still adjudicates originals.
"""

import json
import re
from typing import Literal

from pydantic import Field

from folio.models import Finding, Model, Review, digest
from folio.orchestration.context import gap_material_catalog, project
from folio.orchestration.review import parallel_reviews
from folio.orchestration.review_scope import scoped_review_fields
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted

REVIEW_POLICY = 8
MAX_ISSUE_PAGES = 8


class ReviewFocus(Model):
    question: str = Field(min_length=1, max_length=400)
    section_ids: list[str] = Field(min_length=1, max_length=4)


class ReviewFocusPlan(Model):
    checks: list[ReviewFocus] = Field(default_factory=list, max_length=8)


class ReviewNote(Model):
    source_refs: list[str] = Field(min_length=1)
    source_kind: Literal["evidence", "fact", "proposal", "risk", "body", "requirement"]
    text: str = Field(min_length=1, max_length=400)


class ReviewMaterialRead(Model):
    notes: list[ReviewNote] = Field(default_factory=list, max_length=8)


def review_materials(source):
    """Typed sources include every risk and exact body block, not an opaque shared bundle."""
    result = gap_material_catalog(
        source | {"existing_proposals": source.get("model_proposals", [])}
    )

    def add(kind, value):
        ref = "ctx_" + digest(json.dumps([kind, value], sort_keys=True))
        result.append({"context_ref": ref, "kind": kind, "value": value})

    for section in source["sections"]:
        add("body", {"section_id": section["section_id"], "content": section.get("content", {})})
    for definition in source.get("definitions", []):
        add("requirement", definition)
    for criterion in source.get("work_task", {}).get("completion_criteria", []):
        add("requirement", {"criterion": criterion})
    for gap in source.get("known_gaps", []):
        add("risk", {"unfilled_gap": gap})
    # Raw dependency snapshots have findings; projected ones have risk_briefs.
    # Both representations must preserve uncertainty during source selection.
    for dep in source.get("dependencies", []):
        for finding in dep.get("findings", []):
            add("risk", {"task_id": dep["task_id"], "finding": finding})
        for section in dep.get("sections", []):
            for finding in section.get("findings", []):
                add("risk", {"section_id": section["section_id"], "finding": finding})
    return list({item["context_ref"]: item for item in result}.values())


def material_parts(item, size=600000):
    """Lossless bounded pieces; offsets refer to a stored canonical JSON source.

    Serialized slices are explicitly not standalone JSON. Even a single huge
    paragraph or table is read fully; no characters disappear at page boundaries.
    """
    raw = json.dumps(item["value"], ensure_ascii=False, sort_keys=True)
    if len(raw) <= size:
        return [item]
    return [
        {
            "context_ref": item["context_ref"] + f"_part{start}",
            "kind": item["kind"],
            "value": {
                "parent_ref": item["context_ref"],
                "start": start,
                "end": min(start + size, len(raw)),
                "total_characters": len(raw),
                "serialized_fragment": raw[start : start + size],
            },
        }
        for start in range(0, len(raw), size)
    ]


def mentioned_ref(ref, text):
    """Match a stored identifier, never b1 inside b10 or an invented reference."""
    return bool(
        ref and re.search(r"(?<![A-Za-z0-9_])" + re.escape(ref) + r"(?![A-Za-z0-9_])", text)
    )


def issue_sections(context):
    """Locate actual accused blocks; old prose-only findings remain readable.

    Adjacent prose and the closest heading stay available for qualifiers. Their
    unrelated citations are not automatically promoted to mandatory issue sources.
    An unlocated finding retains its entire section instead of guessing a target.
    """
    finding = context["finding"]
    explicit = {}
    for ref in finding.get("block_refs", []):
        explicit.setdefault(ref["section_id"], set()).add(ref["block_id"])
    result, cited, targets = [], set(finding.get("evidence_ids", [])), []
    description = finding.get("description", "")
    for section in context["sections"]:
        sid = section["section_id"]
        content = section.get("content", {})
        blocks = content.get("blocks", [])
        supplied = {b.get("block_id") for b in blocks}
        ids = explicit.get(sid, set()) & supplied
        if not explicit:
            ids = {b["block_id"] for b in blocks if mentioned_ref(b.get("block_id"), description)}
        retained = set(range(len(blocks)))
        if ids:
            retained = set()
            for i, block in enumerate(blocks):
                if block.get("block_id") not in ids:
                    continue
                retained.update(range(max(0, i - 1), min(len(blocks), i + 2)))
                for j in range(i - 1, -1, -1):
                    if blocks[j].get("kind") == "heading":
                        retained.add(j)
                        break
                targets.append({"section_id": sid, "block_id": block["block_id"]})
        for block in blocks:
            if not ids or block.get("block_id") in ids:
                cited.update(block.get("evidence_ids", []))
        value = {
            k: v
            for k, v in section.items()
            if k in {"section_id", "revision", "content"}
            or (k == "extraction" and finding["kind"] == "extraction")
        }
        if ids:
            # A relevant figure must carry all of its original source blocks.
            visuals = [
                v for v in content.get("visuals", []) if ids.intersection(v["source_block_ids"])
            ]
            visual_ids = {bid for v in visuals for bid in v["source_block_ids"]}
            retained.update(i for i, b in enumerate(blocks) if b.get("block_id") in visual_ids)
            value["content"] = content | {
                "blocks": [b for i, b in enumerate(blocks) if i in retained]
            }
            if "visuals" in content:
                value["content"]["visuals"] = visuals
        result.append(value)
    return result, cited, targets


class FocusedReviewWorkflow:
    async def prepare_issue_context(self, context, *, select_materials=True):
        """Read original material for one finding, never every affected task's peers."""
        sections, cited, targets = issue_sections(context)
        materials = {}
        for business in context.get("business_contexts", []):
            for item in review_materials(business | {"sections": []}):
                materials[item["context_ref"]] = item
        sources = {e["evidence_id"]: e for s in context["sections"] for e in s.get("evidence", [])}
        for item in gap_material_catalog({"evidence": list(sources.values())}):
            materials[item["context_ref"]] = item
        # Legacy findings often name their disputed evidence in the description.
        # Such a source is mandatory even when the selector overlooks it.
        cited.update(
            item["value"]["evidence_id"]
            for item in materials.values()
            if item["kind"] == "evidence"
            and mentioned_ref(
                item["value"].get("evidence_id"), context["finding"].get("description", "")
            )
        )
        purpose = {
            "action": "drill_down",
            "finding": context["finding"],
            "instruction": "Read only material needed to resolve this specific issue; retain relevant counterexamples, applicability conditions and conflicts. "
            "Do not select an entire source bundle merely because it belongs to the same task. Model proposals and risks are not original evidence.",
        }
        catalog = [p for item in materials.values() for p in material_parts(item)]
        # Task requirements are review obligations, not optional evidence. The
        # relevance selector accepts only evidence/fact/proposal/risk sources.
        required = {
            item["context_ref"] for item in catalog if item["kind"] in {"body", "requirement"}
        }
        # Explicitly cited originals are mandatory regardless of relevance scores.
        # Sending their fragments through the selector only to force them back
        # afterwards wastes requests; retain every fragment directly instead.
        required.update(
            part["context_ref"]
            for item in materials.values()
            if item["kind"] == "evidence" and item["value"].get("evidence_id") in cited
            for part in material_parts(item)
        )
        # A located provenance audit must retain the known proposal itself;
        # relevance selection cannot erase the original it is asked to verify.
        required.update(
            part["context_ref"]
            for item in materials.values()
            if item["kind"] == "proposal"
            and any(
                mentioned_ref(item["value"].get(field), context["finding"].get("description", ""))
                for field in ("gap_id", "proposal_ref")
            )
            for part in material_parts(item)
        )
        selectable = [item for item in catalog if item["context_ref"] not in required]
        selected = (
            required
            | {
                ref
                for result in await self.select_reads(purpose, selectable)
                for ref in [*result.selected_refs, *result.risk_refs]
            }
            if select_materials
            else {item["context_ref"] for item in catalog}
        )
        for item in materials.values():
            parts = material_parts(item)
            if selected.intersection(p["context_ref"] for p in parts):
                selected.update(p["context_ref"] for p in parts)
                cited.update(item["value"].get("evidence_ids", []))
        for item in materials.values():
            if item["kind"] == "evidence" and item["value"].get("evidence_id") in cited:
                selected.update(p["context_ref"] for p in material_parts(item))
        return {
            "finding": context["finding"],
            "sections": sections,
            "definitions": context.get("definitions", []),
            "original_materials": [m for m in catalog if m["context_ref"] in selected],
            "issue_scope": {
                "target_blocks": targets,
                "body_representation": "target_blocks_with_context" if targets else "full_sections",
                "material_scope": "selected_originals" if select_materials else "complete_catalog",
                "optional_material_characters": len(
                    json.dumps(selectable, ensure_ascii=False, separators=(",", ":"))
                ),
                "instruction": "Resolve only the issue identified by finding. Context blocks clarify conditions; body text absent from this request cannot be deemed missing from the document.",
            },
            "policy": REVIEW_POLICY,
            **scoped_review_fields(context),
        }

    async def bounded_drill_down(self, context, model, capacity=None):
        """Compare the original target against every bounded source page.

        No summarization call stands in for a check. Pages may not decide from
        absence of evidence or a relationship whose sources are on other pages.
        Mixed, incomplete and ambiguous answers preserve the pending issue.
        """
        ids = list(dict.fromkeys(context["finding"]["section_ids"]))

        def pending(reason):
            return model(
                confirmed=False,
                reason=reason,
                affected_section_ids=ids,
                repair_instruction="",
                needs_confirmation=True,
            )

        originals = context.get("original_materials", [])
        if len(originals) < 2:
            return pending('The full body or an individual original source for this issue exceeds request capacity. Detailed review is incomplete; confirmation is required.')
        # Fragments of one source are indivisible here: its negative conditions
        # may occur after the quoted statement, even across a fragment boundary.
        groups = {}
        for item in originals:
            parent = item["value"].get("parent_ref", item["context_ref"])
            groups.setdefault(parent, []).append(item)
        pages = []

        def request(items):
            return context | {
                "original_materials": items,
                "reading_scope": {
                    "mode": "source_page",
                    "source_count": len(originals),
                    "page_source_count": len(items),
                    "all_originals_present": len(items) == len(originals),
                    "instruction": "This batch includes the complete target body but only some sources. Compare each supplied original with the allegation. "
                    "An absence of supporting evidence in this batch proves neither a false positive nor a genuine omission. "
                    "Set needs_confirmation=true when cross-batch links, missing conditions or other uncertainty prevent an independent decision. "
                    "Resolve only the current finding; do not add new allegations.",
                },
            }

        async def divide(values):
            if len(pages) >= MAX_ISSUE_PAGES:
                return False
            items = [item for group in values for item in group]
            page = request(items)
            if (await self.measure_request("drill_down", page, model))["fits"]:
                pages.append(page)
                return True
            if len(values) < 2:
                return False
            mid = len(values) // 2
            return await divide(values[:mid]) and await divide(values[mid:])

        values = list(groups.values())
        # This is a changed request after a capacity/truncation failure. Avoid
        # merely adding metadata then sending the same oversized inputs again.
        mid = len(values) // 2
        if not mid or not await divide(values[:mid]) or not await divide(values[mid:]):
            return pending('This issue cannot be safely reviewed within eight batches of complete source text. Detailed review is incomplete; confirmation is required.')
        self.archive_context("issue_read_plans", {"context": context, "pages": pages})
        self.store.work_event(self.job_id, f"问题精查：按相关原始资料分为 {len(pages)} 批核对。")

        async def read(page):
            try:
                return await self._call("drill_down", page, model)
            except (ModelCallFailed, TaskRetriesExhausted, ValueError, TimeoutError):
                return pending('Detailed review of this source batch is incomplete; confirmation is required.')

        def progress(done, total, group):
            self.store.work_event(self.job_id, f"问题精查：已完成 {done}/{total} 批原始资料核对。")

        results = await parallel_reviews(pages, 4, read, progress)
        self.archive_context(
            "issue_read_results",
            {
                "finding": context["finding"],
                "pages": pages,
                "results": [r.model_dump() for r in results],
            },
        )
        if any(r.needs_confirmation for r in results):
            return pending('Some source batches cannot independently resolve this issue. Related original text must be checked; the issue was not dismissed as a false positive.')
        if len({r.confirmed for r in results}) != 1:
            return pending('Source batches produced inconsistent conclusions. The issue and original text are preserved for confirmation.')
        if results[0].confirmed:
            # A repair needs one compatible instruction, not one arbitrary
            # page's rewrite silently overriding another page's constraints.
            signatures = {
                (
                    tuple(sorted(r.affected_section_ids)),
                    r.repair_instruction.strip(),
                    tuple(sorted(item.model_dump_json() for item in r.repair_targets)),
                )
                for r in results
            }
            if len(signatures) != 1:
                return pending('The batches confirm the issue but disagree on the repair. Original text is preserved pending confirmation of the change.')
        reason = (
            f'All {len(pages)} selected source batches were checked separately and reached consistent independent conclusions.' + results[0].reason
        )
        return results[0].model_copy(update={"reason": reason[:1000]})

    async def focused_work_review(self, source, source_ref):
        source = project("review_work", source)
        index = {
            "work_task": source.get("work_task", {}),
            "sections": [
                {k: s[k] for k in ("section_id", "title", "heading_path") if k in s}
                for s in source.get("definitions", source["sections"])
            ],
            "policy": REVIEW_POLICY,
            **scoped_review_fields(source),
        }
        ids = {s["section_id"] for s in source["sections"]}
        if not (await self.measure_request("plan_review", index, ReviewFocusPlan))["fits"]:
            return self.review_deferred(ids, 'The review-focus index exceeds request capacity. Task review is incomplete.')
        planned = await self._call("plan_review", index, ReviewFocusPlan)
        checks = [
            ReviewFocus(
                section_ids=[sid],
                question="Check whether this node fulfills its directory instructions and applicable task completion criteria. Compare the body with original sources, dependency rules and supplemental proposals. Review only this node's responsibility; responsibilities assigned to other nodes are not omissions here.",
            )
            for sid in dict.fromkeys(s["section_id"] for s in source["sections"])
            if source.get("review_scope") != "task_consistency"
        ]
        checks.extend(planned.checks)
        checks = list({c.model_dump_json(): c for c in checks}.values())
        materials = review_materials(source)
        plan_ref = self.archive_context(
            "focused_review_plans",
            {
                "source_ref": source_ref,
                "policy": REVIEW_POLICY,
                "checks": [c.model_dump() for c in checks],
                "catalog": materials,
            },
        )
        self.store.work_event(
            self.job_id, f"按业务问题审阅：共 {len(checks)} 项检查，逐项读取相关材料。"
        )

        async def run(focus):
            return await self.review_focus(source, materials, focus, plan_ref)

        def progress(done, total, group):
            self.store.work_event(self.job_id, f"业务问题审阅：已完成 {done}/{total} 项检查。")

        results = await parallel_reviews(checks, self.limits.max_parallel_reviews, run, progress)
        from folio.orchestration.review_candidates import deduplicate_review_findings

        candidates = deduplicate_review_findings([f for r in results for f in r.findings])
        if candidates.audit:
            self.archive_context("review_deduplication", candidates.audit)
        return Review(findings=candidates.findings)

    @staticmethod
    def review_deferred(ids, reason):
        return Review(
            findings=[
                Finding(
                    kind="pending",
                    key="review-incomplete:" + digest(reason + str(sorted(ids))),
                    section_ids=sorted(ids),
                    description=reason,
                )
            ]
        )

    async def review_focus(self, source, materials, focus, plan_ref):
        targets = set(focus.section_ids)
        purpose = focus.model_dump() | {
            "action": "review_work",
            "review_scope": source.get("review_scope", "full"),
            "nodes": [
                {
                    k: v
                    for k, v in node.items()
                    if k in {"section_id", "title", "heading_path", "instruction"}
                }
                for node in source.get("definitions", [])
                if node["section_id"] in targets
            ],
            "instruction": "Select only originals, shared facts, proposals and risks relevant to this issue, not every item from the same task. "
            "Retain counterexamples, negative constraints, applicability conditions and conflicts. Model proposals and unconfirmed risks are not confirmed facts.",
        }
        mandatory = [
            m
            for m in materials
            if (
                m["kind"] in {"body", "requirement"}
                and (m["value"].get("section_id") in targets or "criterion" in m["value"])
            )
        ]
        choices = [
            p
            for m in materials
            if m["kind"] in {"evidence", "fact", "proposal", "risk"}
            for p in material_parts(m)
        ]
        selected = {
            ref
            for selection in await self.select_reads(purpose, choices)
            for ref in [*selection.selected_refs, *selection.risk_refs]
        }
        # Always read original sources cited by the current body or selected facts.
        cited = {
            eid
            for s in source["sections"]
            if s["section_id"] in targets
            for b in s.get("content", {}).get("blocks", [])
            for eid in b.get("evidence_ids", [])
        }
        for item in materials:
            parts = material_parts(item)
            if selected.intersection(p["context_ref"] for p in parts):
                selected.update(p["context_ref"] for p in parts)
                cited.update(item["value"].get("evidence_ids", []))
        for item in materials:
            if item["kind"] == "evidence" and item["value"].get("evidence_id") in cited:
                selected.update(p["context_ref"] for p in material_parts(item))
            if item["kind"] == "proposal" and targets.intersection(
                item["value"].get("section_ids", [])
            ):
                selected.update(p["context_ref"] for p in material_parts(item))
        chosen = [p for m in mandatory for p in material_parts(m)] + [
            item for item in choices if item["context_ref"] in selected
        ]
        chosen = list({m["context_ref"]: m for m in chosen}.values())
        self.archive_context(
            "review_reads",
            {
                "plan_ref": plan_ref,
                "focus": focus.model_dump(),
                "materials": chosen,
            },
        )
        # The selected ORIGINALS often fit together, even when the task's whole
        # archive did not. Do not expand one bounded check into dozens of reading
        # requests merely to summarize material the reviewer can read directly.
        selected_parents = {item["value"].get("parent_ref", item["context_ref"]) for item in chosen}
        direct = {
            "review_focus": purpose,
            "sections": [s for s in source["sections"] if s["section_id"] in targets],
            "original_materials": [
                item
                for item in materials
                if item["context_ref"] in selected_parents and item["kind"] != "body"
            ],
            "material_notes": [],
            "body_representation": "original",
            "reading_complete": True,
            "policy": REVIEW_POLICY,
            **scoped_review_fields(source, targets),
        }
        if (await self.measure_request("review_focus", direct, Review))["fits"]:
            try:
                result = await self._call("review_focus", direct, Review)
                self.check_findings(result.findings, targets)
                self.archive_context(
                    "review_read_results",
                    {
                        "plan_ref": plan_ref,
                        "focus": focus.model_dump(),
                        "pages": [],
                        "mode": "direct_originals",
                        "context": direct,
                    },
                )
                return result
            except ModelCallFailed as exc:
                if exc.code not in {"context_limit", "length_limit"}:
                    raise
        notes, read_pages = [], []

        async def read(items, concise=False):
            context = {"review_focus": purpose, "catalog": items, "policy": REVIEW_POLICY}
            if concise:
                context["reading_style"] = "concise"
            measured = await self.measure_request(
                "read_review_materials", context, ReviewMaterialRead
            )
            # Required originals get a single-source answer whose only legal
            # reference is that source. This avoids repeated prose hiding a
            # missing body/requirement in an otherwise valid multi-source reply.
            page_size = 1 if items[0]["kind"] in {"body", "requirement"} else 8
            if len(items) > page_size or not measured["fits"]:
                if len(items) == 1:
                    raise ValueError(
                        "review source fragment exceeds capacity: " + items[0]["context_ref"]
                    )
                mid = len(items) // 2
                await read(items[:mid])
                await read(items[mid:])
                return
            try:
                result = await self._call("read_review_materials", context, ReviewMaterialRead)
            except ModelCallFailed as exc:
                if exc.code not in {"context_limit", "length_limit"}:
                    raise
                if len(items) == 1:
                    if not concise and exc.code == "length_limit":
                        await read(items, concise=True)
                        return
                    raise
                mid = len(items) // 2
                await read(items[:mid])
                await read(items[mid:])
                return
            notes.extend(n.model_dump() for n in result.notes)
            read_pages.append([m["context_ref"] for m in items])

        # A page contains one source type. The schema pins that type rather than
        # asking the model to reclassify body text labelled origin=evidence.
        for kind in dict.fromkeys(item["kind"] for item in chosen):
            await read([item for item in chosen if item["kind"] == kind])
        final = {
            "review_focus": purpose,
            "sections": [s for s in source["sections"] if s["section_id"] in targets],
            "material_notes": [n for n in notes if n["source_kind"] != "body"],
            "body_representation": "original",
            "reference_contract": 2,
            "reading_complete": True,
            "policy": REVIEW_POLICY,
            **scoped_review_fields(source, targets),
        }
        if not (await self.measure_request("review_focus", final, Review))["fits"]:
            original = {m["context_ref"]: m for m in materials}
            final.update(
                {
                    "sections": [{"section_id": sid} for sid in focus.section_ids],
                    "material_notes": notes,
                    "body_representation": "read_notes",
                    "body_sources": [
                        {
                            "source_refs": [m["context_ref"]],
                            "section_id": original[m["value"].get("parent_ref", m["context_ref"])][
                                "value"
                            ]["section_id"],
                        }
                        for m in chosen
                        if m["kind"] == "body"
                    ],
                }
            )
        self.archive_context(
            "review_read_results",
            {
                "plan_ref": plan_ref,
                "focus": focus.model_dump(),
                "pages": read_pages,
                "context": final,
            },
        )
        if not (await self.measure_request("review_focus", final, Review))["fits"]:
            return self.review_deferred(
                targets, 'Related source notes for this review still exceed capacity. Verification is incomplete: ' + focus.question
            )
        result = await self._call("review_focus", final, Review)
        self.check_findings(result.findings, targets)
        return result


def review_schema(model, schema, context):
    if model is ReviewFocusPlan:
        field = schema["$defs"]["ReviewFocus"]["properties"]["section_ids"]
        field["items"]["enum"] = [s["section_id"] for s in context["sections"]]
    if model is ReviewMaterialRead:
        field = schema["$defs"]["ReviewNote"]["properties"]["source_refs"]
        field["items"]["enum"] = [s["context_ref"] for s in context["catalog"]]
        field["maxItems"] = len(context["catalog"])
        kinds = list(dict.fromkeys(s["kind"] for s in context["catalog"]))
        schema["$defs"]["ReviewNote"]["properties"]["source_kind"]["enum"] = kinds
        if set(kinds).intersection({"body", "requirement"}):
            schema["properties"]["notes"]["minItems"] = 1
        if context.get("reading_style") == "concise":
            schema["properties"]["notes"]["maxItems"] = 2
    return schema


def validate_review_result(parsed, context):
    if isinstance(parsed, ReviewFocusPlan):
        ids = {s["section_id"] for s in context["sections"]}
        for check in parsed.checks:
            if (
                len(check.section_ids) != len(set(check.section_ids))
                or not set(check.section_ids) <= ids
            ):
                raise ValueError("Review focus must reference unique supplied document nodes")
    if isinstance(parsed, ReviewMaterialRead):
        kinds = {item["context_ref"]: item["kind"] for item in context["catalog"]}
        for note in parsed.notes:
            if any(kinds.get(ref) != note.source_kind for ref in note.source_refs):
                raise ValueError("Review notes must retain each original source reference and kind")
        required = {ref for ref, kind in kinds.items() if kind in {"body", "requirement"}}
        read = {ref for note in parsed.notes for ref in note.source_refs}
        if required - read:
            raise ValueError(
                "Review notes must cover each supplied body/requirement source, even if not applicable; missing="
                + str(sorted(required - read))
            )
