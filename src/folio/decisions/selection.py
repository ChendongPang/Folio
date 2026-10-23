"""One bounded relevance selector, with exclusive provider routing per request."""

import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from folio.decisions.batching import (
    DECISION_CONTEXT_CHARS,
    DecisionBatchingError,
    bisect_catalog_item,
    context_chars,
    decision_pages,
)
from folio.decisions.risks import LEGACY_RISK_ORIGINALS_LABEL, RISK_ORIGINALS_LABEL
from folio.decisions.routing import (
    SAFE_DECISION_ERROR_CODES,
    DecisionRouter,
    question_page_limit,
)
from folio.model_settings import feature_enabled
from folio.models import digest
from folio.runtime.base import RuntimeResult, parse_json
from folio.runtime.errors import ModelCallFailed

MAX_CAPACITY_SPLIT_DEPTH = 3
MAX_CAPACITY_ATTEMPTS_PER_PAGE = 15


def page_schema(schema, context):
    result = deepcopy(schema)
    for field, kinds in (
        ("selected_refs", {"evidence", "fact", "proposal"}),
        ("risk_refs", {"risk"}),
    ):
        refs = list(
            dict.fromkeys(i["context_ref"] for i in context["catalog"] if i["kind"] in kinds)
        )
        prop = result["properties"][field]
        prop["maxItems"] = len(refs)
        prop.setdefault("items", {}).pop("enum", None)
        if refs:
            prop["items"]["enum"] = refs
    if not any(i["kind"] == "risk" for i in context["catalog"]):
        result["properties"]["risk_summary"]["const"] = ""
    return result


def selection_result(value, page):
    # The normal engine validates the final set too. Validate each individual
    # page here so an illegal cross-page reference cannot disappear in merging.
    from folio.orchestration.context import (
        SELECTION_REFERENCE_ERROR,
        ContextSelection,
        normalize_selection_references,
    )

    selected = ContextSelection.model_validate(value)
    normalize_selection_references(selected, page["catalog"])
    available = {i["context_ref"]: i["kind"] for i in page["catalog"]}
    for refs, kinds in (
        (selected.selected_refs, {"evidence", "fact", "proposal"}),
        (selected.risk_refs, {"risk"}),
    ):
        if any(available.get(ref) not in kinds for ref in refs):
            raise ValueError(SELECTION_REFERENCE_ERROR)
    if bool(selected.risk_refs) != bool(selected.risk_summary.strip()):
        raise ValueError("Risk summary must describe selected risk references")
    return selected


