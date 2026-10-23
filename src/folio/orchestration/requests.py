"""A business task owns durable work; requests own bounded, purpose-specific reads."""

import json
from copy import deepcopy
from itertools import combinations

from folio.decisions.batching import DecisionBatchingError
from folio.decisions.risks import RISK_ORIGINALS_LABEL
from folio.document.guidance import guidance_context
from folio.models import Extraction, LocalReview, Review, digest
from folio.orchestration.context import (
    CONTEXT_TASKS,
    SELECTION_REFERENCE_ERROR,
    ContextSelection,
    GapMaterialRead,
    apply_selection,
    deduplicate_dependency_facts,
    dependency_catalog,
    gap_material_catalog,
    project,
    scope_work_review,
)
from folio.orchestration.focused_review import FocusedReviewWorkflow
from folio.orchestration.review import parallel_reviews
from folio.orchestration.review_candidates import deduplicate_review_findings
from folio.orchestration.work import BusinessRecord, GapCompletion
from folio.runtime.base import prompt_chars
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted


def validation_content(content, blocks, *, partial_block_ids=(), body_slice=None):
    """A body page may inspect a visual only when all its source blocks are present."""
    result = {"blocks": blocks}
    if "visuals" in content:
        complete_ids = {b["block_id"] for b in blocks} - set(partial_block_ids)
        result["visuals"] = [
            visual
            for visual in content["visuals"]
            if set(visual["source_block_ids"]) <= complete_ids
            and visual["after_block_id"] in complete_ids
        ]
    if "record_only_pending" in content:
        block_ids = {block["block_id"] for block in blocks}
        result["record_only_pending"] = [
            item
            for item in content["record_only_pending"]
            if item["block_id"] in block_ids
            and (
                not body_slice
                or item["block_id"] != body_slice["block_id"]
                or body_slice["row_start"] <= item["row_index"] < body_slice["row_end"]
            )
        ]
    return result


def validation_page(context, *, blocks=None, claims=None, partial_block_ids=()):
    """Keep complete originals for this page's assertions, prose and required sources."""
    extraction = context["extraction"]
    if claims is not None:
        extraction = extraction | {"claims": claims}
        block_ids = {bid for claim in claims for bid in claim.get("block_ids", [])}
        # Historical claims may lack block anchors. Their supporting prose cannot
        # be inferred safely, so retain the parent page's body in that case.
        unanchored = any(not claim.get("block_ids") for claim in claims)
        blocks = [
            block
            for block in context["content"]["blocks"]
            if unanchored or not block_ids or block["block_id"] in block_ids
        ]
    if blocks is None:
        blocks = context["content"]["blocks"]
    source_ids = set(context.get("required_project_evidence_ids", []))
    source_ids.update(eid for block in blocks for eid in block.get("evidence_ids", []))
    source_ids.update(
        eid for claim in extraction["claims"] for eid in claim.get("evidence_ids", [])
    )
    return context | {
        "content": validation_content(
            context["content"],
            blocks,
            partial_block_ids=partial_block_ids,
            body_slice=context.get("body_slice"),
        ),
        "extraction": extraction,
        "evidence": [
            evidence
            for evidence in context.get("evidence", [])
            if evidence["evidence_id"] in source_ids
        ],
    }


