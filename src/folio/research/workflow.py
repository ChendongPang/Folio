"""Autonomous research within the existing business owner and durable engine."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import contextmanager
from copy import copy, deepcopy
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from filelock import FileLock

from folio.models import Evidence, Finding, digest
from folio.orchestration.work import BusinessRecord
from folio.research.artifacts import ArtifactManager, derived_evidence_error
from folio.research.business_presentation import evidence_catalog_item
from folio.research.contracts import (
    CoverageExclusionReview,
    DerivedEvidenceReview,
    ResearchBatch,
)
from folio.research.formats import SUPPORTED_SUFFIXES
from folio.research.ledger import ResearchLedger, ResearchLimitReached
from folio.research.memory import ResearchMemory
from folio.research.sandbox import SandboxManager
from folio.research.settings import load_research_policy
from folio.research.source_index import source_version
from folio.research.workspace import WorkspaceManager


async def local(function, *args, **kwargs):
    # Preserve ownership until a cancelled reader/writer has actually stopped.
    from folio.orchestration.engine import _run_local_export

    return await _run_local_export(function, *args, **kwargs)


def source_workspace(engine):
    root = engine.store.root / "jobs" / engine.job_id / "research"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "sources"
    with FileLock(str(root / "sources.lock")):
        expected = {}
        if engine.request.knowledge_path:
            source = Path(engine.request.knowledge_path).resolve()
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Research sources cannot contain symbolic links")
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                    expected[path.relative_to(source).as_posix()] = path
        for index, path in enumerate(engine.request.reference_paths):
            path = Path(path)
            expected[f"references/{index}/{path.name}"] = path
        destination.mkdir(exist_ok=True)
        for name, path in expected.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or source_version(target) != source_version(path):
                    raise ValueError("Persisted research source snapshot has changed")
            else:
                temporary = target.with_name(target.name + ".copying")
                shutil.copyfile(path, temporary)
                temporary.replace(target)
        actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
        if actual != set(expected):
            raise ValueError("Research source snapshot contains unexpected files")
    return destination


def observation_view(observations, limit, memory=None):
    from folio.research.context_views import observation_page

    if memory is not None:
        # The request-capacity check owns compaction. A read being summarized
        # makes it eligible for removal under pressure, not immediately stale.
        return list(observations)
    result, size = [], 0
    for item in reversed(observations):
        text = item.get("model_text", json.dumps(item, ensure_ascii=False))
        if size + len(text) > limit:
            if not result:
                result.append(observation_page(item | {"model_text": text}, limit))
            break
        result.append(item)
        size += len(text)
    return list(reversed(result))


def update_step_metadata(session, step):
    """Omission is not a request to erase durable research state."""
    if step.notes.strip():
        session["notes"] = step.notes
    if step.provided_metadata("missing_information"):
        session["missing"] = list(step.missing_information)


def result_progress_key(tool, result):
    """Changed selectors/call IDs alone do not make an unchanged read progress."""
    if result.get("evidence"):
        body = {
            "evidence": [(item["evidence_id"], item.get("source_version", ""), item["text"])
                         for item in result["evidence"]],
            # History/evidence pages can deliver a new part of the same long
            # evidence item; compare the actual visible page, not its limit.
            "page_text": result.get("text", ""),
        }
    elif isinstance(result.get("text"), str):
        body = result["text"]
    elif isinstance(result.get("files"), list):
        body = result["files"]
    else:
        body = {key: value for key, value in result.items() if key not in {
            "model_text", "model_evidence_spans", "next_offset", "offset", "request_id",
            "original_request_id", "execution_id", "duration_ms", "created_at",
        }}
    return digest(json.dumps([tool, body], sort_keys=True))


class ResearchTools:
    def __init__(self, engine, task, work, workspace, sandbox, ledger, session):
        self.engine, self.task, self.work = engine, task, work
        self.workspace, self.sandbox, self.ledger = workspace, sandbox, ledger
        self.session = session
        self.context_sources = {}
        self.config = engine.request.research
        self.artifacts = ArtifactManager(
            workspace.task_root, workspace, execution_lookup=sandbox.verify_execution
        )
        from folio.research.artifact_coverage import CoverageManager

        self.coverage = CoverageManager(workspace.task_root, workspace)
        self.evidence = {e.evidence_id: e for e in work.evidence}
        for raw in session["evidence"]:
            value = Evidence.model_validate(raw)
            if value.evidence_id.startswith(("extracted:", "visual:")):
                value = workspace.resolve_evidence(value)
            if value.metadata.get("provenance") != "derived" or not derived_evidence_error(value):
                self.evidence[value.evidence_id] = value
        from folio.research.business_presentation import BusinessPresentation

        self.presentation = BusinessPresentation(workspace, self.evidence.values())
        self.memory = ResearchMemory(session.get("research_memory"))
        for observation in session.setdefault("observations", []):
            if "arguments" not in observation:
                record = ledger.get(observation["request_id"])
                observation["arguments"] = json.loads(record["arguments"]) if record else {}
            self.memory.record_observation(observation)
        for delivery in session.get("model_deliveries", []):
            for observation in delivery.get("observations", []):
                self.memory.record_delivery(
                    observation["request_id"], observation.get("delivered_evidence_ids", []),
                    complete=observation.get("complete", True),
                )

    def save_memory(self):
        self.session["research_memory"] = self.memory.dump()
        self.ledger.save_session(self.task.task_id, self.session["round_id"], self.session)
        self.workspace._atomic_text(
            self.workspace.task_root / "research.md",
            self.memory.markdown(evidence_label=self.presentation.evidence_id),
        )
        self.presentation.save()

    def remember_observation(self, observation):
        self.memory.record_observation(observation)
        self.session["observations"].append(observation)
        # Keep the complete native call/result sequence in the durable session.
        # Only the per-request view may be compacted after checking capacity.

    def research_context(self):
        batches = self.memory.dump()["batches"]
        return {
            "research_notes": self.memory.markdown(evidence_label=self.presentation.evidence_id),
            "research_history": self.memory.history_text(evidence_label=self.presentation.evidence_id),
            "memory_pending_requests": self.memory.pending_requests(),
            "memory_delivered_requests": [key for key in self.memory.pending_requests() if batches[key]["delivered"]],
        }

    def prepare_context_sources(self, context):
        """Navigation reads the full durable source, including on pending replay."""
        self.context_sources = deepcopy(context)
        if hasattr(self, "memory"):
            self.context_sources["research_notes"] = self.memory.markdown(evidence_label=self.presentation.evidence_id)
            self.context_sources["research_history"] = self.memory.history_text(evidence_label=self.presentation.evidence_id)

    @contextmanager
    def model_budget(self):
        from folio.runtime.call_budget import (
            CallBudgetPolicy,
            ResearchModelDeferred,
            call_budget_scope,
        )

        policy = CallBudgetPolicy(
            bucket="business_research:" + self.session.get("quota_task_id", self.task.task_id),
            max_calls=self.config.max_model_calls,
            reserve_calls=min(self.config.reserve_model_calls, max(1, self.engine.limits.max_calls // 4)),
            purpose="research",
        )
        try:
            # Every provider attempt, including retries and subrequests, is
            # admitted atomically with the job counter. Cache hits are free.
            with call_budget_scope(policy):
                yield
        except ResearchModelDeferred as exc:
            raise ResearchLimitReached('The research-stage model allowance is exhausted. Remaining calls are reserved for body writing and review') from exc
        finally:
            self.session["model_calls"] = self.engine.store.call_budget_usage(self.engine.job_id, policy.bucket)
            self.ledger.save_session(self.task.task_id, self.session["round_id"], self.session)

    async def review_fits(self, action, context, schema):
        """Check the complete model request, using characters only as fallback."""
        capacity = getattr(self.engine, "request_capacity", None)
        if capacity:
            return (await capacity(action, context, schema))["fits"]
        return len(json.dumps(context, ensure_ascii=False)) <= self.config.max_artifact_review_chars

    async def model(self, action, context, schema):
        if action == "research_step":
            from folio.research.context_views import fit_context

            self.prepare_context_sources(context)
            context = await fit_context(self.engine, context, schema)
            # Pagination must use the window actually shown to this model call.
            # Already archived history remains durable, but is not silently
            # reinserted when budgeting this call's newly requested reads.
            self.session["pending_model_context"] = deepcopy(context)
            self.session["pending_context_observation_count"] = len(self.session.get("observations", []))
        with self.model_budget():
            result = await self.engine.call(action, context, schema)
        if action == "research_step":
            delivery = {
                "step": self.session.get("step", context.get("step", 0)),
                "observations": [{
                    "request_id": item["request_id"],
                    "text_chars": len(item.get("model_text", "")),
                    "text_digest": digest(item.get("model_text", "")),
                    "complete": item.get("delivery_complete", True),
                    "delivered_text_chars": item.get("delivered_text_chars"),
                    "total_text_chars": item.get("total_text_chars"),
                } | (self.presentation.delivered_evidence(
                    item, self.evidence, self.session.setdefault("evidence_delivery_ranges", {}))
                     if hasattr(self, "presentation") else {})
                    for item in context.get("observations", [])],
            }
            self.session.setdefault("model_deliveries", []).append(delivery)
            if hasattr(self, "memory"):
                for item in delivery["observations"]:
                    self.memory.record_delivery(
                        item["request_id"], item.get("delivered_evidence_ids", []), complete=item["complete"],
                    )
                self.session["pending_batch"] = ResearchBatch.model_validate(result).model_dump(exclude_unset=True)
                self.session.pop("page_images", None)
                await local(self.save_memory)
            else:
                self.ledger.save_session(self.task.task_id, self.session["round_id"], self.session)
        return result

    async def dispatch(self, step, request_id, timeout, *, resolved_arguments=None):
        if step.tool == "research.save":
            return {"status": "completed", "message": 'Valid research notes were saved. Original text and the reading index remain available.'}
        if step.tool == "fs.search" and step.arguments.get("scope") == "web":
            raise ValueError('Local research cannot search the web early. If gaps remain after three rounds, the coordinator automatically schedules up to two Tavily search rounds')
        if not hasattr(self, "presentation"):
            from folio.research.business_presentation import BusinessPresentation

            self.presentation = BusinessPresentation(self.workspace, getattr(self, "evidence", {}).values())
        args = (deepcopy(resolved_arguments) if resolved_arguments is not None
                else await local(self.presentation.resolve_arguments, step.arguments, tool=step.tool))
        if "evidence_ids" in args:
            args["evidence_ids"] = [self.presentation.resolve_evidence_id(key) for key in args["evidence_ids"]]
        if step.tool == "fs.list":
            if args.pop("scope", "sources") == "artifacts":
                return await local(self.artifact_catalog)
            return await local(self.workspace.list, **args)
        if step.tool == "fs.inspect":
            navigation_limit = max(self.config.max_result_chars, self.config.max_observation_chars)
            args["max_chars"] = min(int(args.get("max_chars", navigation_limit)), navigation_limit)
            return await local(self.workspace.inspect_navigation, **args)
        if step.tool == "fs.view":
            self.require_vision()
            from folio.research.source_visuals import SourceVisuals

            return await local(SourceVisuals(self.workspace).render, **args, sandbox=self.sandbox)
        if step.tool == "fs.read":
            scope = args.pop("scope", "sources")
            if scope == "history":
                args.setdefault("limit", self.config.max_result_chars if args.get("request_id") else 20)
                if args.get("request_id"):
                    return await local(self.history_page, **args)
                return await local(self.ledger.history, self.session["quota_task_id"],
                                   inherited_ids=self.session.get("inherited_history_ids", []),
                                   **args, max_chars=self.config.max_result_chars)
            if scope == "context":
                from folio.research.context_views import context_field_text, read_text_page

                field = args.pop("field")
                if field not in self.context_sources:
                    raise ValueError("Unknown context field")
                args.setdefault("limit", self.config.max_result_chars)
                return read_text_page(context_field_text(self.context_sources, field),
                                      **args, max_chars=self.config.max_result_chars)
            if scope == "evidence":
                if args.get("evidence_id"):
                    args.setdefault("limit", self.config.max_result_chars)
                return self.evidence_page(**args)
            if scope != "sources":
                raise ValueError("Unsupported read scope")
            if "artifact_id" in args:
                item = await local(self.artifacts.load, args["artifact_id"])
                return {"artifact": item.model_dump(), "verified": not derived_evidence_error(item)}
            if "execution_id" in args:
                return await local(self.read_output, args["execution_id"], args["output_path"],
                                   args.get("offset", 0), args.get("limit", self.config.max_result_chars))
            # Read the selected section in full first. The whole next request,
            # including sibling tool results and existing history, owns paging.
            return await local(self.workspace.read, **args)
        if step.tool == "fs.search":
            scope = args.pop("scope", "sources")
            if scope == "knowledge":
                return await self.knowledge(args["query"])
            if scope != "sources":
                raise ValueError("Unsupported search scope")
            return await local(self.workspace.search, **args)
        if step.tool == "sandbox.exec":
            operation = args.pop("operation", "python")
            async with asyncio.timeout(timeout):
                if operation == "dependency":
                    return await self.sandbox.request_dependency(**args)
                if operation == "convert":
                    result = await self.sandbox.convert(
                        args["conversion"], args["path"], execution_id=request_id,
                        profile=args.get("profile"),
                        **({"pages": args["pages"]} if "pages" in args else {}),
                    )
                    if result.get("status") == "completed" and args["conversion"] in {"ocr", "office_ocr"}:
                        result["extraction"] = await local(self.register_extraction, args["path"], result)
                    return result
                if operation != "python":
                    raise ValueError("Unsupported sandbox operation")
                return await self.sandbox.execute(
                    args["code"], execution_id=request_id,
                    profile=args.get("profile", "base"), timeout_seconds=timeout,
                )
        if step.tool == "artifact.save":
            return await self.save_artifact(args)
        raise ValueError("Unsupported research tool")

    def present_result(self, step, result):
        if isinstance(result.get("model_text"), str):
            return result["model_text"]
        try:
            args = self.presentation.resolve_arguments(step.arguments, tool=step.tool)
        except ValueError:
            args = step.arguments
        text = self.presentation.result_text(step.tool, args, result)
        self.presentation.save()
        return text

    def history_page(self, request_id, query="", offset=0, limit=600000):
        from folio.research.context_views import page_evidence_spans, read_text_page

        # Use the existing task ownership check before fetching the raw record.
        self.ledger.history(self.session["quota_task_id"], request_id=request_id, query=query,
                            offset=0, limit=1, max_chars=self.config.max_result_chars,
                            inherited_ids=self.session.get("inherited_history_ids", []))
        record = self.ledger.get(request_id)
        result = record["result"] or {"status": 'Incomplete'}
        text = result.get("model_text")
        if text is None:
            text = self.presentation.result_text(record["tool"], json.loads(record["arguments"]), result)
            self.presentation.save()
        page = read_text_page(text, offset=offset, limit=limit, max_chars=self.config.max_result_chars)
        spans = page_evidence_spans(result.get("model_evidence_spans", []), offset, len(page["text"]))
        keys = {span["evidence_id"] for span in spans}
        return page | {
            "request_id": request_id, "original_request_id": request_id,
            "model_evidence_spans": spans,
            "evidence": [self.evidence[key].model_dump() for key in keys if key in self.evidence],
        }

    def evidence_page(self, evidence_id=None, query="", offset=0, limit=20):
        from folio.research.context_views import page_evidence_spans, read_text_page

        if evidence_id:
            if evidence_id not in self.evidence:
                raise ValueError("Unknown evidence identity")
            item = self.evidence[evidence_id]
            text = self.presentation.evidence_text(item)
            page = read_text_page(text, offset=offset, limit=limit, max_chars=self.config.max_result_chars)
            spans = [{"evidence_id": evidence_id, "start": len(text) - len(item.text), "end": len(text)}]
            return page | {"model_evidence_spans": page_evidence_spans(spans, offset, len(page["text"])),
                           "evidence": [item.model_dump()]}
        if (not isinstance(query, str) or len(query) > 3500 or isinstance(offset, bool)
                or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool)
                or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise ValueError("Invalid evidence catalog range")
        found = [e for e in self.evidence.values() if query.casefold() in
                 (e.text + e.source_id + e.locator).casefold()]
        items, end, size = [], offset, 0
        for item in found[offset:offset + limit]:
            value = evidence_catalog_item(item)
            length = len(json.dumps(value, ensure_ascii=False))
            if items and size + length > self.config.max_result_chars - 300:
                break
            items.append(value)
            end += 1
            size += length
        return {"items": items, "total": len(found), "next_offset": end if end < len(found) else None}

    def require_vision(self):
        if not getattr(self.engine.runtime, "config", {}).get("supports_image_input", False):
            raise ValueError('The current research model has not enabled image input. Use an image-capable model; the original-image range remains unverified.')

    def read_output(self, execution_id, output_path, offset=0, limit=600000):
        record = self.sandbox.verify_execution(execution_id)
        if not any(a["path"] == output_path for a in record["artifacts"]):
            raise ValueError("Output is not part of this execution")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Invalid output offset")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self.config.max_result_chars:
            raise ValueError("Invalid output read limit")
        path = self.workspace.task_root / output_path
        if path.stat().st_size > 2_000_000:
            raise ValueError('The artifact exceeds text-reading capacity. Use a script to produce a smaller verifiable result')
        text = path.read_text(encoding="utf-8")
        return {"path": output_path, "text": text[offset:offset + limit],
                "total_chars": len(text), "next_offset": offset + limit if offset + limit < len(text) else None,
                "provenance": "unverified_execution_output"}

    def register_extraction(self, path, result):
        record = self.sandbox.verify_execution(result["execution_id"])
        output = next((a for a in record["artifacts"] if a["path"].endswith("/extraction.json")), None)
        if output is None:
            raise ValueError("OCR execution did not produce extraction.json")
        file = self.workspace.task_root / output["path"]
        if file.stat().st_size > 8_000_000:
            raise ValueError("OCR extraction exceeds capacity")
        return self.workspace.register_extraction(
            path, json.loads(file.read_text(encoding="utf-8")),
            execution_id=result["execution_id"], image_digest=result["image_digest"],
        )

    async def knowledge(self, query):
        from folio.orchestration.retrieval_audit import search_with_audit

        if not isinstance(query, str) or not query.strip() or len(query) > 3500:
            raise ValueError("Invalid knowledge query")
        if len(self.work.searches) >= self.engine.limits.max_searches:
            raise ResearchLimitReached('The source retrieval allowance is exhausted')
        # Admit before I/O: interruption cannot reset the existing search allowance.
        self.work.searches.append([query])
        self.engine.store.save_work_state(self.engine.job_id, self.work)
        found = await search_with_audit(
            self.engine.store, self.engine.job_id, self.engine.provider, [query],
            task_id=self.task.task_id, source_kind="knowledge_base",
            limit=self.engine.limits.retrieval_limit, max_chars=self.engine.limits.evidence_chars,
        )
        return {"evidence": [e.model_dump() for e in found]}

    def artifact_catalog(self):
        result = []
        root = self.engine.store.root / "jobs" / self.engine.job_id / "research" / "tasks"
        for path in sorted(root.glob("*/*/.research/artifact_records/*.json")):
            # Artifacts can be discovered, but reuse independently checks their signature,
            # current inputs, output hashes, environment and prior approval.
            from folio.research.artifacts import _read_record

            record = _read_record(path)
            if record["validation"]["status"] == "verified":
                result.append({"artifact_id": record["artifact_id"],
                               "description": record["registration"]["description"][:1000],
                               "profile": record["registration"]["environment"]["profile"]})
        return {"artifacts": result[:100], "truncated": len(result) > 100}

    async def save_artifact(self, args):
        from folio.research.artifact_issues import (
            record_artifact_issue,
            resolve_verified_artifact,
        )

        operation = args.pop("operation", "register")
        if operation == "visual.extract":
            self.require_vision()
            from folio.research.extraction_review import ExtractionReviewManager
            from folio.research.source_visuals import SourceVisuals
            from folio.research.visual_extraction import VisualExtractionReview

            rendered = await local(SourceVisuals(self.workspace).render, args["path"],
                                   page=args["page"], execution_id=args.get("execution_id"),
                                   sandbox=self.sandbox)
            manager = ExtractionReviewManager(self.workspace.task_root, self.workspace)
            prepared = await local(manager.prepare_visual, args["path"], args["page"], args["text"],
                                   page_count=rendered["page_count"])
            context = prepared | {"proposed_text": args["text"], "page_images": rendered["page_images"]}
            if not await self.review_fits("verify_visual_extraction", context, VisualExtractionReview):
                raise ValueError('Original-page interpretation exceeds independent-verification capacity. Submit a shorter accurate description; partial interpretation does not establish full-page coverage')
            review = await self.model("verify_visual_extraction", context, VisualExtractionReview)
            cached_context = self.engine.request_context("verify_visual_extraction", context)
            key = self.engine.request_cache_key(
                "verify_visual_extraction", cached_context,
                self.engine.request_schema("verify_visual_extraction", cached_context, VisualExtractionReview),
            )
            return await local(manager.record_visual, prepared, review_key=key,
                               supported=review.supported, complete=review.complete, reason=review.reason,
                               image_paths=[str(Path(image["path"]).relative_to(self.workspace.task_root))
                                            for image in rendered["page_images"]])
        if operation == "extraction.verify":
            self.require_vision()
            from folio.research.extraction_review import (
                ExtractionReview,
                ExtractionReviewManager,
            )
            from folio.research.source_visuals import SourceVisuals

            pages = args["pages"]
            if not isinstance(pages, list) or not 1 <= len(pages) <= 2:
                raise ValueError('Independent original-image verification supports 1 to 2 pages per call. Verify additional pages in batches')
            manager = ExtractionReviewManager(self.workspace.task_root, self.workspace)
            prepared = await local(manager.prepare, args["path"], pages)
            images = []
            for page in sorted(pages):
                rendered = await local(SourceVisuals(self.workspace).render, args["path"], page=page,
                                       execution_id=args.get("execution_id"), sandbox=self.sandbox)
                images.extend(rendered["page_images"])
            context = prepared | {"page_images": images}
            if not await self.review_fits("verify_extraction", context, ExtractionReview):
                raise ValueError('The original images and complete extracted text in this batch exceed verification capacity. Reduce the page count')
            review = await self.model("verify_extraction", context, ExtractionReview)
            if {item.page for item in review.pages} != set(pages):
                raise ValueError('Independent verification did not cover each original page supplied in this request')
            page_blocks = {item["page"]: item["blocks"] for item in prepared["pages"]}
            if any(item.page in review.approved_pages and not page_blocks[item.page]
                   and not item.blank_or_decorative for item in review.pages):
                raise ValueError('The original page contains business content but extraction is empty. Full coverage cannot be approved')
            cached_context = self.engine.request_context("verify_extraction", context)
            key = self.engine.request_cache_key(
                "verify_extraction", cached_context,
                self.engine.request_schema("verify_extraction", cached_context, ExtractionReview),
            )
            result = await local(manager.record, prepared, approved_pages=review.approved_pages,
                                 review_key=key, reason=review.reason,
                                 image_paths=[str(Path(image["path"]).relative_to(self.workspace.task_root))
                                              for image in images])
            # Earlier reads may be character slices with different IDs from the
            # full reviewed block. Refresh those exact quotes as well.
            for evidence_id, evidence in list(self.evidence.items()):
                if evidence.source_id == args["path"] and evidence.evidence_id.startswith("extracted:"):
                    self.evidence[evidence_id] = await local(self.workspace.resolve_evidence, evidence)
            return result
        if operation == "coverage.create":
            record = await local(self.coverage.create, args["path"], args.get("locator", ""), node_id=args.get("node_id"))
            cid = record["coverage_id"]
            self.session.setdefault("coverage_ids", [])
            if cid not in self.session["coverage_ids"]:
                self.session["coverage_ids"].append(cid)
            if cid not in self.session.setdefault("manual_coverage_ids", []):
                self.session["manual_coverage_ids"].append(cid)
            return record
        if operation == "coverage.resolve":
            results = []
            for item in args["resolutions"]:
                if any(r["section_id"] not in self.task.section_ids for r in item.get("references", [])):
                    raise ValueError("Coverage resolution is outside the business assignment")
                results.append(await local(self.coverage.record, args["coverage_id"], **item))
                if item["status"] == "not_applicable":
                    record = await local(self.coverage._load, args["coverage_id"])
                    source_item = next(i for i in record["scope"]["items"] if i["item_id"] == item["item_id"])
                    context = {
                        "sections": [s.model_dump() for s in self.engine.task_sections(self.task)],
                        "source_id": record["scope"]["source_id"],
                        "source_version": record["scope"]["source_version"],
                        "scope": record["scope"].get("node_id") or record["scope"]["locator_prefix"],
                        "item": source_item, "exclusion_reason": item["reason"],
                    }
                    review = await self.model("review_coverage_exclusion", context, CoverageExclusionReview)
                    if review.approved:
                        prepared = self.engine.request_context("review_coverage_exclusion", context)
                        key = self.engine.request_cache_key(
                            "review_coverage_exclusion", prepared,
                            self.engine.request_schema("review_coverage_exclusion", prepared, CoverageExclusionReview),
                        )
                        await local(self.coverage.review_exclusion, args["coverage_id"], item["item_id"],
                                    review_key=key, reason=review.reason)
                    results[-1]["independent_review"] = review.model_dump()
            return {"resolutions": results}
        if operation == "reuse":
            aid = args["artifact_id"]
            if not isinstance(aid, str) or len(aid) != 64 or any(c not in "0123456789abcdef" for c in aid):
                raise ValueError("Invalid artifact identity")
            root = self.engine.store.root / "jobs" / self.engine.job_id / "research" / "tasks"
            from folio.research.artifacts import _read_record

            for path in root.glob(f"*/*/.research/artifact_records/{aid}.json"):
                record = await local(_read_record, path)
                environment = await self.sandbox.environment(record["registration"]["environment"]["profile"])
                profiles = {item["environment"]["profile"]
                            for item in record["registration"].get("intermediate_executions", [])}
                intermediate_environments = {
                    profile: await self.sandbox.environment(profile) for profile in profiles
                }
                item = await local(
                    self.artifacts.reuse, str(path),
                    source_versions={k: v["version"] for k, v in self.workspace._manifest.items()},
                    environment=environment,
                    intermediate_environments=intermediate_environments,
                )
                if item:
                    resolve_verified_artifact(self.session, item)
                    return {"evidence": [item.model_dump()], "reused": True}
            raise ValueError('No verified artifact matches the current sources and execution environment')
        if operation != "register":
            raise ValueError("Unsupported artifact operation")
        sources = [self.evidence[eid] for eid in args["evidence_ids"]]
        item = await local(
            self.artifacts.register, args["execution_id"], args["output_path"], sources,
            args["methodology"], args.get("description") or args["claim"],
        )
        aid = item.metadata["derivation"]["artifact_id"]
        record_artifact_issue(self.session, aid, item.text, 'The calculation result has not completed independent verification', args["evidence_ids"])
        self.ledger.save_session(self.task.task_id, self.session["round_id"], self.session)
        review_context = await local(self.artifacts.verification_context, aid)
        if not await self.review_fits("verify_derivation", review_context, DerivedEvidenceReview):
            record_artifact_issue(self.session, aid, item.text, 'Inputs, script, and results exceed complete-verification capacity', args["evidence_ids"])
            return {"artifact_id": aid, "status": "needs_confirmation",
                    "message": 'Inputs, script, and results exceed complete-verification capacity. Split the calculation while retaining original ranges.'}
        review = await self.model("verify_derivation", review_context, DerivedEvidenceReview)
        cache_context = self.engine.request_context("verify_derivation", review_context)
        key = self.engine.request_cache_key(
            "verify_derivation", cache_context,
            self.engine.request_schema("verify_derivation", cache_context, DerivedEvidenceReview),
        )
        item = await local(self.artifacts.record_validation, aid, review.supported, review.reason, key)
        if review.supported:
            resolve_verified_artifact(self.session, item)
        else:
            record_artifact_issue(self.session, aid, item.text, review.reason, args["evidence_ids"])
        return {"artifact_id": aid, "status": "verified" if review.supported else "needs_confirmation",
                "reason": review.reason, "evidence": [item.model_dump()] if review.supported else []}


def evidence_fingerprint(evidence):
    return digest(json.dumps([item.model_dump() for item in evidence], ensure_ascii=False, sort_keys=True))


async def assess_research(engine, task, work, tools, assignment):
    """Re-read newly collected evidence before deciding whether another round helps."""
    work.evidence = list(tools.evidence.values())
    previous_missing = list(tools.session["missing"])
    work.record = await engine.call("understand_work", {
        "work_task": task.model_dump(), "sections": assignment["sections"],
        "evidence": [e.model_dump() for e in work.evidence],
        "previous_record": work.record.model_dump(), "dependencies": assignment["dependencies"],
        "global_facts": list({fact["record_id"]: fact
                             for facts in assignment["global_facts"].values()
                             for fact in facts}.values()),
        "research_notes": tools.memory.markdown(evidence_label=tools.presentation.evidence_id)
        + ('\n\nHistory overview: ' + tools.session["notes"] if tools.session["notes"] else ""),
        "repair_instruction": work.research_instruction,
        "writing_gaps": list(dict.fromkeys(assignment["writing_gaps"] + previous_missing)),
        "source_catalog": await local(tools.workspace.list),
        "read_progress": list(tools.session.get("read_progress", {}).values()),
        "directory_research": assignment.get("directory_research", {}),
        "research_phase": tools.session.get("phase", "local"),
        "local_round": tools.session.get("local_round", 0),
        "research_assessment": "Recheck each existing gap. Remove those answered by evidence and retain unanswered questions in open_questions. "
        "A remaining question is not itself an instruction to repeat research. Supply research_actions only for concrete "
        "local reads/verification that can still help or verifiable public facts suitable for public_web. "
        "Use read_progress and research_notes to avoid repeating completed work. Keep user-held facts and decisions pending "
        "without retrieval actions when no available original can answer them. Return [] when research is complete. "
        "Check directory_research.web_research for earlier public findings, source URLs and searches before requesting "
        "another public_web action; identify the unanswered point or necessary freshness/source verification. "
        "Public web sources can support public information, not replace project-specific facts or establish complete coverage of originals.",
    }, BusinessRecord)
    # A fresh independent evidence assessment owns remaining business gaps.
    # Otherwise a resolved question (or tool-limit diagnostic) could never leave
    # the list, causing unnecessary web calls and meaningless model proposals.
    missing = (work.record.open_questions + work.record.conflicts
               + [action.question for action in work.record.research_actions])
    if not work.evidence and not missing:
        missing = ['No original sources supporting the current business task were obtained']
    work.missing = list(dict.fromkeys(missing))
    tools.session["missing"] = work.missing
    tools.session["research_actions"] = [action.model_dump() for action in work.record.research_actions]
    tools.session["assessment_version"] = 2
    tools.session["assessed_step"] = tools.session["step"]
    tools.session["assessed_evidence"] = evidence_fingerprint(work.evidence)
    engine.store.save_work_state(engine.job_id, work)


def concurrent_source_read(step, arguments):
    """Only immutable source I/O may overlap; aliases/state commit afterwards."""
    if step.tool in {"fs.inspect", "fs.view"}:
        return True
    if step.tool in {"fs.list", "fs.search", "fs.read"}:
        return arguments.get("scope", "sources") == "sources" and not any(
            key in arguments for key in ("execution_id", "artifact_id")
        )
    return False


def _read_progress_entry(step, arguments, request_id, result):
    key = digest(json.dumps({k: v for k, v in arguments.items()
                             if k not in {"offset", "character_offset", "limit", "max_chars"}},
                            sort_keys=True) + step.tool)
    return key, {
        "tool": step.tool, "arguments": arguments, "request_id": request_id,
        "next_offset": result.get("next_offset"),
        "next_character_offset": result.get("next_character_offset"),
        "pagination_complete": result.get("next_offset") is None,
    }


def _source_result_context(tools, pending, correlation):
    """Preview the next request without committing evidence, notes or aliases."""
    baseline = tools.session.get("pending_model_context")
    context = deepcopy(baseline if baseline is not None else tools.context_sources)
    presentation = copy(tools.presentation)
    for field in ("files", "evidence", "ranges", "cursors", "legacy_cursors"):
        setattr(presentation, field, deepcopy(getattr(presentation, field)))
    memory = ResearchMemory(tools.memory.dump())
    if baseline is None:
        observations = list(tools.session["observations"])
    else:
        since = tools.session["pending_context_observation_count"]
        observations = list(context.get("observations", [])) + tools.session["observations"][since:]
    evidence = dict(tools.evidence)
    progress = deepcopy(tools.session.get("read_progress", {}))
    images = list(tools.session.get("page_images", []))
    notes, missing = tools.session["notes"], tools.session["missing"]
    for item, original in pending:
        step, result = item["step"], deepcopy(original)
        text = result.get("model_text")
        if text is None:
            text = presentation.result_text(step.tool, item["arguments"], result)
        result["model_text"] = text
        observation = {
            "tool": step.tool, "tool_call": step.tool_call, "arguments": item["arguments"],
            "request_id": item["request_id"], "result": result, "model_text": text,
            **correlation,
        }
        observations.append(observation)
        memory.record_observation(observation)
        for raw in result.get("evidence", []):
            value = Evidence.model_validate(raw)
            evidence[value.evidence_id] = value
        images.extend(result.get("page_images", []))
        if (step.tool in {"fs.read", "fs.inspect", "fs.search", "fs.list"}
                and result.get("status") not in {"failed", "interrupted"}):
            key, value = _read_progress_entry(step, item["arguments"], item["request_id"], result)
            progress[key] = value
        if step.notes.strip():
            notes = step.notes
        if step.provided_metadata("missing_information"):
            missing = list(step.missing_information)
    catalog = [evidence_catalog_item(e) for e in list(evidence.values())[-40:]]
    pending_requests = memory.pending_requests()
    batches = memory.dump()["batches"]
    updates = dict(
        observations=observations, notes=notes, missing_information=missing,
        evidence_catalog=catalog, evidence_catalog_truncated=len(evidence) > 40,
        evidence_catalog_text=presentation.evidence_catalog_text(catalog),
        read_progress=list(progress.values()),
        read_progress_text=presentation.progress_text(list(progress.values())),
        research_notes=memory.markdown(evidence_label=presentation.evidence_id),
        research_history=memory.history_text(evidence_label=presentation.evidence_id),
        memory_pending_requests=pending_requests,
        memory_delivered_requests=[key for key in pending_requests if batches[key]["delivered"]],
        budget=tools.ledger.usage(tools.session["quota_task_id"]),
        step=tools.session["step"] + len(pending),
        round_steps_remaining=tools.config.max_steps_per_round - tools.session["round_steps"] - len(pending),
    )
    # Do not undo an already necessary navigation view by restoring every full
    # catalog/history field while measuring a new read. Tool results themselves
    # still contain all newly supplied source text and its continuation.
    navigation = context.get("context_navigation", {})
    for key, value in updates.items():
        field = key.removesuffix("_text")
        if field not in navigation:
            context[key] = value
    if images:
        context["page_images"] = images
    else:
        context.pop("page_images", None)
    return context


async def fit_source_results(tools, group, results, correlation):
    """Share remaining request capacity; never evict history to fit new reads."""
    from folio.research.transport import research_prompt_chars

    candidates = [
        index for index, (item, result) in enumerate(zip(group, results, strict=True))
        if item["step"].tool in {"fs.read", "fs.search"}
        and item["arguments"].get("scope", "sources") == "sources"
        and not {"execution_id", "artifact_id"} & item["arguments"].keys()
        and result.get("evidence") and result.get("status") not in {"failed", "interrupted"}
    ]
    if not candidates:
        return results
    engine = tools.engine
    runtime = getattr(engine, "runtime", None)
    limit = getattr(getattr(engine, "limits", None), "max_prompt_chars", tools.config.max_observation_chars)

    async def context_fits(context):
        local_fits = getattr(runtime, "fits_prompt_budget", None)
        if local_fits:
            prepared = engine.request_context("research_step", context)
            schema = engine.request_schema("research_step", prepared, ResearchBatch)
            return await local(local_fits, "research_step", prepared, schema, limit)
        if hasattr(engine, "request_capacity"):
            return (await engine.request_capacity("research_step", context, ResearchBatch))["fits"]
        return research_prompt_chars(context, ResearchBatch.model_json_schema(), task="research_step") <= limit

    async def fits(values):
        context = await local(_source_result_context, tools, list(zip(group, values, strict=True)), correlation)
        return await context_fits(context)

    if await fits(results):
        return results

    # The response may have added valid research notes before these reads.
    # If those notes make the existing window itself overflow, apply the normal
    # archival policy to that window first. Never archive merely to make space
    # for new source text when the existing window still fits.
    existing_context = await local(_source_result_context, tools, [], correlation)
    if not await context_fits(existing_context):
        from folio.research.context_views import fit_context

        async def local_capacity(_action, value, _schema):
            return {"fits": await context_fits(value)}

        tools.session["pending_model_context"] = await fit_context(
            SimpleNamespace(request_capacity=local_capacity), existing_context, ResearchBatch,
        )
        tools.session["pending_context_observation_count"] = len(tools.session["observations"])
        await local(tools.save_memory)
        if await fits(results):
            return results

    lengths = {index: sum(len(value["text"]) for value in results[index]["evidence"])
               for index in candidates}
    total = sum(lengths.values())

    async def page(budget):
        values = list(results)
        readers = asyncio.Semaphore(4)

        async def read(index):
            item = group[index]
            arguments = dict(item["arguments"])
            arguments.pop("scope", None)
            arguments["max_chars"] = max(1, lengths[index] * budget // total)
            function = tools.workspace.read if item["step"].tool == "fs.read" else tools.workspace.search
            async with readers:
                result = await local(function, **arguments)
            if result.get("next_offset") is not None:
                result["pagination_reason"] = "context_capacity"
            return result

        for index, value in zip(candidates, await asyncio.gather(*(read(i) for i in candidates)), strict=True):
            values[index] = value
        return values

    minimum = await page(0)
    if not await fits(minimum):
        raise ResearchLimitReached(
            "Existing research history and this batch's reading locations fill the model context; original text cannot be appended. Existing history and unread ranges are preserved"
        )
    lower, upper, best = 1, total - 1, minimum
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = await page(middle)
        if await fits(candidate):
            lower, best = middle + 1, candidate
        else:
            upper = middle - 1
    return best


async def execute_research_batch(tools, batch):
    """Admit in order, overlap independent reads, durably commit in call order.

    The persisted cursor advances with each observation. Ledger receipts survive
    a crash between execution and observation, so replay never charges twice.
    """
    session, ledger, config = tools.session, tools.ledger, tools.config
    task, engine, sandbox = tools.task, tools.engine, tools.sandbox
    round_id = session["round_id"]
    start = session["pending_batch_start"]
    cursor = session["pending_batch_cursor"]
    started_indices = session.setdefault(
        "pending_batch_started", list(range(len(batch.steps))) if session.get("pending_step") else [],
    )
    prior_ids = session.setdefault("pending_batch_evidence_ids", list(tools.evidence))
    prior_evidence = {key: tools.evidence[key] for key in prior_ids if key in tools.evidence}
    prior_aliases = session.setdefault("pending_batch_evidence_aliases", dict(tools.presentation.evidence))
    def resolve_prior(key):
        return next((original for original, alias in prior_aliases.items() if alias == key), key)

    batch_id = digest(json.dumps([
        task.task_id, round_id, start, batch.model_dump(exclude={"reasoning_content"}),
    ], sort_keys=True))
    correlation = {
        "batch_id": batch_id,
        "batch_tool_calls": [step.tool_call for step in batch.steps if step.tool_call],
        "assistant_content": batch.assistant_content,
        **({"reasoning_content": batch.reasoning_content} if batch.reasoning_content is not None else {}),
    }
    mixed_finish = len(batch.steps) > 1 and any(step.tool == "finish" for step in batch.steps)
    from folio.runtime.image_inputs import MAX_IMAGES

    allowed_views = {index for index, step in enumerate(batch.steps) if step.tool == "fs.view"}
    allowed_views = set(sorted(allowed_views)[:MAX_IMAGES])
    prepared = []
    stop_reason = ""
    # Never validate notes against evidence from a sibling tool whose result the
    # model has not received yet. Admission/notes precede every dispatch.
    for index, step in enumerate(batch.steps):
        if index < cursor:
            continue
        call_id = step.tool_call.get("id") or digest(json.dumps(step.model_dump(), sort_keys=True))
        error, note_changes = None, 0
        arguments = dict(step.arguments)
        try:
            if mixed_finish:
                raise ValueError('finish must be called alone. This batch mixed it with other tools and was not executed. Submit the tool calls again.')
            if step.tool == "fs.view" and index not in allowed_views:
                raise ValueError(f'One model request accepts at most {MAX_IMAGES} original-page images. This viewing request was not executed; continue in the next batch.')
            arguments = await local(tools.presentation.resolve_arguments, step.arguments, tool=step.tool)
            note_changes = tools.memory.apply(
                step.note_updates(), evidence=prior_evidence,
                resolve_evidence=resolve_prior, call_id=call_id,
            )
            repeat = tools.memory.check_repeat(step.tool, arguments, step.review_reason)
            if repeat:
                raise ValueError(repeat)
        except (ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        if step.tool == "finish" and error is None:
            # Unused excerpts need not become notes. Their originals remain
            # protected from compaction; the following assessment checks actual
            # business gaps, evidence and required coverage.
            update_step_metadata(session, step)
            session["phase"] = "assess"
            result = {"status": "completed", "message": 'This research round has been submitted. The program will distinguish actionable verification questions from items needing user confirmation; when no actionable research remains, body preparation begins.'}
            tools.remember_observation({
                "tool": step.tool, "tool_call": step.tool_call, "arguments": {},
                "request_id": step.tool_call.get("id", ""), "result": result,
                "model_text": result["message"], **correlation,
            })
            _clear_pending_batch(session)
            await local(tools.save_memory)
            return
        # Preserve pre-batch operation identity for existing pending_step receipts.
        arguments_json = (session.get("pending_arguments_json", step.arguments_json)
                          if len(batch.steps) == 1 else step.arguments_json)
        request_id = digest(json.dumps([task.task_id, round_id, start + index, step.tool,
                                       arguments_json], sort_keys=True))
        previous = ledger.get(request_id)
        usage = ledger.usage(session["quota_task_id"])
        timeout = (previous["reserved_seconds"] if previous else
                   min(config.max_seconds_per_execution, max(0, config.max_execution_seconds - usage["seconds"])))
        reserve = timeout if step.tool == "sandbox.exec" else 0
        fresh, admitted = False, True
        try:
            if previous is None and start + index >= config.max_steps:
                raise ResearchLimitReached("Autonomous research reached this round's step limit")
            if previous is None and session["round_steps"] + index - cursor >= config.max_steps_per_round:
                raise ResearchLimitReached("This research round's steps are exhausted. Rechecking whether existing sources are sufficient")
            if previous is None and step.tool == "sandbox.exec" and reserve < 1:
                raise ResearchLimitReached('The script-execution allowance for this business task is exhausted')
            fresh = ledger.begin(request_id, session["quota_task_id"], round_id, start + index,
                                 step.tool, json.loads(arguments_json), config=config, reserve=reserve)
            if previous and previous["status"] == "running" and index not in started_indices:
                fresh = True  # Admitted before cancellation, but not dispatched yet.
        except ResearchLimitReached as exc:
            error, admitted = str(exc), False
            stop_reason = str(exc)
        prepared.append({
            "index": index, "step": step, "call_id": call_id, "arguments": arguments,
            "request_id": request_id, "fresh": fresh, "admitted": admitted,
            "timeout": timeout, "reserve": reserve, "error": error,
            "note_changes": note_changes, "started": monotonic(),
        })
    await local(tools.save_memory)

    async def execute(item):
        step, request_id = item["step"], item["request_id"]
        if not item["admitted"]:
            return {"status": "failed", "error": item["error"]}
        if not item["fresh"]:
            previous = ledger.get(request_id)
            if previous["result"] is not None:
                return previous["result"]
            await sandbox.cancel_execution(request_id)
            return {"status": "interrupted", "message": "The previous execution was not fully committed and must not be treated as successful. Submit the operation again if needed."}
        engine.store.work_event(engine.job_id, f"“{task.title}”研究资料：{step.summary or step.tool}")
        try:
            if item["error"]:
                raise ValueError(item["error"])
            return await tools.dispatch(step, request_id, item["timeout"], resolved_arguments=item["arguments"])
        except asyncio.CancelledError:
            await sandbox.cancel_execution(request_id)
            raise
        except ResearchLimitReached as exc:
            item["limit_error"] = str(exc)
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)[:3500] or type(exc).__name__}

    async def commit(item, result):
        nonlocal stop_reason
        step, request_id = item["step"], item["request_id"]
        result["model_text"] = await local(tools.present_result, step, result)
        if item["admitted"]:
            status = result.get("status", "completed")
            if status not in {"failed", "interrupted"}:
                status = ("failed" if step.tool == "sandbox.exec" and status not in {"completed", "approved"}
                          else "completed")
            ledger.finish(request_id, result, elapsed=(monotonic() - item["started"] if item["fresh"]
                          else item["timeout"]) if item["reserve"] else 0, status=status)
        for raw in result.get("evidence", []):
            evidence = Evidence.model_validate(raw)
            if evidence.metadata.get("provenance") != "derived" or not derived_evidence_error(evidence):
                tools.evidence[evidence.evidence_id] = evidence
        session["evidence"] = [e.model_dump() for e in tools.evidence.values()]
        if result.get("page_images"):
            session.setdefault("page_images", []).extend(result["page_images"])
        tools.remember_observation({
            "tool": step.tool, "tool_call": step.tool_call, "arguments": item["arguments"],
            "request_id": request_id, "result": result, "model_text": result["model_text"], **correlation,
        })
        result_key = result_progress_key(step.tool, result)
        seen = session.setdefault("research_seen_results", [])
        progressed = bool(item["note_changes"]) or (
            step.tool != "research.save" and result.get("status") not in {"failed", "interrupted"}
            and result_key not in seen
        )
        if result_key not in seen:
            seen.append(result_key)
        stalled = tools.memory.record_attempt(item["call_id"], progressed)
        if not mixed_finish:
            update_step_metadata(session, step)
        session["step"] += 1
        session["round_steps"] += 1
        session["pending_batch_stalled"] = stalled
        if step.tool in {"fs.read", "fs.inspect", "fs.search", "fs.list"} and result.get("status") not in {"failed", "interrupted"}:
            key, value = _read_progress_entry(step, item["arguments"], request_id, result)
            session.setdefault("read_progress", {})[key] = value
        session["pending_batch_cursor"] = item["index"] + 1
        await local(tools.save_memory)
        engine.store.audit_event(engine.job_id, "research_tool", {
            "task_id": task.task_id, "round_id": round_id, "request_id": request_id,
            "tool": step.tool, "status": result.get("status", "completed"),
            "tool_call_id": step.tool_call.get("id"), "batch_id": batch_id,
        })
        stop_reason = item.get("limit_error", stop_reason)

    # Mutating operations are barriers. Read all independent siblings before
    # fitting their combined results; four-worker waves bound actual I/O only.
    position = 0
    while position < len(prepared):
        group = [prepared[position]]
        if concurrent_source_read(group[0]["step"], group[0]["arguments"]):
            while (position + len(group) < len(prepared)
                   and concurrent_source_read(prepared[position + len(group)]["step"],
                                              prepared[position + len(group)]["arguments"])):
                group.append(prepared[position + len(group)])
        results = []
        for wave_start in range(0, len(group), 4):
            wave = group[wave_start:wave_start + 4]
            for item in wave:
                if item["index"] not in started_indices:
                    started_indices.append(item["index"])
            await local(tools.save_memory)
            workers = [asyncio.create_task(execute(item)) for item in wave]
            try:
                results.extend(await asyncio.gather(*workers))
            except BaseException:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                # Keep completed siblings' receipts; never fabricate an
                # observation for an unfinished or not-yet-dispatched call.
                completed = list(zip(group[:wave_start], results, strict=True))
                completed.extend((item, worker.result())
                                 for item, worker in zip(wave, workers, strict=True)
                                 if worker.done() and not worker.cancelled() and worker.exception() is None)
                for item, result in completed:
                    if item["admitted"]:
                        status = result.get("status") if result.get("status") in {"failed", "interrupted"} else "completed"
                        ledger.finish(item["request_id"], result, status=status)
                raise
        results = await fit_source_results(tools, group, results, correlation)
        for offset, (item, result) in enumerate(zip(group, results, strict=True)):
            try:
                await commit(item, result)
            except BaseException:
                # A stop during ordered publication must not discard already
                # successful siblings. Leave the interrupted item's receipt as-is.
                for sibling, value in zip(group[offset + 1:], results[offset + 1:], strict=True):
                    if sibling["admitted"]:
                        status = value.get("status") if value.get("status") in {"failed", "interrupted"} else "completed"
                        ledger.finish(sibling["request_id"], value, status=status)
                raise
        position += len(group)
    if session.get("pending_batch_stalled", 0) >= 3:
        session["phase"] = "assess"
        session["round_stop_reason"] = 'Consecutive research actions added no valid notes or sources. Entering bounded source-gap verification'
    _clear_pending_batch(session)
    await local(tools.save_memory)
    if stop_reason:
        if stop_reason == "This research round's steps are exhausted. Rechecking whether existing sources are sufficient":
            session["phase"] = "assess"
            session["round_stop_reason"] = stop_reason
            await local(tools.save_memory)
        else:
            raise ResearchLimitReached(stop_reason)


def _clear_pending_batch(session):
    for key in ("pending_batch", "pending_batch_start", "pending_batch_cursor", "pending_batch_started",
                "pending_batch_evidence_ids", "pending_batch_evidence_aliases", "pending_batch_stalled",
                "pending_model_context", "pending_context_observation_count",
                "pending_step", "pending_arguments_json"):
        session.pop(key, None)


async def research_business(engine, task, work, *, writing_gaps=()):
    from folio.confirmations import rounds

    config = engine.request.research
    confirmations = rounds(engine.store, engine.job_id)
    grant_id = confirmations[-1]["request_id"] if confirmations else ""
    sources = await local(source_workspace, engine)
    policy = load_research_policy(engine.store.root)
    identity = [work.context_key, list(writing_gaps), policy]
    if grant_id:
        identity.append(grant_id)
    round_id = digest(json.dumps(identity, sort_keys=True))
    root = engine.store.root / "jobs" / engine.job_id / "research"
    task_root = root / "tasks" / task.task_id / round_id
    facts_id = digest(json.dumps(work.global_facts, sort_keys=True))
    assignment = {
        "task": task.model_dump(), "sections": [s.model_dump() for s in engine.task_sections(task)],
        "facts_snapshot_id": facts_id, "global_facts": work.global_facts,
        "dependencies": engine.dependency_context(task), "writing_gaps": list(writing_gaps),
        "instruction": work.research_instruction,
    }
    workspace = await local(WorkspaceManager, sources, task_root, cache_root=engine.store.root / "source-cache")
    assignment["source_snapshot_id"] = workspace.source_snapshot_id
    ledger = ResearchLedger(root / "ledger.sqlite")
    session = ledger.load_session(task.task_id, round_id)
    restored_session = bool(session.get("round_id"))
    from folio.research.session_memory import prepare_session_memory

    memory_root = await local(prepare_session_memory, ledger, task, work, workspace, session,
                              assignment, policy, round_id=round_id)
    if memory_root != workspace.task_root:
        workspace = await local(WorkspaceManager, sources, memory_root,
                                cache_root=engine.store.root / "source-cache")
    task_root = workspace.task_root
    from folio.research.planning_handoff import seed_directory_research

    directory_research = await local(seed_directory_research, engine, task, work, workspace)
    sandbox = SandboxManager(sources, task_root, policy | {
        "max_timeout_seconds": config.max_seconds_per_execution,
    })
    # A new explicitly submitted continuation grants a new bounded research
    # allowance; ordinary retries/repairs retain the previous grant's accounting.
    quota_task_id = task.task_id + (":" + grant_id if grant_id else "")
    session["round_id"] = round_id
    session["quota_task_id"] = quota_task_id
    session.setdefault("phase", "local")
    session.setdefault("round_steps", 0)
    session.pop("memory_finish_pending", None)  # Legacy note-only completion gate.
    session.setdefault("unresolved_artifact_issues", deepcopy(work.research.get("unresolved_artifact_issues", {})))
    tools = ResearchTools(engine, task, work, workspace, sandbox, ledger, session)
    directory_web = directory_research.get("web_research", {})
    seeded_web_ids = {source["evidence_id"] for source in directory_web.get("sources", [])}
    # A resumed session cannot overwrite the revalidated frozen web receipt with
    # an older cached copy carrying altered content or provenance metadata.
    tools.evidence.update({item.evidence_id: item for item in work.evidence if item.evidence_id in seeded_web_ids})
    await local(tools.save_memory)
    assignment["directory_research"] = directory_research
    if session.get("inherited_from"):
        assignment["research_continuation"] = (
            "Research notes, reading history, and progress were inherited from the same business task and matching source/fact versions. "
            "Use existing conclusions first and investigate current writing_gaps. An old finish represents only the previous research conclusion; "
            "it does not permit skipping newly raised questions."
        )
    session_context = task_root / "context" / "sessions" / round_id
    session_context.mkdir(parents=True, exist_ok=True)
    await local(workspace._atomic_json, session_context / "assignment.json", assignment)
    await local(workspace._atomic_json, task_root / "context" / "assignment.json", assignment)
    await local(workspace._atomic_json, task_root / "context" / "facts_snapshot.json", work.global_facts)
    interrupted_reason = ""
    try:
        from folio.research.coverage_obligations import establish_coverage

        coverage_requirements = await establish_coverage(tools, assignment)
        # Pre-fix sessions may have accepted finish, then reopened local research
        # solely because user-held details remained unknown. Reassess the saved
        # evidence once using the new disposition contract before another read.
        # An admitted pending tool batch must retain its normal replay identity.
        if (session.get("assessment_version") != 2
                and not session.get("pending_batch") and not session.get("pending_step")
                and restored_session
                and any(item.get("tool") == "finish" for item in session["observations"])):
            session["phase"] = "assess"
        while not session["finished"]:
            if session["phase"] == "assess" and not session.get("pending_batch") and not session.get("pending_step"):
                await assess_research(engine, task, work, tools, assignment)
                if session.get("local_round"):
                    ledger.finish_round(quota_task_id, session["local_round"])
                session.pop("local_round", None)
                session["round_steps"] = 0
                session["phase"] = "local"
                if (not any(action["channel"] == "local_sources" for action in session["research_actions"])
                        and not session.get("coverage_unresolved")):
                    session["finished"] = True
                ledger.save_session(task.task_id, round_id, session)
                if session["finished"]:
                    break
            if not session.get("local_round"):
                number = ledger.begin_round(quota_task_id, round_id)
                if number is None:
                    session.update(finished=True, local_exhausted=True)
                    ledger.save_session(task.task_id, round_id, session)
                    break
                session["local_round"] = number
                ledger.save_session(task.task_id, round_id, session)
                engine.store.work_event(engine.job_id, f"“{task.title}”开始第 {number}/3 轮本地资料研究。")
            if session["round_steps"] >= config.max_steps_per_round and not session.get("pending_batch") and not session.get("pending_step"):
                session["round_stop_reason"] = "This research round's steps are exhausted. Rechecking whether existing sources are sufficient"
                session["phase"] = "assess"
                ledger.save_session(task.task_id, round_id, session)
                continue
            if session["step"] >= config.max_steps and not session.get("pending_batch") and not session.get("pending_step"):
                raise ResearchLimitReached("Autonomous research reached this round's step limit")
            catalog = await local(workspace.list)
            catalog["model_text"] = tools.presentation.catalog_text(catalog)
            for observation in session["observations"]:
                if "model_text" not in observation:
                    record = ledger.get(observation["request_id"])
                    args = json.loads(record["arguments"]) if record else {}
                    observation["model_text"] = tools.presentation.result_text(
                        observation["tool"], args, observation["result"])
            context = assignment | {
                "catalog": catalog,
                "coverage_requirements": coverage_requirements,
                "notes": session["notes"], "missing_information": session["missing"],
                "research_actions": session.get("research_actions", []),
                "observations": observation_view(session["observations"], config.max_observation_chars, tools.memory),
                "evidence_catalog": [evidence_catalog_item(e) for e in list(tools.evidence.values())[-40:]],
                "evidence_catalog_truncated": len(tools.evidence) > 40,
                "budget": ledger.usage(quota_task_id), "step": session["step"],
                "research_phase": "local", "local_round": session["local_round"],
                "max_local_rounds": 3, "round_steps_remaining": config.max_steps_per_round - session["round_steps"],
                "read_progress": list(session.get("read_progress", {}).values()),
            } | tools.research_context()
            context["evidence_catalog_text"] = tools.presentation.evidence_catalog_text(context["evidence_catalog"])
            context["read_progress_text"] = tools.presentation.progress_text(context["read_progress"])
            await local(tools.presentation.save)
            if session.get("page_images"):
                context["page_images"] = session["page_images"]
            tools.prepare_context_sources(context)
            if session.get("pending_batch"):
                batch = ResearchBatch.model_validate(session["pending_batch"])
            elif session.get("pending_step"):
                batch = ResearchBatch.model_validate(session["pending_step"])
            else:
                batch = await tools.model("research_step", context, ResearchBatch)
                batch = ResearchBatch.model_validate(batch)
                session.pop("page_images", None)
            session["pending_batch"] = batch.model_dump(exclude_unset=True)
            session.setdefault("pending_batch_start", session["step"])
            session.setdefault("pending_batch_cursor", 0)
            ledger.save_session(task.task_id, round_id, session)
            await execute_research_batch(tools, batch)
            if any(step.tool in {"fs.read", "artifact.save"} for step in batch.steps):
                coverage_requirements = await establish_coverage(tools, assignment)
    except ResearchLimitReached as exc:
        interrupted_reason = str(exc)
        session.update(finished=True, local_exhausted=True)
        session["incomplete"] = interrupted_reason
        await local(tools.save_memory)
    finally:
        await sandbox.close()
    work.evidence = list(tools.evidence.values())
    if (session.get("assessed_step") != session["step"] or interrupted_reason
            or session.get("assessment_version") != 2
            or session.get("assessed_evidence") != evidence_fingerprint(work.evidence)):
        await assess_research(engine, task, work, tools, assignment)
    else:
        work.missing = list(session["missing"])
    # The coordinator, rather than a model-selected tool, owns phase transitions.
    # Tavily attempts are reserved durably by supplement_web and never reset on retry.
    for _ in range(max(0, 2 - len(work.web_searches))):
        public_questions = list(dict.fromkeys(
            action["question"] for action in session.get("research_actions", [])
            if action["channel"] == "public_web"
        ))
        if not public_questions or not engine.web_search_available(work):
            break
        session["phase"] = "web"
        ledger.save_session(task.task_id, round_id, session)
        before = {e.evidence_id for e in work.evidence}
        await engine.supplement_web(task, work, questions=public_questions, previous_research=directory_web)
        tools.evidence.update({e.evidence_id: e for e in work.evidence})
        if any(e.evidence_id not in before for e in work.evidence):
            await assess_research(engine, task, work, tools, assignment)
        session["evidence"] = [e.model_dump() for e in tools.evidence.values()]
        ledger.save_session(task.task_id, round_id, session)
    public_pending = any(action["channel"] == "public_web" for action in session.get("research_actions", []))
    web_status = ("not_needed" if not public_pending else
                  "unavailable" if engine.web_knowledge is None else "exhausted")
    if public_pending and web_status == "unavailable":
        engine.store.work_event(engine.job_id, f"“{task.title}”未配置可用的 Tavily 搜索，保留资料缺口并生成标注建议。")
    session["phase"] = "completion" if work.missing else "ready"
    session["fallback_ready"] = bool(work.missing)
    ledger.save_session(task.task_id, round_id, session)
    work.research = {"version": 2, "round_id": round_id, "source_snapshot_id": workspace.source_snapshot_id,
                     "facts_snapshot_id": facts_id, "task_root": str(task_root),
                     "directory_research": directory_research,
                     "coverage_ids": session.get("coverage_ids", []), "usage": ledger.usage(quota_task_id),
                     "coverage_contract": session.get("coverage_contract", {}),
                     "required_coverage_ids": session.get("required_coverage_ids", []),
                     "coverage_unresolved": session.get("coverage_unresolved", []),
                     "unresolved_artifact_issues": session.get("unresolved_artifact_issues", {}),
                     "quota_task_id": quota_task_id,
                     "local_rounds": len(ledger.local_rounds(quota_task_id)),
                     "phase": session["phase"], "fallback_ready": session["fallback_ready"],
                     "web_rounds": len(work.web_searches),
                     "web_status": web_status,
                     "read_progress": session.get("read_progress", {}),
                     "model_deliveries": session.get("model_deliveries", []),
                     "research_memory": tools.memory.dump(),
                     "research_actions": session.get("research_actions", []),
                     "research_notes_path": str(task_root / "research.md"),
                     "incomplete": session.get("incomplete", interrupted_reason), "notes": session["notes"]}
    if work.missing:
        await engine.complete_gaps(task, work, work.missing)
    work.researched = work.writable = True
    work.review_key = ""
    engine.store.save_work_state(engine.job_id, work)
    return work


def coverage_context(work):
    """Tell writers/reviewers which exact source scopes were enumerated."""
    if not work.research:
        return {}
    from folio.research.artifact_coverage import CoverageManager

    task_root = Path(work.research["task_root"])
    manager = CoverageManager(task_root, WorkspaceManager(task_root.parents[2] / "sources", task_root))
    scopes = []
    for cid in work.research.get("coverage_ids", []):
        record = manager._load(cid)
        scope = record["scope"]
        scopes.append({"coverage_id": cid, "source_id": scope["source_id"],
                       "source_version": scope["source_version"],
                       "locator_prefix": scope["locator_prefix"], "node_id": scope.get("node_id"),
                       "total": len(scope["items"]),
                       "exclusions": [{"item_id": key, "reason": value.get("review_reason", "")}
                                      for key, value in record["dispositions"].items()
                                      if value["status"] == "not_applicable" and value.get("review_key")]})
    from folio.research.coverage_obligations import coverage_summary

    return {"scopes": scopes, "contract": coverage_summary(work.research),
            "incomplete": work.research.get("incomplete", ""),
            "instruction": "Check actual body citations against each original inventory item; a blanket claim of coverage is insufficient. "
            "If the section requires all clauses/fields but the source scope lacks an inventory or includes unread ranges, report a coverage gap. "
            "No inventory means none was registered, not that exhaustive checking is complete. It also does not create an exhaustive-coverage obligation for sections without one."}


def research_findings(engine, task, work, states, *, update_coverage=True):
    """Recheck coverage against final body revisions; research notes cannot waive it."""
    if not work.research:
        return []
    from folio.research.artifact_issues import findings as artifact_findings

    findings = artifact_findings(work.research, task.section_ids, states=states)
    coverage_unresolved = list(work.research.get("coverage_unresolved", []))
    contract = work.research.get("coverage_contract", {})
    if contract.get("status") == "unavailable" and not coverage_unresolved:
        coverage_unresolved.append('Independent determination of complete-coverage requirements is incomplete')
    expected_scopes = {(scope["source_id"], scope["locator_prefix"], scope.get("node_id"))
                       for scope in contract.get("required_scopes", [])}
    if len(work.research.get("required_coverage_ids", [])) < len(expected_scopes) and not coverage_unresolved:
        coverage_unresolved.append('Coverage inventories have not been created for all independently determined required ranges')
    for gap in coverage_unresolved:
        findings.append(Finding(kind="pending", key="research-coverage:" + task.task_id + ":" + digest(gap),
                                description=gap[:3500], section_ids=task.section_ids))
    if work.research.get("incomplete"):
        findings.append(Finding(kind="pending", key="research-incomplete:" + task.task_id,
                                description=work.research["incomplete"][:3500], section_ids=task.section_ids))
    coverage_ids = list(dict.fromkeys(work.research.get("coverage_ids", [])
                                     + work.research.get("required_coverage_ids", [])))
    if coverage_ids:
        from folio.research.artifact_coverage import CoverageManager

        task_root = Path(work.research["task_root"])
        workspace = WorkspaceManager(task_root.parents[2] / "sources", task_root)
        manager = CoverageManager(task_root, workspace)
        for cid in coverage_ids:
            record = manager._load(cid)
            # Link actual saved blocks; never rely on model-invented future block IDs.
            for item in record["scope"]["items"]:
                refs = manager.covering_references(record["scope"], item, states)
                if refs and update_coverage:
                    manager.record(cid, item["item_id"], "covered", references=refs)
            report = manager.report(cid, states)
            # Keep all missing entries outside the bounded, user-facing finding.
            # Read-only checks must not overwrite the persisted review report.
            report_path = task_root / ".research" / "coverage_reports" / (cid + ".json")
            if update_coverage:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                workspace._atomic_json(report_path, report | {
                    "section_revisions": {state.section_id: state.revision for state in states},
                })
            if not report["complete"]:
                findings.append(Finding(kind="coverage", key="research-coverage:" + cid,
                                        description=f"Complete source coverage is still missing {len(report['missing'])}/{report['total']} items. "
                                        f"Coverage inventory: {cid}. Check the original requirements against the saved body citations.",
                                        section_ids=task.section_ids, rule="coverage"))
    return findings