class MaterialSelector:
    def __init__(self, runtime):
        self.runtime = runtime
        # Intent/action fallback failures must not trip the strict selector's
        # circuit before it has ever contacted its configured decision service.
        self.router = DecisionRouter(runtime.config)
        self.cache = {}

    @property
    def use_decision(self):
        return feature_enabled(self.runtime.config, "decision")

    async def prepare(self, context, schema):
        if not context.get("catalog"):
            return []
        enabled = self.use_decision
        key = digest(json.dumps([context, schema, enabled], ensure_ascii=False, sort_keys=True))
        if key in self.cache:
            return self.cache[key]

        def input_characters(page):
            if enabled:
                questions, _ = self.router.questions(
                    "select_context", page, config=self.runtime.config
                )
                return context_chars({"state": page, "questions": questions})
            return self.runtime.prompt_chars("select_context", page, page_schema(schema, page))

        # A primary LLM first sees the complete candidate set when its token
        # budget permits it. The typed decision service lacks a tokenizer and
        # retains the larger character fallback plus its question-count limit.
        budget = DECISION_CONTEXT_CHARS if enabled else max(context_chars(context), input_characters(context))

        # Include instructions/schema (LLM) or typed decision questions in the
        # packing decision itself. Token windows may still require smaller
        # pages, but one source's character overhead no longer shrinks every
        # unrelated page's character allowance.
        for _ in range(20):
            pages = decision_pages(
                "select_context",
                context,
                max_chars=budget,
                max_items=question_page_limit(self.runtime.config) if enabled else len(context["catalog"]),
                measure_chars=input_characters,
            )
            prepared, excess, token_overflow = [], 0, False
            for page in pages:
                contract = page_schema(schema, page)
                if enabled:
                    size = input_characters(page)
                    measured = {
                        "characters": size,
                        "input_tokens": None,
                        "measurement": "decision_characters",
                        "fits": size <= DECISION_CONTEXT_CHARS,
                    }
                else:
                    measured = await self.runtime.capacity.measure(
                        "select_context", page, contract, DECISION_CONTEXT_CHARS
                    )
                    size = measured["characters"]
                    token_overflow |= not measured["fits"]
                if enabled:
                    excess = max(excess, size - DECISION_CONTEXT_CHARS)
                prepared.append((page, contract, measured))
            if not excess and not token_overflow:
                if len(self.cache) >= 32:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = prepared
                return prepared
            budget = min(
                budget - max(256, excess + 128), int(budget * 0.75) if token_overflow else budget
            )
            if budget <= 0:
                break
        raise ModelCallFailed(
            "Material selection cannot fit a safe request", "selection_context_limit"
        )

    async def measure(self, context, schema):
        try:
            pages = await self.prepare(context, schema)
        except DecisionBatchingError:
            return {
                "characters": context_chars(context),
                "measurement": "selection_pages",
                "fits": False,
            }
        if not pages:
            return {
                "characters": 0,
                "measurement": "selection_pages",
                "fits": True,
                "selection_batches": 0,
            }
        largest = max((measured for _, _, measured in pages), key=lambda m: m["characters"])
        return largest | {
            "fits": True,
            "selection_batches": len(pages),
            **({"character_limit": DECISION_CONTEXT_CHARS} if self.use_decision else {}),
        }

    async def split_rejected_page(self, page, schema, parent_characters):
        """Change the failed input boundary; never retry an identical prompt."""
        items = page["catalog"]
        if len(items) > 1:
            middle = len(items) // 2
            children = [page | {"catalog": part} for part in (items[:middle], items[middle:])]
        else:
            children = [page | {"catalog": [part]} for part in bisect_catalog_item(items[0])]
        prepared = []
        for child in children:
            prepared.extend(await self.prepare(child, schema))
        if len(prepared) < 2 or any(
            measured["characters"] >= parent_characters for _, _, measured in prepared
        ):
            raise DecisionBatchingError("select_context", "capacity_split_did_not_reduce_input")
        return prepared

    async def complete(self, context, schema, session_key):
        try:
            pages = await self.prepare(context, schema)
        except DecisionBatchingError as exc:
            raise ModelCallFailed(
                "Material selection cannot safely split shared instructions",
                "selection_context_limit",
            ) from exc
        results, usages, completed_pages, audit = [], [], [], []
        use_decision = self.use_decision

        async def read(page, contract, measured, path, depth, attempts, previous_failure=None):
            if attempts[0] >= MAX_CAPACITY_ATTEMPTS_PER_PAGE:
                raise previous_failure or ModelCallFailed(
                    "Material selection capacity recovery exhausted", "selection_context_limit"
                )
            attempts[0] += 1
            entry = {
                "page": path,
                "depth": depth,
                "characters": measured["characters"],
                "source_refs": list(dict.fromkeys(item["context_ref"] for item in page["catalog"])),
                "fragment_refs": list(
                    dict.fromkeys(
                        item["context_ref"] for item in page["catalog"] if item.get("fragment")
                    )
                ),
                "status": "started",
            }
            audit.append(entry)
            try:
                if use_decision:
                    result, reason = await self.router.try_complete(
                        "select_context", page, contract
                    )
                    if result is None:
                        failure = ModelCallFailed(
                            "Material decision failed: " + (reason or "invalid_result"),
                            reason
                            if reason in SAFE_DECISION_ERROR_CODES
                            else "decision_unavailable",
                        )
                        failure.diagnostics = getattr(reason, "diagnostics", {})
                        raise failure
                else:
                    result = await self.runtime._complete_llm(
                        "select_context", page, contract, f"{session_key}:page:{path}"
                    )
            except ModelCallFailed as exc:
                entry["status"] = exc.code
                if use_decision:
                    entry["decision_http_attempts"] = getattr(exc, "diagnostics", {}).get(
                        "decision_http_attempts", []
                    )
                if (
                    use_decision
                    or exc.code not in {"context_limit", "length_limit"}
                    or depth >= MAX_CAPACITY_SPLIT_DEPTH
                ):
                    raise
                try:
                    children = await self.split_rejected_page(page, schema, measured["characters"])
                except (DecisionBatchingError, ModelCallFailed) as split_error:
                    # The original service failure remains the useful diagnosis.
                    # Nothing from a partial response becomes a successful page.
                    raise exc from split_error
                for index, child in enumerate(children):
                    await read(*child, f"{path}.{index}", depth + 1, attempts, exc)
                return
            results.append(selection_result(parse_json(result.content), page))
            usages.append(result.usage or {})
            completed_pages.append(page)
            entry["status"] = "completed"
            if use_decision:
                entry["relevance"] = (result.usage or {}).get("decision_relevance", [])
                entry["decision_http_attempts"] = (result.usage or {}).get(
                    "decision_http_attempts", []
                )

        # An engine model slot belongs to this logical call. Keep its pages
        # serial; independent catalog groups already run concurrently upstream.
        try:
            for index, prepared in enumerate(pages):
                await read(*prepared, str(index), 0, [0])
        except ModelCallFailed as exc:
            exc.diagnostics = getattr(exc, "diagnostics", {}) | {
                "selection_page_audit": audit,
                "decision_http_attempts": [
                    attempt
                    for entry in audit
                    for attempt in entry.get("decision_http_attempts", [])
                ],
            }
            raise
        selected = {ref for r in results for ref in r.selected_refs}
        risks = {ref for r in results for ref in r.risk_refs}
        # OR across accepted fragments retains the complete original source;
        # Accept only probabilities strictly above the relevance threshold.
        # No fragments ever replace stored evidence.
        catalog = context.get("catalog", [])
        selected_refs = list(
            dict.fromkeys(i["context_ref"] for i in catalog if i["context_ref"] in selected)
        )
        risk_refs = list(
            dict.fromkeys(i["context_ref"] for i in catalog if i["context_ref"] in risks)
        )
        summaries = list(dict.fromkeys(r.risk_summary for r in results if r.risk_summary))
        summary = "\n".join(summaries)
        fragmented_risks = {
            item["context_ref"]
            for page in completed_pages
            for item in page["catalog"]
            if item.get("fragment") and item["kind"] == "risk"
        }
        if len(summary) > 1200 or fragmented_risks & risks or {RISK_ORIGINALS_LABEL, LEGACY_RISK_ORIGINALS_LABEL}.intersection(summaries):
            summary = RISK_ORIGINALS_LABEL
        value = {"selected_refs": selected_refs, "risk_refs": risk_refs, "risk_summary": summary}
        if not Draft202012Validator(schema).is_valid(value):
            raise ValueError("Invalid merged material selection")
        usage = {
            "selection_provider": "decision" if use_decision else "llm",
            "selection_batches": len(completed_pages),
            "selection_attempts": len(audit),
            "selection_page_audit": audit,
            "selection_max_input_characters": max(
                (entry["characters"] for entry in audit), default=0
            ),
            **({"selection_character_limit": DECISION_CONTEXT_CHARS} if use_decision else {}),
        }
        if use_decision:
            usage.update(
                decision_provider=self.runtime.config.get("provider_mode", "custom"),
                decision_model=next(
                    (u["decision_model"] for u in usages if u.get("decision_model")),
                    self.runtime.config.get("decision_model", ""),
                ),
                decision_calls=sum(u.get("decision_calls", 0) for u in usages),
                decision_http_attempts=[
                    attempt
                    for entry in audit
                    for attempt in entry.get("decision_http_attempts", [])
                ],
                decision_usage=[item for u in usages for item in u.get("decision_usage", [])],
                decision_relevance=[
                    item | {"page": entry["page"]}
                    for entry in audit
                    for item in entry.get("relevance", [])
                ],
            )
        else:
            usage["selection_page_usage"] = usages
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if any(name in u for u in usages):
                    usage[name] = sum(u.get(name, 0) or 0 for u in usages)
        return RuntimeResult(json.dumps(value, ensure_ascii=False), usage=usage)