class RequestWorkflow(FocusedReviewWorkflow):
    def request_context(self, task, context):
        if task == "style_analysis":
            # Examples are isolated analysis data. Project facts and long writing
            # requirements must not inflate or redefine this bounded task.
            return context
        profile = getattr(self, "writing_profile", None)
        stage = (
            "planning" if task in {"plan", "repair_plan", "revise_plan", "work_plan"}
            else "writing" if task in {"generate", "repair", "integrate_supplement", "understand_work", "research_step"}
            else "layout" if task in {"format_document", "review_layout"}
            else None
        )
        if stage and profile:
            from folio.document.style_profile import profile_for_stage

            context = context | {"writing_profile": profile_for_stage(profile, stage)}
        if task == "review":
            from folio.orchestration.cross_review import scope_cross_review

            context = scope_cross_review(context)
        if (
            not context.get("scope_contract")
            and task
            in {
                "generate",
                "repair",
                "validate",
                "review_work",
                "review",
                "review_focus",
                "drill_down",
            }
            and getattr(self, "plan", None)
        ):
            from folio.orchestration.review_scope import attach_review_scopes

            context = attach_review_scopes(
                context,
                self.plan.sections,
                self.store.states(self.job_id),
                self.request.user_request,
            )
        # Directory planning already carries the complete confirmed request.
        # Do not duplicate potentially long requirements under a second field.
        has_bounded_request = (
            task in {"plan", "directory_research"} and "requirements" in context
        ) or (task == "research_step" and "user_request" in context)
        if not has_bounded_request:
            context = context | {"user_request": self.request.user_request}
        if getattr(self.request, "language", None):
            context = context | {"document_language": self.request.language}
        if getattr(self.request, "ui_language", None):
            context = context | {"ui_language": self.request.ui_language}
        if task in {"generate", "repair", "integrate_supplement"}:
            context = context | {"writing_contract": 2}
        return guidance_context(task, context, getattr(self.request, "writing_guide", None))

    async def measure_request(self, task, context, model, schema=None):
        context = self.request_context(task, context)
        limit = (
            self.limits.max_planning_prompt_chars
            if task == "plan"
            else self.limits.max_prompt_chars
        )
        # Dynamic ID enums and response bounds count toward the same budget as the body.
        schema = self.request_schema(task, context, model) if schema is None else schema
        if hasattr(self.runtime, "measure"):
            return await self.runtime.measure(task, context, schema, limit)
        size = prompt_chars(self.runtime, task, context, schema)
        return {"characters": size, "measurement": "character_fallback", "fits": size <= limit}

    async def request_capacity(self, task, context, model):
        """Cached requests need no online capacity check; new calls are measured."""
        # Reconstructing a cached bounded request must not depend on an online
        # tokenizer/capacity endpoint. Identity and validation match _call exactly.
        if hasattr(self, "request_cache_key"):
            context = self.request_context(task, context)
            schema = self.request_schema(task, context, model)
            cached = self.store.task(self.job_id, self.request_cache_key(task, context, schema))
            if cached and cached["result"]:
                parsed = model.model_validate_json(cached["result"])
                self.check_result(parsed, context, task=task)
                return {"measurement": "validated_cache", "fits": True}
        return await self.measure_request(task, context, model)

    def archive_context(self, kind, value):
        """Content addressed, local-only audit. No credentials or hidden reasoning."""
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        key = digest(encoded)
        path = self.store.root / "jobs" / self.job_id / "contexts" / kind / (key + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(encoded, encoding="utf-8")
        return key

    async def select_reads(self, purpose, catalog, *, capacity_retry=False):
        """Read every candidate losslessly; each actual page is a durable task."""
        pages = []

        async def divide(items):
            context = {"purpose": purpose, "catalog": items}
            if capacity_retry:
                context["selection_capacity_retry"] = True
            if (
                len(items) <= 24
                and (await self.request_capacity("select_context", context, ContextSelection))[
                    "fits"
                ]
            ):
                pages.append(context)
            elif len(items) > 1:
                mid = len(items) // 2
                await divide(items[:mid])
                await divide(items[mid:])
            else:
                raise ValueError(
                    "select_context: a single source item exceeds request capacity; source="
                    + items[0]["context_ref"]
                )

        if catalog:
            selector = getattr(self.runtime, "material_selector", None)
            if selector is None:
                await divide(catalog)
            else:
                context = {"purpose": purpose, "catalog": catalog}
                if capacity_retry:
                    context["selection_capacity_retry"] = True
                context = self.request_context("select_context", context)
                schema = self.request_schema("select_context", context, ContextSelection)
                try:
                    pages = [page for page, _, _ in await selector.prepare(context, schema)]
                except DecisionBatchingError as exc:
                    raise ModelCallFailed(
                        "Material selection cannot safely split shared instructions",
                        "selection_context_limit",
                    ) from exc

        async def select_page(context):
            try:
                return await self._call("select_context", context, ContextSelection)
            except TaskRetriesExhausted as exc:
                if exc.last_error != "ValueError: " + SELECTION_REFERENCE_ERROR or context.get(
                    "selection_reference_retry"
                ):
                    raise
                # Only this exhausted page gets a new, corrected request identity.
                # Successful pages keep their cache; previous errors/cost stay intact.
                return await self._call(
                    "select_context",
                    context
                    | {
                        "selection_reference_retry": "Return only the outer context_ref from this page's catalog, never an evidence_id, fact_id or gap_id nested inside value. selected_refs may select only evidence/fact/proposal items; risk_refs may select only risk items."
                    },
                    ContextSelection,
                )

        results = await parallel_reviews(
            pages,
            4,
            select_page,
            lambda *_: None,
        )
        references = [item["context_ref"] for page in pages for item in page["catalog"]]
        if len(set(references)) != len(references):
            # A long source may be relevant in several fragments. Load it once,
            # from the original catalog, never append each fragment as a fact.
            selected = list(dict.fromkeys(ref for r in results for ref in r.selected_refs))
            risks = list(dict.fromkeys(ref for r in results for ref in r.risk_refs))
            return [
                ContextSelection(
                    selected_refs=selected,
                    risk_refs=risks,
                    risk_summary=RISK_ORIGINALS_LABEL if risks else "",
                )
            ]
        return results

    async def prepare_context(self, task, source):
        context = project(task, source)
        if task == "drill_down":
            return await self.prepare_issue_context(context)
        purpose = (
            self.gap_purpose(context)
            if task == "complete_gaps"
            else self.writing_purpose(context)
            if task in {"generate", "repair"}
            else None
        )

        # Dependency fingerprints use the complete backend snapshot. Only the model view changes.
        async def dependencies(value, purpose):
            if isinstance(value, dict):
                if task not in {"complete_gaps", "generate", "repair"}:
                    purpose = value.get("work_task", purpose)
                if value.get("dependencies"):
                    catalog, objects, views = dependency_catalog(value["dependencies"])
                    for selection in await self.select_reads(purpose, catalog):
                        apply_selection(views, objects, selection)
                    value["dependencies"] = views
                    deduplicate_dependency_facts(value)
                for key, child in value.items():
                    if key != "dependencies":
                        await dependencies(child, purpose)
            elif isinstance(value, list):
                for child in value:
                    await dependencies(child, purpose)

        await dependencies(context, purpose or context.get("section", {"action": task}))
        if task == "complete_gaps":
            context = await self.select_gap_materials(context)
        if task in {"generate", "repair"}:
            context = await self.select_writing_materials(context)
        return context

    def writing_purpose(self, context):
        purpose = {
            "action": "write_current_section",
            "section": context.get("section", {}),
            "repair_instruction": context.get("repair_instruction", ""),
            "instruction": "Select only originals, facts and constraints directly needed to write the current section. "
            "Membership in the same task, adjacent sections or a similar product is not sufficient relevance. "
            "Do not apply another product's exclusive rules here; general rules must actually apply to this section. "
            "Retain relevant negative constraints and conflicts. Select nothing when no material is relevant.",
        }
        if context.get("integration_item"):
            purpose.update(
                action="integrate_one_supplement",
                supplement=context["integration_item"],
                instruction="Determine only whether the material directly supports or constrains this supplement. "
                "section identifies its location; do not reselect material for the entire section or task. "
                "Retain facts, counterexamples, negations and conflicts concerning this item's applicability. "
                "Pure graphics encodings, webpage styles and fragments with no business meaning are irrelevant.",
            )
        elif context.get("child_sections"):
            purpose["child_sections"] = context["child_sections"]
            purpose["writing_scope"] = context.get("writing_scope", {})
            purpose["instruction"] += (
                "This section has explicitly assigned child sections. The parent needs only the module overview, module relationships, shared rules and parent-specific requirements. Do not select every child operation, field and parameter merely because the parent instruction asks for detail. Keep the complete original when it also contains rules needed by the parent; retain shared constraints and counterexamples."
            )
        return purpose

    async def select_writing_materials(self, context, *, capacity_retry=False):
        """Scope shared indexes and originals to one node without changing backend records."""
        required = set(context.get("required_project_evidence_ids", []))
        required.update(
            eid
            for block in context.get("current", {}).get("blocks", [])
            for eid in block.get("evidence_ids", [])
        )
        groups = {
            "evidence": ("evidence", context.get("evidence", [])),
            "facts": ("fact", context.get("business_record", {}).get("facts", [])),
            "global_facts": ("fact", context.get("global_facts", [])),
        }
        record = context.get("business_record", {})
        for field in ("conflicts", "open_questions"):
            groups[field] = ("risk", record.get(field, []))
        # Questions and summaries need the same relevance decision as raw sources.
        # Previously they bypassed selection and every chapter copied the whole
        # group's missing information into its own supplement queue.
        groups["known_gaps"] = ("risk", context.get("known_gaps", []))
        groups["summary"] = ("fact", [record["summary"]] if record.get("summary") else [])
        groups["terminology"] = ("fact", record.get("terminology", []))
        catalog, items = [], {}
        for field, (kind, values) in groups.items():
            for value in values:
                if field == "evidence" and value["evidence_id"] in required:
                    # Existing prose must retain its complete original sources.
                    # Scoring them and forcing them back wastes a selection call
                    # and risks losing citations in the compressed wire schema.
                    continue
                ref = "ctx_" + digest(json.dumps([field, value], sort_keys=True))
                items[ref] = (field, value)
                catalog.append({"context_ref": ref, "kind": kind, "value": value})
        purpose = self.writing_purpose(context)
        if capacity_retry:
            purpose["instruction"] += (
                "The previous conservative selection still exceeds request capacity. Check precisely whether each source and fact directly serves the current section. Exclude content relevant only to the overall document or other sections; retain applicable constraints and negative rules. Do not discard genuinely necessary material merely to fit capacity: select all indispensable material even if it remains too large."
            )
        selected = {
            ref
            for selection in await self.select_reads(
                purpose, catalog, capacity_retry=capacity_retry
            )
            for ref in selection.selected_refs + selection.risk_refs
        }
        kept = {field: [] for field in groups}
        for ref, (field, value) in items.items():
            if ref in selected:
                kept[field].append(value)
        required.update(e["evidence_id"] for e in kept["evidence"])
        required.update(
            eid
            for fact in kept["facts"] + kept["global_facts"]
            for eid in fact.get("evidence_ids", [])
        )
        kept["evidence"] = [e for e in context.get("evidence", []) if e["evidence_id"] in required]
        facts = kept.pop("facts")
        record = {
            "facts": facts,
            **{field: kept.pop(field) for field in ("conflicts", "open_questions")},
        }
        summary, terminology = "".join(kept.pop("summary")), kept.pop("terminology")
        if summary:
            record["summary"] = summary
        if terminology:
            record["terminology"] = terminology
        return (
            context
            | kept
            | {
                "business_record": record,
                "evidence_selected": True,
            }
        )

    def gap_purpose(self, context):
        return {
            "action": "complete_gaps",
            "gaps": context["gaps"],
            "work_task": {
                k: v
                for k, v in context.get("work_task", {}).items()
                if k in {"task_id", "title", "objective"}
            },
            "sections": [
                {k: v for k, v in s.items() if k in {"section_id", "title", "heading_path"}}
                for s in context.get("sections", [])
            ],
            "instruction": "Select only material and applicable constraints needed for this batch of gaps. The task and directory locate the scope; "
            "do not select all material from the same task. Retain relevant negative constraints, conflicts and existing proposals.",
        }

    async def select_gap_materials(self, context):
        """Select full originals for this gap batch; the source archive remains complete."""
        groups = {
            "evidence": ("evidence", context.get("evidence", [])),
            "facts": ("fact", context.get("business_record", {}).get("facts", [])),
            "global_facts": ("fact", context.get("global_facts", [])),
            "existing_proposals": ("proposal", context.get("existing_proposals", [])),
        }
        catalog, items = [], {}
        for field, (kind, values) in groups.items():
            for value in values:
                ref = "ctx_" + digest(json.dumps([field, value], sort_keys=True))
                items[ref] = (field, value)
                catalog.append({"context_ref": ref, "kind": kind, "value": value})
        selected = {
            ref
            for result in await self.select_reads(self.gap_purpose(context), catalog)
            for ref in result.selected_refs
        }
        kept = {field: [] for field in groups}
        for ref, (field, value) in items.items():
            if ref in selected:
                kept[field].append(value)
        # Keep original sources behind selected local facts, not merely their synthesis.
        required = {eid for f in kept["facts"] for eid in f.get("evidence_ids", [])}
        chosen = {e["evidence_id"] for e in kept["evidence"]} | required
        kept["evidence"] = [e for e in context.get("evidence", []) if e["evidence_id"] in chosen]
        facts = kept.pop("facts")
        return (
            context
            | kept
            | {"business_record": context.get("business_record", {}) | {"facts": facts}}
        )

    async def select_evidence(self, context):
        catalog = [
            {"context_ref": e["evidence_id"], "kind": "evidence", "value": e}
            for e in context["evidence"]
        ]
        purpose = (
            self.writing_purpose(context) if "section" in context else self.gap_purpose(context)
        )
        selected = {
            ref
            for selection in await self.select_reads(purpose, catalog)
            for ref in selection.selected_refs
        }
        selected.update(context.get("required_project_evidence_ids", []))
        return context | {
            "evidence": [e for e in context["evidence"] if e["evidence_id"] in selected],
            "evidence_selected": True,
        }

    async def call(self, task, context, model):
        if task not in CONTEXT_TASKS:
            return await self._call(task, context, model)
        source_ref = self.archive_context("sources", {"task": task, "context": context})
        if task == "review_work":
            projected = project(task, context)
            size = await self.request_capacity(task, projected, model)
            if not size["fits"]:
                return await self.focused_work_review(projected, source_ref)
        # Partition before reading: splitting only the output gaps retained the entire
        # task's evidence/facts in every child, so even a single gap could overflow.
        projected = project(task, context)
        # Capacity and relevance are separate: writers must select materials for
        # this node even when the shared business-task bundle already fits.
        # Other small action views can read their complete originals directly.
        if task == "drill_down":
            # The measured model budget decides whether these originals fit.
            # There is no second, smaller character ceiling for this task.
            complete = await self.prepare_issue_context(projected, select_materials=False)
            working = (
                complete
                if (await self.request_capacity(task, complete, model))["fits"]
                else await self.prepare_context(task, context)
            )
        elif task in {"generate", "repair"}:
            working = await self.prepare_context(task, context)
        elif (await self.request_capacity(task, projected, model))["fits"]:
            working = projected
        else:
            working = await self.prepare_context(task, context)
        if (
            task == "generate"
            and working.get("model_completion_allowed") is False
            and working.get("evidence_selected")
            and not working.get("evidence")
        ):
            # A task may have sources while this node has none. Do not ask a
            # writer to invent uncited "facts" before its research allowance is
            # exhausted. The ordinary bounded research path handles this gap.
            section = working["section"]
            pending = (
                f"{section.get('title', 'Current section')}: No relevant supporting sources have been found; "
                f"research this section's requirements: {section.get('instruction', '')}"
            )
            self.archive_context("deferred_writing", {"source_ref": source_ref, "section": section})
            self.store.work_event(
                self.job_id,
                f"《{section.get('title', 'Current section')}》暂无相关依据，先补查资料再写正文。",
            )
            return model(blocks=[], pending_items=[pending])
        before = await self.request_capacity(task, working, model)
        if task in {"generate", "repair"} and not before["fits"]:
            from folio.model_settings import feature_enabled

            if feature_enabled(getattr(self.runtime, "config", {}), "decision"):
                # Refine once with the SAME selector and a more specific purpose.
                # Its bounded pages must never switch to the main LLM.
                self.store.work_event(
                    self.job_id, "当前小节的候选资料仍较多，正在分批核对直接相关资料。"
                )
                working = await self.select_writing_materials(working, capacity_retry=True)
                before = await self.request_capacity(task, working, model)
        self.archive_context(
            "views",
            {
                "source_ref": source_ref,
                "task": task,
                "context": working,
                "capacity": before,
                "policy": 2,
            },
        )
        return await self.call_bounded(
            task, working, model, gap_source=context if task == "complete_gaps" else None
        )

    async def split_gaps(self, context, model):
        gaps = context["gaps"]
        mid = len(gaps) // 2
        # Independent gap owners read the same immutable research snapshot.
        # Merge only after both complete; task review checks their combined design.
        results = await parallel_reviews(
            [gaps[:mid], gaps[mid:]],
            2,
            lambda batch: self.call("complete_gaps", context | {"gaps": batch}, model),
            lambda *_: None,
        )
        proposals = [p for result in results for p in result.proposals]
        result = GapCompletion(proposals=proposals)
        self.check_result(result, context)
        return result

    async def read_gap_materials(self, context):
        """Read every selected source in bounded pages before synthesizing one gap.

        These notes guide an explicitly model-origin proposal, never become evidence
        or verified global facts. Writers and validators still read original sources.
        """
        purpose = self.gap_purpose(context)

        async def read(items):
            request = {"purpose": purpose, "catalog": items}
            capacity = await self.request_capacity("read_gap_materials", request, GapMaterialRead)
            failure = None
            if len(items) <= 8 and capacity["fits"]:
                try:
                    return (
                        await self._call("read_gap_materials", request, GapMaterialRead)
                    ).constraints
                except ModelCallFailed as exc:
                    if exc.code not in {"context_limit", "length_limit"}:
                        raise
                    failure = exc
            if len(items) > 1:
                mid = len(items) // 2
                results = await parallel_reviews(
                    [items[:mid], items[mid:]], 2, read, lambda *_: None
                )
                return results[0] + results[1]
            if failure:
                raise failure
            raise ValueError(
                "read_gap_materials: a single source exceeds capacity; source="
                + items[0]["context_ref"]
            )

        catalog = gap_material_catalog(context)
        constraints = await read(catalog) if catalog else []
        notes = [c.model_dump() for c in constraints]
        self.archive_context(
            "gap_reads", {"gaps": context["gaps"], "catalog": catalog, "constraints": notes}
        )
        return {
            k: v
            for k, v in context.items()
            if k
            not in {
                "evidence",
                "business_record",
                "global_facts",
                "existing_proposals",
                "dependencies",
            }
        } | {"material_constraints": notes, "gap_materials_read": True}

    async def call_bounded(self, task, context, model, *, gap_source=None):
        if task == "review_work":
            context = scope_work_review(context)
        capacity = await self.request_capacity(task, context, model)
        provider_failure = None
        # Use the actual configured capacity, not legacy item-count estimates
        # made for small output budgets. Real capacity/output failures can split below.
        if capacity["fits"]:
            try:
                return await self._call(task, context, model)
            except ModelCallFailed as exc:
                if exc.code not in {"context_limit", "length_limit"} or task not in {
                    "understand_work",
                    "complete_gaps",
                    "extract",
                    "validate",
                    "review_work",
                    "review",
                    "drill_down",
                }:
                    raise
                provider_failure = exc
                # Incomplete results are discarded. Change request boundaries, not the task.

        if task == "review" and (
            not capacity["fits"]
            or (provider_failure is not None and provider_failure.code == "context_limit")
        ):
            from folio.orchestration.cross_review import defer_comparison_originals

            index_context = defer_comparison_originals(context)
            if (
                index_context is not None
                and (await self.request_capacity(task, index_context, model))["fits"]
            ):
                # The full source request was archived by call(). No source text,
                # index claim or proposal is truncated; originals are read in drill-down.
                self.store.work_event(
                    self.job_id,
                    "相关正文较长，本组先核对完整事实索引；疑点将读取原始正文精查。",
                )
                return await self.call_bounded(task, index_context, model)

        if task == "drill_down":
            self.store.work_event(
                self.job_id, "当前问题的原始资料较多，正在分批精查；无法确定的问题会保留待确认。"
            )
            return await self.bounded_drill_down(context, model, capacity)
        if task in {"review", "review_work"}:
            text = "当前审阅组需要进一步拆分，正在分组核对相关内容；已完成结果会保留。"
        else:
            text = f"正在分批完成{task}，保留业务任务和已有检索进度。"
        self.store.work_event(self.job_id, text)
        if task == "review_work":
            source_ref = self.archive_context("sources", {"task": task, "context": context})
            return await self.focused_work_review(context, source_ref)
        if task == "understand_work" and len(context["evidence"]) > 1:
            values = context["evidence"]
            # Pages have disjoint source inputs and no writes to the shared record.
            # Stable ordering and the model semaphore bound concurrent reads.
            results = await parallel_reviews(
                [values[: len(values) // 2], values[len(values) // 2 :]],
                2,
                lambda batch: self.call_bounded(
                    task,
                    context | {"evidence": batch, "previous_record": {}, "read_batch": True},
                    model,
                ),
                lambda *_: None,
            )
            facts = {}
            for result in results:
                for fact in result.facts:
                    key = digest(
                        json.dumps([fact.statement, sorted(fact.evidence_ids)], ensure_ascii=False)
                    )
                    facts[key] = fact.model_copy(update={"fact_id": "fact_" + key})
            return BusinessRecord(
                summary="\n".join(r.summary for r in results),
                facts=list(facts.values()),
                research_actions=list({
                    action.model_dump_json(): action
                    for result in results for action in result.research_actions
                }.values()),
                **{
                    k: list(dict.fromkeys(v for r in results for v in getattr(r, k)))
                    for k in ("terminology", "conflicts", "open_questions")
                },
            )
        if task == "complete_gaps" and len(context["gaps"]) > 1:
            # Re-read from the complete snapshot, not from a previously selected view.
            # Re-projecting dependency views would lose the original risk references.
            return await self.split_gaps(gap_source or context, model)
        if (
            task == "complete_gaps"
            and not capacity["fits"]
            and not context.get("gap_materials_read")
        ):
            self.store.work_event(
                self.job_id, "单项缺口的相关资料较多，正在分批读取业务约束，再补写内容。"
            )
            return await self.call_bounded(task, await self.read_gap_materials(context), model)
        if task == "extract" and len(context["content"]["blocks"]) > 1:
            values = context["content"]["blocks"]
            mid = len(values) // 2
            results = [
                await self.call_bounded(
                    task, context | {"content": context["content"] | {"blocks": batch}}, model
                )
                for batch in (values[:mid], values[mid:])
            ]
            claims = []
            for index, result in enumerate(results):
                claims += [
                    c.model_copy(update={"claim_id": f"{index}_{c.claim_id}"})
                    for c in result.claims
                ]
            return Extraction(
                summary="\n".join(r.summary for r in results),
                claims=claims,
                entities=list(dict.fromkeys(v for r in results for v in r.entities)),
            )
        if task == "validate" and not context.get("validation_scope"):
            return await self.validate_parts(context, model)
        if (
            task == "validate"
            and context.get("validation_scope") == "body"
            and len(context["content"]["blocks"]) > 1
        ):
            blocks = context["content"]["blocks"]
            results = []
            for part in (blocks[: len(blocks) // 2], blocks[len(blocks) // 2 :]):
                results.append(
                    await self.call_bounded(
                        task,
                        validation_page(context, blocks=part),
                        model,
                    )
                )
            return LocalReview(
                verdicts=[],
                findings=deduplicate_review_findings(
                    [f for r in results for f in r.findings]
                ).findings,
            )
        if task == "validate" and len(context["extraction"]["claims"]) > 1:
            claims = context["extraction"]["claims"]
            mid = len(claims) // 2
            # Each claim page keeps its original source blocks and complete cited
            # evidence. Separate body pages check omissions/unextracted assertions.
            results = [
                await self.call_bounded(task, validation_page(context, claims=batch), model)
                for batch in (claims[:mid], claims[mid:])
            ]
            return LocalReview(
                verdicts=[v for r in results for v in r.verdicts],
                findings=deduplicate_review_findings(
                    [f for r in results for f in r.findings]
                ).findings,
            )
        if task == "validate" and context.get("validation_scope") == "body":
            blocks = context["content"]["blocks"]
            if len(blocks) == 1 and blocks[0].get("kind") == "table" and len(blocks[0]["rows"]) > 1:
                rows = blocks[0]["rows"]
                mid = len(rows) // 2
                source_slice = context.get("body_slice", {})
                offset = source_slice.get("row_start", 0)
                results = []
                for start, end in ((0, mid), (mid, len(rows))):
                    body_slice = {
                        "block_id": blocks[0]["block_id"],
                        "row_start": offset + start,
                        "row_end": offset + end,
                        "total_rows": source_slice.get("total_rows", len(rows)),
                    }
                    if context["content"].get("record_only_pending"):
                        body_slice["headers"] = source_slice.get("headers", rows[0])
                    child = validation_page(
                        context | {"body_slice": body_slice},
                        blocks=[blocks[0] | {"rows": rows[start:end]}],
                        partial_block_ids=[blocks[0]["block_id"]],
                    )
                    results.append(await self.call_bounded(task, child, model))
                return LocalReview(
                    verdicts=[],
                    findings=deduplicate_review_findings(
                        [f for r in results for f in r.findings]
                    ).findings,
                )
        if (
            task in {"validate", "review"}
            and capacity["fits"]
            and provider_failure is not None
            and provider_failure.code == "length_limit"
            and not context.get("compact_review_retry")
            and (task != "review" or len(context["sections"]) <= 2)
        ):
            # A tiny indivisible input can still trigger verbose output. Try once
            # with an explicit concise-output request; never accept partial JSON.
            return await self.call_bounded(task, context | {"compact_review_retry": True}, model)
        if task == "review" and len(context["sections"]) > 2:
            # Pair coverage avoids losing relationships across batch boundaries.
            results = await parallel_reviews(
                list(combinations(context["sections"], 2)),
                self.limits.max_parallel_reviews,
                lambda pair: self.call_bounded(task, context | {"sections": list(pair)}, model),
                lambda *_: None,
            )
            return Review(
                findings=deduplicate_review_findings(
                    [f for r in results for f in r.findings]
                ).findings
            )
        if task == "review":
            # When even a node pair is too large, compare smaller original parts.
            # Both branches retain the other node, preserving cross-node pair coverage.
            candidates = []
            for index, section in enumerate(context["sections"]):
                for path in (("content", "blocks"), ("claims",), ("model_proposals",)):
                    value = section
                    for name in path:
                        value = value.get(name, {}) if isinstance(value, dict) else {}
                    if isinstance(value, list) and len(value) > 1:
                        candidates.append(
                            (len(json.dumps(value, ensure_ascii=False)), index, path, value)
                        )
            if candidates:
                _, index, path, values = max(candidates, key=lambda item: item[0])
                results = []
                for part in (values[: len(values) // 2], values[len(values) // 2 :]):
                    child = deepcopy(context)
                    target = child["sections"][index]
                    for name in path[:-1]:
                        target = target[name]
                    target[path[-1]] = part
                    child["review_slice"] = True
                    results.append(await self.call_bounded(task, child, model))
                return Review(
                    findings=deduplicate_review_findings(
                        [f for r in results for f in r.findings]
                    ).findings
                )
        if (
            task in {"generate", "repair", "complete_gaps"}
            and len(context.get("evidence", [])) > 1
            and not context.get("evidence_selected")
        ):
            child = await self.select_evidence(context)
            return await self.call_bounded(task, child, model)
        if provider_failure:
            raise provider_failure
        raise ValueError(
            f"{task}: smallest safe request exceeds capacity; characters={capacity['characters']}, input_tokens={capacity.get('input_tokens')}, output_tokens={capacity.get('output_tokens')}, window={capacity.get('context_window')}. Full source/checkpoints retained."
        )

    async def validate_parts(self, context, model):
        """Separate verdicts, unextracted assertions and project coverage; never repeat all three per claim page."""
        results = []
        base = {
            k: v
            for k, v in context.items()
            if k
            not in {
                "business_context",
                "content",
                "evidence",
                "extraction",
                "required_project_evidence_ids",
            }
        }
        blocks = context["content"]["blocks"]
        evidence = context.get("evidence", [])
        claims = context["extraction"]["claims"]
        if "visuals" in context["content"]:
            # Only partitioned validation needs this extra small request. A normal
            # whole-section validation already sees the full figure list, including [].
            # Semantic source checks for figures spanning pages happen before rendering
            # with build_visuals' validate_visual, reading their saved source blocks.
            results.append(
                await self.call_bounded(
                    "validate",
                    base
                    | {
                        "validation_scope": "visual_plan",
                        "validation_instruction": (
                            "Check only whether the complete visuals list covers this node's explicit figure requirements. "
                            "Body and evidence are intentionally omitted: blocks=[] does not mean the document lacks body text. "
                            "Other steps check body completeness, provenance and cross-block consistency; do not assess them here. "
                            "Allow visuals=[] when no figures are explicitly required. Do not require illustrations for every node."
                        ),
                        "content": {"blocks": [], "visuals": context["content"]["visuals"]},
                        "evidence": [],
                        "extraction": {"claims": []},
                        "required_project_evidence_ids": [],
                    },
                    model,
                )
            )
        for start in range(0, len(claims), 24):
            page = claims[start : start + 24]
            results.append(
                await self.call_bounded(
                    "validate",
                    validation_page(
                        base
                        | {
                            "validation_scope": "claims",
                            "content": {
                                "blocks": blocks,
                                **(
                                    {
                                        "record_only_pending": context["content"][
                                            "record_only_pending"
                                        ]
                                    }
                                    if "record_only_pending" in context["content"]
                                    else {}
                                ),
                            },
                            "evidence": evidence,
                            "extraction": {"claims": page},
                            "required_project_evidence_ids": [],
                        },
                        claims=page,
                    ),
                    model,
                )
            )
        for start in range(0, len(blocks), 8):
            page = blocks[start : start + 8]
            results.append(
                await self.call_bounded(
                    "validate",
                    validation_page(
                        base
                        | {
                            "validation_scope": "body",
                            "content": context["content"],
                            "evidence": evidence,
                            "extraction": {"claims": []},
                            "required_project_evidence_ids": [],
                        },
                        blocks=page,
                    ),
                    model,
                )
            )
        for eid in context.get("required_project_evidence_ids", []):
            results.append(
                await self.call_bounded(
                    "validate",
                    base
                    | {
                        "validation_scope": "coverage",
                        "content": context["content"],
                        "evidence": [e for e in evidence if e["evidence_id"] == eid],
                        "extraction": {"claims": []},
                        "required_project_evidence_ids": [eid],
                    },
                    model,
                )
            )
        result = LocalReview(
            verdicts=[v for r in results for v in r.verdicts],
            findings=deduplicate_review_findings([f for r in results for f in r.findings]).findings,
        )
        self.check_result(result, context)
        return result
