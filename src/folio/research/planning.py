"""Bounded, auditable source research for directory planning, independent of jobs."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

from folio.models import Evidence, digest
from folio.research.memory import ResearchMemory, ResearchNoteUpdate
from folio.research.planning_progress import (
    action_message,
    error_message,
    gap_message,
    result_message,
)
from folio.research.planning_replay import DirectoryResearchReplay
from folio.research.planning_web import DirectoryWebResearch
from folio.research.presentation import SourcePresentation
from folio.research.workspace import WorkspaceError, WorkspaceManager
from folio.runtime.tool_calls import MAX_TOOL_CALLS_PER_BATCH

DIRECTORY_RESEARCH_MAX_CALLS = 80
DIRECTORY_RESEARCH_MAX_SECONDS = 30 * 60


class PlanningResearchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_rounds: int = Field(default=3, ge=1, le=3)
    max_steps_per_round: int = Field(default=12, ge=1, le=40)
    max_calls: int = Field(default=DIRECTORY_RESEARCH_MAX_CALLS, ge=1, le=120)
    max_tool_calls: int = Field(default=120, ge=1, le=1000)
    max_seconds: float = Field(default=DIRECTORY_RESEARCH_MAX_SECONDS, gt=0, le=3600)
    call_timeout_seconds: float = Field(default=180, gt=0)
    # Compatibility for lightweight runtimes without token measurement. The
    # production runtime uses its shared token budget, not this character cap.
    max_context_chars: int = Field(default=600000, ge=4000)
    max_result_chars: int = Field(default=600000, ge=2000)
    max_evidence_chars: int | None = Field(default=None, ge=2000)


class PlanningRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=3500)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


class SelectedRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


class ExcludedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=3500)
    reason: str = Field(min_length=1, max_length=1000)


class UnreadAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=3500)
    blocks_directory: bool
    reason: str = Field(min_length=1, max_length=3500)


class WebFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1, description="W references returned by search_web; external reference only.")


class ResearchConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=6000)
    requirements: list[PlanningRequirement] = Field(default_factory=list, max_length=240)
    web_findings: list[WebFinding] = Field(default_factory=list, description="Optional public-web findings affecting outline choices. Keep separate from uploaded requirements.")
    # Internal handoff metadata, derived from cited originals rather than a
    # second set of model-authored identifiers.
    selected_ranges: SkipJsonSchema[list[SelectedRange]] = Field(default_factory=list)
    excluded_files: list[ExcludedFile] = Field(default_factory=list, max_length=500)
    unread_assessments: list[UnreadAssessment] = Field(default_factory=list, max_length=500)
    unresolved: list[str] = Field(default_factory=list, max_length=100)
    blocking_gaps: list[str] = Field(default_factory=list, max_length=100)
    local_research_actions: list[str] | None = Field(
        default=None,
        description="Concrete useful reads or checks still possible against available local sources. "
        "Return [] explicitly when remaining gaps require user input or unavailable material. "
        "Do not repeat completed reads to obtain bidder details or decisions not present in the sources. "
        "This does not remove unresolved questions or actual outline scope blockers.",
    )

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_ranges(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "selected_ranges"}
        return value


class PlanningResearchStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["fs.list", "fs.inspect", "fs.search", "fs.read", "search.web", "research.save", "finish"]
    arguments: dict = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=6000)
    research_notes: list[ResearchNoteUpdate] = Field(default_factory=list)
    review_reason: str = Field(default="", max_length=3500)
    # Preserve the provider's real call, including its ID and original arguments.
    # Resolved file aliases below are execution data, never replacement calls.
    tool_call: SkipJsonSchema[dict] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def bounded_arguments(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if isinstance(value.get("arguments"), dict):
            value["arguments"] = {key: item for key, item in value["arguments"].items() if item is not None}
        if len(json.dumps(value.get("arguments", {}), ensure_ascii=False)) > 1_024_000:
            raise ValueError('Tool arguments are too long')
        return value


class PlanningResearchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[PlanningResearchStep] = Field(min_length=1, max_length=MAX_TOOL_CALLS_PER_BATCH)
    assistant_content: str = ""
    reasoning_content: SkipJsonSchema[str | None] = None


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


async def _local(function, *args, **kwargs):
    # Cancellation waits for an active reader/index writer before caller cleanup
    # can remove or reuse this immutable input snapshot.
    from folio.research.workflow import local

    return await local(function, *args, **kwargs)


def _has_unread_content(file):
    # Some readers can supply a specific omission warning before exposing a
    # complete flag. Navigation-only warnings (e.g. missing PDF bookmarks) do
    # not imply that source business content is missing.
    # Keep legacy Chinese warnings readable alongside newly authored diagnostics.
    omission_markers = (
        "未完整读取", "尚未读取", "当前不提取", "未提取到文字", "尚未进行 OCR",
        "not fully read", "unread", "not currently extracted", "no text was extracted",
        "has not undergone ocr", "not extracted",
    )
    return file.get("source_complete") is False or any(
        marker in warning.lower() for warning in file.get("warnings", []) for marker in omission_markers
    )


def _limits(value):
    if value is None:
        return PlanningResearchLimits()
    if isinstance(value, PlanningResearchLimits):
        return value
    if isinstance(value, dict):
        return PlanningResearchLimits.model_validate(value)
    # The caller's general Limits do not become a second unbounded budget.
    return PlanningResearchLimits(
        max_calls=min(DIRECTORY_RESEARCH_MAX_CALLS, value.max_calls),
        call_timeout_seconds=value.call_timeout_seconds,
        max_seconds=min(DIRECTORY_RESEARCH_MAX_SECONDS, value.run_timeout_seconds),
        max_context_chars=value.max_prompt_chars,
    )


def _covered(pages, total):
    if total == 0:
        return True
    end = 0
    for start, stop in sorted(pages):
        if start > end:
            return False
        end = max(end, stop)
    return end >= total


def _register_read_segments(state, result):
    """Track characters within source blocks without promoting fragments to blocks."""
    segments = result.get("read_segments")
    if segments is None:
        state["pages"].append((result["offset"], result["offset"] + len(result["evidence"])))
    else:
        blocks = state.setdefault("character_pages", {})
        for segment in segments:
            block = blocks.setdefault(str(segment["offset"]), {
                "total": segment["total_characters"], "pages": [],
            })
            block["pages"].append((segment["character_offset"], segment["character_end"]))
            if _covered(block["pages"], block["total"]):
                state["pages"].append((segment["offset"], segment["offset"] + 1))
    state["complete"] = _covered(state["pages"], state["total"])
    offset = 0
    for start, end in sorted(state["pages"]):
        if start > offset:
            break
        offset = max(offset, end)
    character_offset = 0
    block = state.get("character_pages", {}).get(str(offset), {})
    for start, end in sorted(block.get("pages", [])):
        if start > character_offset:
            break
        character_offset = max(character_offset, end)
    state["next_offset"] = None if state["complete"] else offset
    state["next_character_offset"] = None if state["complete"] else character_offset


def _page(value, offset=0, limit=600000, **extra):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 600000:
        raise ValueError('limit must be between 1 and 600000')
    text = value if isinstance(value, str) else _json(value)
    end = min(offset + limit, len(text))
    return {"text": text[offset:end], "offset": offset, "total": len(text),
            "next_offset": end if end < len(text) else None,
            "complete": end >= len(text), **extra}


class _DirectoryResearch:
    def __init__(self, workspace, output, requirements, template, limits, progress, *, web=None,
                 output_language=None):
        self.workspace, self.output = workspace, output
        self.presentation = SourcePresentation(workspace)
        self.web = web
        self.web_results = {}
        self.delivered_web_evidence = set()
        self.limits, self.progress = limits, progress
        self.context_sources = {"requirements": requirements, "template": template}
        # Historical parameter name: this controls research activity wording,
        # not the document language derived from the user's writing request.
        self.output_language = output_language
        from folio.research.transport import research_text

        self.context_text = {"requirements": requirements,
                             "template": research_text({"template": template})}
        self.context_pages = {}
        self.events = output / "events"
        self.events.mkdir(exist_ok=True)
        self.calls = self.rounds = 0
        self.tool_calls = 0
        self.step_in_round = 0
        self.started = monotonic()
        self.notes = ""
        self.memory = ResearchMemory()
        self.conclusion = ResearchConclusion()
        self.evidence = {}
        self.ranges = {}
        self.structures = {}
        self.node_titles = {}
        self.listed = set()
        self.observations = []
        self.history = {}
        self.deliveries = []
        self.delivered_observations = set()
        self.delivered_evidence = set()
        self.delivered_structures = {}
        self.delivered_listed = set()
        self.delivered_text_pages = {}
        self.delivered_context_pages = {}
        self.result_budget = limits.max_result_chars
        self.issues = []
        self.usage = []
        self.reason = None
        self.termination_reason = None
        self.failure = None
        self.conclusion_submitted = False
        self.finalization_attempted = False
        self.last_progress = None
        self.waiting_message = ""
        self.replay = None
        self.files = {name: {"path": name, "name": Path(name).name,
                             "format": item["format"], "source_version": item["version"],
                             "inspected": False, "structure_complete": False,
                             "read": False, "characters": 0, "read_ranges": [], "readings": [],
                             "source_complete": None, "warnings": [],
                             "delivered_characters": 0, "structure_delivered": False}
                      for name, item in workspace.source_index.files.items()}

    async def notify(self, message, *, force=False):
        if message == self.last_progress:
            return
        if self.replay is not None and self.replay.active and not force:
            return  # Replayed steps were already reported by the interrupted run.
        self.last_progress = message
        if self.progress:
            value = self.progress(message)
            if inspect.isawaitable(value):
                await value

    def save(self, name, value):
        WorkspaceManager._atomic_json(self.output / name, value)

    def event(self, name, value):
        WorkspaceManager._atomic_json(self.events / name, value)

    def observe(self, entry):
        self.observations.append(entry)
        self.memory.record_observation(entry)

    def memory_context(self, field):
        if field == "research_notes":
            return self.memory.markdown(evidence_label=self.presentation.evidence_id)
        if field == "research_history":
            return self.memory.history_text(evidence_label=self.presentation.evidence_id)
        return self.context_text[field]

    def attempt(self, request_id, progress):
        """Bound repeated failed/no-op calls independently of the overall budget."""
        stalled = self.memory.record_attempt(request_id, progress=progress)
        if stalled >= 3:
            errors = [entry for entry in self.observations
                      if "error" in entry["result"] and entry.get("batch_id", entry["request_id"])
                      in {f"research_{number:03d}" for number in range(max(1, self.calls - 2), self.calls + 1)}]
            self.stop("tool_error" if errors else "no_progress",
                      'Three consecutive research calls obtained no new sources or valid notes. Further reading stopped; existing results will be organized')
            if errors:
                entry = errors[-1]
                self.failure = {"code": "tool_error", "message": entry["result"]["error"],
                                "request_id": entry["request_id"]}
            return False
        return True

    def stop(self, code, message):
        self.termination_reason = code
        self.reason = message

    def working_findings(self):
        """Retain source-backed work without promoting notes to approved requirements."""
        findings = []
        for note in self.memory.dump()["notes"]:
            references = note.get("references", {})
            if note["evidence_ids"] and all(
                key in self.delivered_evidence and key in self.evidence
                and key in references and all(
                    self.evidence[key].get(field, "") == references[key].get(field, "")
                    for field in ("source_id", "source_version", "locator"))
                for key in note["evidence_ids"]
            ):
                findings.append(note)
        return findings

    def conclude_notes(self, report, request_id):
        """A validated model conclusion can summarize its cited, delivered batches."""
        updates = [ResearchNoteUpdate(
            topic='Outline requirement: ' + requirement["requirement_id"], text=requirement["text"],
            kind="source_statement", evidence_ids=requirement["evidence_ids"],
        ) for requirement in report["requirements"]]
        return self.memory.apply(updates, evidence=self.evidence,
                                 resolve_evidence=self.presentation.resolve_evidence_id,
                                 call_id=request_id + ":conclusion")

    def parse_conclusion(self, arguments):
        # Old native calls can still include this field. Discard it before alias
        # resolution as well as model validation, including malformed old values.
        value = {key: item for key, item in arguments.items() if key != "selected_ranges"}
        conclusion = ResearchConclusion.model_validate(self.presentation.resolve_conclusion(value))
        for finding in conclusion.web_findings:
            if self.web is None:
                raise ValueError("No public-web references have been retrieved")
            ids = [self.web.resolve_reference(key).evidence_id for key in finding.evidence_ids]
            if not set(ids) <= self.delivered_web_evidence:
                raise ValueError("Web findings must cite public excerpts actually supplied to the model")
            finding.evidence_ids = list(dict.fromkeys(ids))
        return conclusion

    def accept_conclusion(self, arguments):
        self.conclusion = self.parse_conclusion(arguments)

    def conclusion_sources(self):
        """Associate cited, delivered originals with server-owned reading records."""
        requirements = self.conclusion.requirements
        cited = {key for item in requirements for key in item.evidence_ids}
        batches = self.memory.dump()["batches"].values()
        verified, selected = set(), []
        for scope in self.ranges.values():
            matching = set()
            for key in cited.intersection(scope["evidence_ids"], self.delivered_evidence):
                raw = self.evidence.get(key, {})
                reference = {field: raw.get(field, "") for field in ("source_id", "source_version", "locator")}
                if (raw.get("evidence_id") != key or reference["source_id"] != scope["path"]
                        or reference["source_version"] != self.files[scope["path"]]["source_version"]
                        or not reference["locator"]):
                    continue
                if any(batch["tool"] == "fs.read" and batch["path"] == scope["path"]
                       and key in batch["delivered_evidence_ids"]
                       and batch["references"].get(key) == reference for batch in batches):
                    matching.add(key)
            if matching:
                verified.update(matching)
                ids = [item.requirement_id for item in requirements if matching.intersection(item.evidence_ids)]
                selected.append(SelectedRange(
                    range_id=scope["range_id"], reason=('Read original text supporting outline requirements: ' + "、".join(ids))[:1000],
                ))
        self.conclusion.selected_ranges = selected
        return verified

    def navigation_signature(self):
        return (frozenset(self.listed), frozenset(self.node_titles),
                frozenset((path, tuple(page)) for path, value in self.structures.items()
                          for page in value["pages"]))

    def pending_view(self, entry):
        """A summarized prefix need not occupy the next delivery of a large batch."""
        batch = self.memory.dump()["batches"].get(entry["request_id"], {})
        reviewed = set(batch.get("reviewed_evidence_ids", []))
        if (not reviewed or entry["tool"] != "fs.read"
                or entry.get("arguments", {}).get("scope", "sources") != "sources"):
            return entry
        evidence = entry["result"].get("evidence", [])
        prefix = 0
        while prefix < len(evidence) and evidence[prefix]["evidence_id"] in reviewed:
            prefix += 1
        if not prefix or prefix == len(evidence):
            return entry
        from copy import deepcopy

        view = deepcopy(entry)
        view["result"]["evidence"] = view["result"]["evidence"][prefix:]
        if "read_segments" in view["result"]:
            view["result"]["read_segments"] = view["result"]["read_segments"][prefix:]
            first = view["result"]["read_segments"][0]
            view["result"].update(offset=first["offset"], character_offset=first["character_offset"])
        else:
            view["result"]["offset"] += prefix
        view["model_text"] = self.presentation.result_text(view["tool"], view["arguments"], view["result"])
        return view

    def visible_evidence(self, entry):
        """Only whole, byte-matching quotes are delivered; labels alone prove nothing."""
        text = entry.get("model_text", "")
        keys = []
        for span in entry["result"].get("model_evidence_spans", []):
            key = span.get("evidence_id")
            source = self.evidence.get(key)
            start, end = span.get("start"), span.get("end")
            if (source and span.get("complete", True) and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(text) and text[start:end] == source["text"]):
                keys.append(key)
        return keys

    def register(self, tool, args, result):
        if tool in {"fs.inspect", "fs.read"} and args.get("scope", "sources") == "sources":
            file = self.files[args["path"]]
            file["warnings"] = list(dict.fromkeys([*file["warnings"], *result.get("warnings", [])]))
            complete = result.get("source_complete") if tool == "fs.read" else result.get("complete")
            if isinstance(complete, bool):
                file["source_complete"] = file["source_complete"] is not False and complete
        if tool == "fs.list":
            self.listed.update(item["path"] for item in result["files"])
        elif tool == "fs.inspect":
            path = args["path"]
            self.files[path]["inspected"] = True
            for node in [result.get("node"), *result.get("structure", [])]:
                if node:
                    self.node_titles[(path, node["node_id"])] = node["title"]
            if not args.get("node_id"):
                state = self.structures.setdefault(path, {"pages": [], "total": result["structure_total"]})
                state["pages"].append((result["offset"], result["offset"] + len(result["structure"])))
                self.files[path]["structure_complete"] = _covered(state["pages"], state["total"])
        elif tool == "fs.read" and args.get("scope", "sources") == "sources":
            selector = {key: value for key, value in args.items()
                        if key not in {"offset", "character_offset", "limit", "max_chars", "scope"} and value is not None}
            key = "range_" + digest(json.dumps(selector, sort_keys=True, ensure_ascii=False))
            state = self.ranges.setdefault(key, {
                "range_id": key, "path": args["path"], "selector": selector,
                "pages": [], "total": result["total"], "evidence_ids": [],
                "delivered_pages": [], "delivered_evidence_ids": [], "delivered_complete": False,
            })
            _register_read_segments(state, result)
            for raw in result["evidence"]:
                evidence = Evidence.model_validate(raw)
                self.evidence[evidence.evidence_id] = evidence.model_dump()
                if evidence.evidence_id not in state["evidence_ids"]:
                    state["evidence_ids"].append(evidence.evidence_id)
            if "read_segments" in result:
                state.setdefault("evidence_segments", {}).update({
                    raw["evidence_id"]: segment
                    for raw, segment in zip(result["evidence"], result["read_segments"], strict=True)
                })
            file = self.files[args["path"]]
            file["read"] = file["read"] or bool(result["evidence"])
            file["characters"] = sum(len(item["text"]) for item in self.evidence.values()
                                     if item["source_id"] == args["path"])
            file["read_ranges"] = [entry for entry in self.ranges.values() if entry["path"] == args["path"]]
            readings = []
            for entry in file["read_ranges"]:
                if not entry["evidence_ids"]:
                    continue
                chosen = entry["selector"]
                label = self.node_titles.get((args["path"], chosen.get("node_id")))
                label = label or chosen.get("locator") or (
                    f"Sheet {chosen['sheet']} {chosen.get('cell_range', '')}" if chosen.get("sheet") else
                    f"Page {chosen['page']}" if chosen.get("page") else
                    f"Slide {chosen['slide']}" if chosen.get("slide") else 'Document body'
                )
                readings.append({"label": label, "range_id": entry["range_id"],
                                 "characters": sum(len(self.evidence[key]["text"]) for key in entry["evidence_ids"]),
                                 "complete": entry["complete"]})
            file["readings"] = readings
            result["range_id"] = key
            result["range_complete"] = state["complete"]

    async def dispatch(self, tool, args):
        args = dict(args)
        if tool == "search.web":
            if self.web is None or not self.web.available:
                raise ValueError("Public-web search is unavailable")
            remaining = self.limits.max_seconds - (monotonic() - self.started)
            if remaining <= 0:
                raise TimeoutError("Directory research time budget exhausted")
            try:
                return await asyncio.wait_for(self.web.search(**args, max_chars=self.limits.max_result_chars),
                                              timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError('Web search exceeded the remaining research time. Existing sources and results are preserved') from exc
        if tool == "research.save":
            return {"status": "saved", "message": 'Valid research notes were saved. No new sources were read.'}
        if tool == "fs.read" and args.get("scope", "sources") != "sources":
            scope = args.pop("scope")
            args["limit"] = min(args.get("limit", self.limits.max_result_chars), self.limits.max_result_chars)
            if scope == "history":
                request_id = args.pop("request_id")
                if request_id not in self.history:
                    raise ValueError('The tool record does not exist')
                entry = self.web_results.get(request_id) or next(
                    item for item in self.observations if item["request_id"] == request_id)
                # History is the same readable text, never a JSON document inside a string.
                from folio.research.context_views import page_evidence_spans

                page = _page(entry["model_text"], **args, original_request_id=request_id)
                spans = page_evidence_spans(entry["result"].get("model_evidence_spans", []),
                                            page["offset"], len(page["text"]))
                keys = {span["evidence_id"] for span in spans}
                return page | {"model_evidence_spans": spans,
                               "evidence": [self.evidence[key] for key in keys if key in self.evidence]}
            if scope == "context":
                field = args.pop("field")
                if field not in {*self.context_sources, "research_notes", "research_history"}:
                    raise ValueError('Only requirements, template, research_notes, or research_history can be read')
                result = _page(self.memory_context(field), **args)
                if field in self.context_sources:
                    self.context_pages.setdefault(field, []).append(
                        (result["offset"], result["offset"] + len(result["text"]))
                    )
                return result
            if scope == "evidence":
                evidence_id = args.pop("evidence_id", None)
                if evidence_id is not None and evidence_id not in self.evidence:
                    raise ValueError('The evidence is not in the record of read original text')
                value = (f"[{self.presentation.evidence_id(evidence_id)}] " + self.evidence[evidence_id]["text"]
                         if evidence_id else "\n".join(
                             f"{self.presentation.evidence_id(key)}：{self.presentation.file_id(item['source_id'])} {item['locator']}"
                             for key, item in self.evidence.items()))
                page = _page(value, **args, evidence_id=evidence_id)
                if evidence_id:
                    from folio.research.context_views import page_evidence_spans

                    spans = [{"evidence_id": evidence_id,
                              "start": len(value) - len(self.evidence[evidence_id]["text"]), "end": len(value)}]
                    page |= {"model_evidence_spans": page_evidence_spans(spans, page["offset"], len(page["text"])),
                             "evidence": [self.evidence[evidence_id]]}
                return page
            raise ValueError('Outline research can only read local sources, history, evidence, and task context')
        operation, source_args = self.prepare_source(tool, args)
        result = await _local(operation, **source_args)
        return self.commit_source(tool, source_args, result)

    def prepare_source(self, tool, args, *, reserved=0):
        """Prepare immutable reader arguments; reserve evidence before launching a batch."""
        args = dict(args)
        if args.pop("scope", "sources") != "sources":
            raise ValueError('Outline research only supports local sources')
        operation = {"fs.list": self.workspace.list, "fs.inspect": self.workspace.inspect_navigation,
                     "fs.read": self.workspace.read, "fs.search": self.workspace.search}[tool]
        if tool == "fs.read":
            # Read the selected chapter in full first. The batch's exact next
            # request decides whether pagination is needed, not a fixed cap.
            if self.limits.max_evidence_chars is not None:
                remaining = self.limits.max_evidence_chars - sum(
                    len(e["text"]) for e in self.evidence.values())
                if remaining <= 0:
                    raise WorkspaceError('The outline research source-text budget is exhausted. Preserve gaps and call finish; previously read text can be retrieved with scope=evidence')
                args["max_chars"] = min(args.get("max_chars", remaining), remaining)
        elif tool != "fs.list":
            maximum = min(self.limits.max_result_chars, self.result_budget)
            # Reserve the tiny file/range caption added to the visible tool text.
            args["max_chars"] = min(args.get("max_chars", maximum), max(2000, maximum - 160))
        return operation, args

    def commit_source(self, tool, args, result):
        """Assign aliases and merge source evidence in request order, never in workers."""
        if tool == "fs.read":
            selector = {key: value for key, value in args.items()
                        if key not in {"offset", "character_offset", "limit", "max_chars", "scope"} and value is not None}
            result["range_id"] = "range_" + digest(json.dumps(selector, sort_keys=True, ensure_ascii=False))
        if tool != "fs.read":
            self.fit_result(tool, args, result)
        self.register(tool, args, result)
        return result

    def record_tool(self, entry, result):
        """Persist the executed call and its ID before exposing it to the next model request."""
        entry = entry | {"result": result}
        if "error" in result:
            entry["model_text"] = 'Execution failed: ' + result["error"]
            self.event(entry["request_id"] + "-error.json", {
                "error": "tool_error", "message": result["error"],
                "tool": entry["tool"], "tool_call": entry.get("tool_call", {}),
            })
        else:
            entry["model_text"] = self.result_text(entry["tool"], entry["arguments"], result)
        self.history[entry["request_id"]] = result
        self.event(entry["request_id"] + "-tool.json", entry)
        self.observe(entry)

    def projected_context(self, initial, feedback, entries, results, runtime=None, schema=None, current_context=None):
        """Build the next request without evicting history or committing unseen text."""
        from copy import copy, deepcopy

        projected = copy(self)
        projected.presentation = copy(self.presentation)
        for name in ("files", "evidence", "ranges", "cursors"):
            setattr(projected.presentation, name, deepcopy(getattr(self.presentation, name)))
        projected.presentation.legacy_cursors = set(self.presentation.legacy_cursors)
        for name in ("files", "ranges", "structures", "node_titles", "evidence", "listed", "memory"):
            setattr(projected, name, deepcopy(getattr(self, name)))
        projected.observations = list(self.observations if current_context is None
                                      else current_context["observations"])
        projected.step_in_round += 1
        errors = []
        for entry, raw in zip(entries, results, strict=True):
            result = deepcopy(raw) if not isinstance(raw, BaseException) else {"error": str(raw)[:3000]}
            if "error" not in result:
                projected.register(entry["tool"], entry["arguments"], result)
            else:
                errors.append(result["error"])
            text = projected.result_text(entry["tool"], entry["arguments"], result)
            projected.observe(entry | {"result": result, "model_text": text})
        return projected.context(initial, errors, runtime, schema, preserve_history=True)

    async def fit_batch(self, entries, prepared, results, initial, feedback, runtime, schema, current_context=None):
        """Page only new reads, sharing the remaining capacity of their next request."""
        if current_context is not None:
            existing = self.projected_context(
                initial, feedback, [], [], runtime, schema, current_context)
            if not self.fits_context(existing, runtime, schema):
                # Notes supplied with this call can fill the existing context
                # before any new result is added. Apply the normal checkpoint
                # policy only to that already-overfull history, then preserve
                # its retained entries while fitting the new batch.
                current_context = self.context(initial, [], runtime, schema)
        selected = [index for index, entry in enumerate(entries)
                    if (entry["tool"] == "search.web" or entry["tool"] == "fs.read"
                        and (entry["arguments"].get("scope", "sources") == "sources"
                             or entry["arguments"].get("scope") == "history"
                             and entry["arguments"].get("request_id") in self.web_results))
                    and not isinstance(results[index], BaseException)
                    and "error" not in results[index]
                    and (results[index].get("evidence") or results[index].get("text"))]
        sizes = {index: (len(self.web_results[entries[index]["request_id"]]["model_text"])
                         if entries[index]["tool"] == "search.web" else
                         len(results[index]["text"]) if entries[index]["arguments"].get("scope") == "history" else
                         sum(len(item["text"]) for item in results[index]["evidence"]))
                 for index in selected}
        total = sum(sizes.values())
        remaining_evidence = (None if self.limits.max_evidence_chars is None else
                              max(0, self.limits.max_evidence_chars
                                  - sum(len(item["text"]) for item in self.evidence.values())))

        def fits(candidate):
            if remaining_evidence is not None and sum(
                len(item["text"]) for index in selected if entries[index]["tool"] == "fs.read"
                and entries[index]["arguments"].get("scope", "sources") == "sources"
                for item in candidate[index].get("evidence", [])
            ) > remaining_evidence:
                return False
            context = self.projected_context(
                initial, feedback, entries, candidate, runtime, schema, current_context)
            return self.fits_context(context, runtime, schema)

        if fits(results):
            return results
        if not selected:
            raise WorkspaceError('Remaining context cannot fit this tool-result batch. History is preserved; the results cannot be marked as delivered to the model')
        original = results

        async def page(budget):
            candidate = list(original)
            for index in selected:
                if budget < len(selected):
                    candidate[index] = {"error": 'The remaining context or source-text budget cannot fit this read. Organize existing sources first'}
                    continue
                maximum = max(1, sizes[index] * budget // total)
                if maximum >= sizes[index]:
                    continue
                if entries[index]["tool"] == "search.web":
                    candidate[index] = self.web_page(entries[index]["request_id"], maximum)
                    continue
                if entries[index]["arguments"].get("scope") == "history":
                    candidate[index] = self.history_page(original[index], maximum)
                    continue
                operation, args = prepared[index]
                candidate[index] = await _local(operation, **(args | {"max_chars": maximum}))
                candidate[index]["pagination_reason"] = (
                    "evidence_budget" if remaining_evidence is not None and total > remaining_evidence
                    else "context_capacity")
            return candidate

        # Binary search the shared body allowance. Every candidate includes all
        # call/result pairs, original history, tool definitions and output reserve.
        while True:
            low, high, best = len(selected), total, None
            while low <= high:
                middle = (low + high) // 2
                candidate = await page(middle)
                if fits(candidate):
                    best, low = candidate, middle + 1
                else:
                    high = middle - 1
            if best is not None:
                return best
            # A delivered web page can fill the old request exactly. Even the
            # continuation call's metadata then needs space. Only after that
            # proven overflow may already-delivered, droppable history leave
            # the window; original results and delivery receipts remain saved.
            retained = list(self.observations if current_context is None else current_context["observations"])
            removable = next((entry for entry in retained
                              if entry["request_id"] in self.delivered_observations
                              and self.memory.can_drop(entry["request_id"])), None)
            if removable is None:
                raise WorkspaceError('Remaining context cannot fit continuation positions for this source batch. History and unread ranges are preserved')
            retained.remove(removable)
            current_context = {"observations": retained}

    async def execute_batch(self, batch, request_id, *, initial=None, feedback=(), runtime=None, schema=None,
                            current_context=None):
        """Run independent source readers concurrently, then merge every result in call order."""
        entries, prepared, notes_added, reserved = [], [], 0, 0
        metadata = {"batch_id": request_id, "batch_tool_calls": [step.tool_call for step in batch.steps],
                    "assistant_content": batch.assistant_content,
                    **({"reasoning_content": batch.reasoning_content} if batch.reasoning_content is not None else {})}
        previous_evidence = set(self.evidence)
        previous_web = {item["evidence_id"] for item in self.web.report()["evidence"]} if self.web else set()
        previous_navigation = self.navigation_signature()
        previous_searches = {item["evidence_id"] for entry in self.observations
                             if entry["tool"] == "fs.search" for item in entry["result"].get("evidence", [])}
        previous_pages = {(entry["arguments"].get("scope"), entry["arguments"].get("request_id"),
                           entry["arguments"].get("evidence_id"), entry["arguments"].get("field"),
                           entry["result"].get("offset"), entry["result"].get("text"))
                          for entry in self.observations if entry["tool"] == "fs.read"
                          and entry["arguments"].get("scope", "sources") != "sources"
                          and "text" in entry["result"]}
        for index, step in enumerate(batch.steps):
            item_id = request_id if len(batch.steps) == 1 else f"{request_id}_tool_{index + 1:03d}"
            entry = metadata | {"request_id": item_id, "tool": step.tool,
                                "tool_call": step.tool_call, "arguments": step.arguments}
            entries.append(entry)
            try:
                notes_added += self.memory.apply(
                    step.research_notes, evidence=self.evidence,
                    resolve_evidence=self.presentation.resolve_evidence_id, call_id=item_id)
                if step.notes.strip():
                    self.notes = step.notes
                args = await _local(self.presentation.resolve_arguments, step.arguments, tool=step.tool)
                entry["arguments"] = args
                repeated = (self.memory.check_repeat(step.tool, args, step.review_reason)
                            if step.tool != "search.web" else None)
                if repeated:
                    raise ValueError(repeated)
                if step.tool.startswith("fs.") and args.get("scope", "sources") == "sources":
                    operation, source_args = self.prepare_source(step.tool, args, reserved=reserved)
                    prepared.append((operation, source_args))
                else:
                    prepared.append(None)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                prepared.append(exc)

        errors, progressed = [], False

        def commit(index, result, source_args=None):
            nonlocal progressed
            entry, step = entries[index], batch.steps[index]
            args = entry["arguments"]
            try:
                if isinstance(result, BaseException):
                    raise result
                if source_args is not None:
                    result = self.commit_source(step.tool, source_args, result)
                progressed |= bool(
                    set(self.evidence) - previous_evidence
                    or self.navigation_signature() != previous_navigation
                    or step.tool == "fs.search" and {
                        item["evidence_id"] for item in result.get("evidence", [])} - previous_searches
                    or step.tool == "search.web" and set(result.get("evidence_ids", [])) - previous_web
                    or step.tool == "fs.read" and args.get("scope", "sources") != "sources"
                    and bool(result.get("text"))
                    and (args.get("scope"), args.get("request_id"), args.get("evidence_id"), args.get("field"),
                         result.get("offset"), result.get("text")) not in previous_pages)
                self.record_tool(entry, result)
                self.waiting_message = result_message(step.tool, args, result, self.files, self.node_titles)
            except Exception as exc:
                message = str(exc)[:3000]
                errors.append(message)
                self.record_tool(entry, {"error": message})
                self.waiting_message = error_message(step.tool, args, self.files, exc)
            return self.waiting_message

        semaphore = asyncio.Semaphore(4)

        async def read(item):
            operation, args = item
            async with semaphore:
                return await _local(operation, **args)

        async def execute(index, item):
            if isinstance(item, tuple):
                return await read(item)
            if item is not None:
                return item
            step, args = batch.steps[index], entries[index]["arguments"]
            async with semaphore:
                return await self.dispatch(step.tool, args)

        if len(prepared) > 1:
            await self.notify(f"正在并行处理 {len(prepared)} 个资料读取任务，最多同时执行 4 个。")
            for step, entry in zip(batch.steps, entries, strict=True):
                if step.tool == "search.web":
                    await self.notify(action_message(step.tool, entry["arguments"], self.files,
                                                     self.node_titles, step.summary))
        else:
            await self.notify(action_message(batch.steps[0].tool, entries[0]["arguments"],
                                             self.files, self.node_titles, batch.steps[0].summary))
        pending = asyncio.gather(*(execute(index, item) for index, item in enumerate(prepared)),
                                 return_exceptions=True)
        cancelled = False
        try:
            results = await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
            results = await pending
        for entry, result in zip(entries, results, strict=True):
            if entry["tool"] == "search.web" and isinstance(result, dict) and "error" not in result:
                text = self.result_text(entry["tool"], entry["arguments"], result)
                self.web_results[entry["request_id"]] = entry | {"result": result, "model_text": text}
        if not cancelled and initial is not None:
            results = await self.fit_batch(
                entries, prepared, results, initial, feedback, runtime, schema, current_context)
        messages = [commit(index, result, prepared[index][1] if isinstance(prepared[index], tuple) else None)
                    for index, result in enumerate(results)]
        self.presentation.save()
        if cancelled:
            raise asyncio.CancelledError
        for message in messages:
            await self.notify(message)
        return progressed, notes_added, errors

    def fit_result(self, tool, args, result):
        """Bound the visible response, including citation labels and continuation."""
        maximum = min(self.limits.max_result_chars, self.result_budget)
        if tool in {"fs.read", "fs.search"} and args.get("scope", "sources") == "sources":
            if len(self.preview_result_text(tool, args, result)) <= maximum:
                return
            fitted = self.fit_read(args, result, lambda candidate: len(
                self.preview_result_text(tool, args, candidate)) <= maximum, tool=tool,
                pagination_reason="result_budget")
            result.clear()
            result.update(fitted)
            return
        key = "files" if tool == "fs.list" else "evidence"
        while len(self.presentation.result_text(tool, args, result)) > maximum:
            if key not in result or len(result[key]) <= 1:
                raise WorkspaceError('This result does not fit the remaining context. Narrow the reading range')
            result[key].pop()
            end = result["offset"] + len(result[key])
            result["next_offset"] = end if end < result["total"] else None
            result["complete"] = end >= result["total"]

    def preview_result_text(self, tool, args, result):
        from copy import copy

        presentation = copy(self.presentation)
        for name in ("files", "evidence", "ranges", "cursors"):
            setattr(presentation, name, dict(getattr(self.presentation, name)))
        presentation.legacy_cursors = set(self.presentation.legacy_cursors)
        return presentation.result_text(tool, args, result)

    def result_text(self, tool, args, result):
        if "error" in result:
            return 'Execution failed: ' + result["error"]
        if tool == "search.web" and self.web:
            if result.get("web_result_page"):
                text = result["text"]
                if not result["complete"]:
                    text += (f"\n\nPublic excerpt result continues: fs_read(scope='history', "
                             f"request_id='{result['original_request_id']}', offset={result['next_offset']}). "
                             "Unshown excerpts have not been read.")
                return text
            return self.web.format_result(result)
        return self.presentation.result_text(tool, args, result)

    def web_page(self, request_id, maximum):
        from folio.research.context_views import page_evidence_spans

        entry = self.web_results[request_id]
        result = entry["result"]
        page = _page(entry["model_text"], limit=maximum, original_request_id=request_id)
        return {key: result[key] for key in ("query", "purpose", "status", "cached", "evidence_ids")} | page | {
            "web_result_page": True,
            "source_count": len({item["source_id"] for item in result.get("evidence", [])}),
            "model_evidence_spans": page_evidence_spans(result.get("model_evidence_spans", []), 0, len(page["text"])),
        }

    @staticmethod
    def history_page(result, maximum):
        from folio.research.context_views import page_evidence_spans

        text = result["text"][:maximum]
        end = result["offset"] + len(text)
        return result | {"text": text, "next_offset": end if end < result["total"] else None,
                         "complete": end >= result["total"],
                         "model_evidence_spans": page_evidence_spans(
                             result.get("model_evidence_spans", []), 0, len(text))}

    def fit_read(self, args, result, fits, *, tool="fs.read", pagination_reason="context_capacity"):
        """Use the canonical reader so a sliced long block retains exact provenance."""
        arguments = {key: value for key, value in args.items() if key not in {"scope", "cursor"}}
        arguments.update(offset=result["offset"], character_offset=result.get("character_offset", 0))
        low, high = 1, sum(len(item["text"]) for item in result.get("evidence", []))
        best = None
        while low <= high:
            middle = (low + high) // 2
            operation = self.workspace.read if tool == "fs.read" else self.workspace.search
            candidate = operation(**(arguments | {"max_chars": middle}))
            candidate["pagination_reason"] = pagination_reason
            if result.get("range_id"):
                candidate["range_id"] = result["range_id"]
            if fits(candidate):
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        if best is None:
            raise WorkspaceError('Remaining context cannot fit source continuation positions. Unread original text cannot be marked complete')
        return best

    def delivered(self, context, request_id):
        """Record what a completed model request contained, separately from extraction."""
        self.delivered_listed.update(item["path"] for item in context["manifest"]["files"])
        record = {"request_id": request_id, "observations": [], "evidence_ids": [], "web_evidence_ids": []}
        for field in self.context_pages:
            preview = context[field]
            if isinstance(preview, dict) and "offset" in preview and "text" in preview:
                self.delivered_context_pages.setdefault(field, []).append(
                    (preview["offset"], preview["offset"] + len(preview["text"])))
        visited = set()

        def accept(entry):
            original_id = entry["request_id"]
            if original_id in visited:
                return
            visited.add(original_id)
            record["observations"].append(original_id)
            tool, args, result = entry["tool"], entry.get("arguments", {}), entry["result"]
            if "error" in result:
                self.delivered_observations.add(original_id)
                visited.remove(original_id)
                return
            original = next((item for item in self.observations if item["request_id"] == original_id), entry)
            if self.web:
                text = entry.get("model_text", "")
                for span in result.get("model_evidence_spans", []):
                    key, start, end = span.get("evidence_id"), span.get("start"), span.get("end")
                    if not isinstance(key, str) or not key.startswith("web_"):
                        continue
                    source = self.web.resolve_reference(key)
                    if (span.get("complete", True) and type(start) is int and type(end) is int
                            and 0 <= start < end <= len(text) and text[start:end] == source.text):
                        self.delivered_web_evidence.add(key)
                        record["web_evidence_ids"].append(key)
                if tool == "search.web" and result.get("web_result_page"):
                    self.delivered_text_pages.setdefault(("history", original_id), []).append(
                        (result["offset"], result["offset"] + len(result["text"])))
            self.memory.record_observation(original)
            if tool == "fs.read" and args.get("scope", "sources") == "sources":
                known = self.memory.dump()["batches"][original_id]["references"]
                additions = [self.workspace.resolve_evidence(raw).model_dump()
                             for raw in result.get("evidence", [])
                             if raw["evidence_id"] not in known]
                if additions:
                    # A context-sized view may contain canonical character
                    # slices absent from the original whole-block receipt.
                    self.memory.record_delivery_evidence(original_id, additions)
                    self.register(tool, args, result)
            self.memory.record_delivery(
                original_id, self.visible_evidence(entry),
                complete=entry.get("model_text", "") == original.get("model_text", ""),
            )
            if tool == "fs.read":
                ids = self.visible_evidence(entry)
                self.delivered_evidence.update(ids)
                record["evidence_ids"].extend(ids)
            if tool == "fs.list":
                self.delivered_listed.update(item["path"] for item in result["files"])
            if tool == "fs.inspect" and not args.get("node_id"):
                pages = self.delivered_structures.setdefault(args["path"], [])
                pages.append((result["offset"], result["offset"] + len(result["structure"])))
                self.files[args["path"]]["structure_delivered"] = _covered(pages, result["structure_total"])
            if tool == "fs.read" and args.get("scope", "sources") == "sources":
                state = self.ranges[result["range_id"]]
                ids = self.visible_evidence(entry)
                self.delivered_evidence.update(ids)
                record["evidence_ids"].extend(ids)
                state["delivered_evidence_ids"] = list(dict.fromkeys([*state["delivered_evidence_ids"], *ids]))
                if "read_segments" not in result:
                    state["delivered_pages"].append((result["offset"], result["offset"] + len(ids)))
                else:
                    for raw, segment in zip(result["evidence"], result["read_segments"], strict=True):
                        if (raw["evidence_id"] in ids and segment["character_offset"] == 0
                                and segment["character_end"] == segment["total_characters"]):
                            state["delivered_pages"].append((segment["offset"], segment["offset"] + 1))
            elif tool == "fs.read" and args.get("scope") in {"history", "evidence", "context"}:
                scope = args["scope"]
                target = args.get("request_id") if scope == "history" else (
                    args.get("evidence_id") if scope == "evidence" else args.get("field"))
                if target:
                    pages = self.delivered_text_pages.setdefault((scope, target), [])
                    pages.append((result["offset"], result["offset"] + len(result["text"])))
                    if scope == "context":
                        self.delivered_context_pages.setdefault(target, []).append(pages[-1])
                    elif _covered(pages, result["total"]):
                        if scope == "history":
                            original = self.web_results.get(target) or next(
                                (item for item in self.observations if item["request_id"] == target), None)
                            if original is not None:
                                accept(original)
                        elif target in self.evidence:
                            self.delivered_evidence.add(target)
                            record["evidence_ids"].append(target)
            self.delivered_observations.add(original_id)
            visited.remove(original_id)

        for entry in context["observations"]:
            accept(entry)
        for state in self.ranges.values():
            state["delivered_evidence_ids"] = [key for key in state["evidence_ids"] if key in self.delivered_evidence]
            if "evidence_segments" in state:
                coverage = {"pages": [], "total": state["total"]}
                _register_read_segments(coverage, {"read_segments": [
                    segment for key, segment in state["evidence_segments"].items()
                    if key in self.delivered_evidence
                ]})
                state["delivered_pages"] = coverage["pages"]
                state["delivered_complete"] = coverage["complete"] and bool(state["delivered_evidence_ids"])
            else:
                state["delivered_complete"] = (state["complete"] and bool(state["evidence_ids"])
                                                 and set(state["evidence_ids"]) <= self.delivered_evidence)
        for path, file in self.files.items():
            file["delivered_characters"] = sum(len(self.evidence[key]["text"]) for key in self.delivered_evidence
                                                if self.evidence[key]["source_id"] == path)
        record["observations"] = list(dict.fromkeys(record["observations"]))
        record["evidence_ids"] = list(dict.fromkeys(record["evidence_ids"]))
        record["web_evidence_ids"] = list(dict.fromkeys(record["web_evidence_ids"]))
        self.deliveries.append(record)
        self.event(request_id + "-delivery.json", record)

    def validation(self, *, include_source_gaps=True):
        conclusion = self.conclusion
        verified = self.conclusion_sources()
        issues = []
        for field in self.context_pages:
            total = len(self.context_text[field])
            pages = self.delivered_context_pages.get(field, [])
            if not _covered(pages, total):
                issues.append(f'The complete task context has not been fully paged through: {field}')
        excluded = {item.path: item.reason for item in conclusion.excluded_files}
        if len(excluded) != len(conclusion.excluded_files):
            issues.append('The excluded-files list contains duplicate paths')
        for path in excluded:
            if path not in self.files or path not in self.delivered_listed:
                issues.append(f'An excluded file was not in the browsed Manifest: {path}')
        selected = [self.ranges[item.range_id] for item in conclusion.selected_ranges]
        if not selected:
            issues.append('No outline requirements supported by read key source text have been submitted')
        selected_paths = {item["path"] for item in selected}
        assessments = {item.path: item for item in conclusion.unread_assessments}
        if len(assessments) != len(conclusion.unread_assessments):
            issues.append('Unread-content impact assessments contain duplicate file paths')
        for path, assessment in assessments.items():
            if path not in selected_paths:
                issues.append(f'An unread-content impact assessment has no matching selected range: {path}')
            elif assessment.blocks_directory and include_source_gaps:
                issues.append(f'Unread content affects the outline scope: {path}; {assessment.reason}')
        for path, file in self.files.items():
            if path in selected_paths:
                if path not in self.delivered_listed:
                    issues.append(f'The selected source was not browsed through the Manifest: {path}')
                if path in excluded:
                    issues.append(f'A source cannot be both supporting evidence and excluded: {path}')
                if _has_unread_content(file) and path not in assessments:
                    warnings = "；".join(file["warnings"]) or 'The source contains incompletely extracted content'
                    issues.append(f'The effect of a source-reading completeness warning on the outline has not been assessed: {path}; {warnings}. Submit unread_assessments and explain their relationship to the actual selected range')
            elif path not in excluded:
                issues.append(f'The file has not been selected or given an exclusion reason: {path}')
        if not conclusion.requirements:
            issues.append('No outline requirements supported by original text have been submitted')
        ids = [item.requirement_id for item in conclusion.requirements]
        if len(set(ids)) != len(ids):
            issues.append('Duplicate outline requirement_id')
        for item in conclusion.requirements:
            invalid = set(item.evidence_ids) - self.evidence.keys()
            if invalid:
                issues.append(f"Requirement {item.requirement_id} cites original text without an actual reading record: {', '.join(sorted(invalid))}")
            unseen = set(item.evidence_ids) - self.delivered_evidence - invalid
            if unseen:
                issues.append(f"Citations for requirement {item.requirement_id} have not been delivered to the model: {', '.join(sorted(unseen))}")
            mismatched = set(item.evidence_ids) - verified - invalid - unseen
            if mismatched:
                issues.append(f"The source version or location cited by requirement {item.requirement_id} does not match the read original text: {', '.join(sorted(mismatched))}")
        return issues

    def report(self, status=None):
        issues = self.validation() if self.files else []
        issues.extend(self.issues)
        if self.reason:
            issues.append(self.reason)
        issues = list(dict.fromkeys(issues))
        conclusion = self.conclusion.model_dump()
        web_findings = conclusion.pop("web_findings")
        web_report = (self.web.report(web_findings) | {"delivered_evidence_ids": sorted(self.delivered_web_evidence)}
                      if self.web else {"available": False, "searches": [], "evidence": [], "findings": []})
        findings = self.working_findings()
        if not conclusion["summary"] and findings:
            conclusion["summary"] = ('Research is incomplete. The following saved interim findings are not complete outline requirements:\n'
                                     + "\n".join(f"- {item['topic']}：{item['text']}" for item in findings))[:6000]
        selected_paths = {self.ranges[item.range_id]["path"] for item in self.conclusion.selected_ranges
                          if item.range_id in self.ranges}
        limitations = []
        for assessment in self.conclusion.unread_assessments:
            file = self.files.get(assessment.path)
            if (file and assessment.path in selected_paths and _has_unread_content(file)
                    and not assessment.blocks_directory):
                warnings = "；".join(file["warnings"]) or 'The source contains incompletely extracted content'
                limitations.append(f'Source reading limitation ({assessment.path}): {warnings}; the research model considers it nonblocking for the outline for now: {assessment.reason}. This assessment does not verify unread content.')
        conclusion["unresolved"] = list(dict.fromkeys([*conclusion["unresolved"], *limitations]))
        # Never forward fabricated references to planning, even in a partial draft.
        verified = self.conclusion_sources()
        valid_requirements = [item for item in conclusion["requirements"]
                              if set(item["evidence_ids"]) <= verified]
        remaining_actions = [f'Local verification still needed: {action}' for action in conclusion["local_research_actions"] or []]
        actual_status = status or ("no_sources" if not self.files else
                                  "partial" if issues or conclusion["blocking_gaps"] or remaining_actions else "ready")
        return {**conclusion, "requirements": valid_requirements,
                "web_research": web_report,
                "status": actual_status, "validation_issues": issues,
                "termination_reason": self.termination_reason, "failure": self.failure,
                "conclusion_submitted": self.conclusion_submitted,
                "conclusion_validated": self.conclusion_submitted and not self.validation(),
                "finalization_attempted": self.finalization_attempted,
                "working_findings": findings,
                "blocking_gaps": list(dict.fromkeys([*conclusion["blocking_gaps"], *remaining_actions, *issues])),
                "files": list(self.files.values()), "evidence": list(self.evidence.values()),
                "source_snapshot_id": self.workspace.source_snapshot_id,
                "calls": self.calls, "tool_calls": self.tool_calls, "rounds": self.rounds, "usage": self.usage,
                "elapsed_seconds": round(monotonic() - self.started, 3),
                "model_deliveries": self.deliveries,
                "research_memory": self.memory.dump(),
                "limits": self.limits.model_dump(),
                **({"resume": self.replay.summary()} if self.replay is not None else {})}

    def checkpoint(self, status=None):
        value = self.report(status)
        self.save("report.json", value)
        WorkspaceManager._atomic_text(
            self.output / "research.md", self.memory.markdown(evidence_label=self.presentation.evidence_id))
        return value

    def fits_context(self, value, runtime=None, schema=None):
        if runtime is not None:
            capacity = getattr(runtime, "capacity", None)
            local_measure = getattr(capacity, "local_measure", None)
            if local_measure is not None:
                return local_measure("directory_research", value, schema, self.limits.max_context_chars)["fits"]
            fits = getattr(runtime, "fits_prompt_budget", None)
            if fits is not None:
                return fits("directory_research", value, schema, self.limits.max_context_chars)
            from folio.runtime.base import prompt_chars
            return prompt_chars(runtime, "directory_research", value, schema) <= self.limits.max_context_chars
        from folio.research.transport import research_prompt_chars
        return research_prompt_chars(value, schema or PlanningResearchBatch.model_json_schema()) <= self.limits.max_context_chars

    def context(self, initial, feedback, runtime=None, schema=None, *, force_checkpoint=False, finalizing=False,
                preserve_history=False):
        result = {
            "research_session_id": digest(str(self.output.resolve())),
            "requirements": self.context_sources["requirements"],
            "template": self.context_sources["template"],
            "manifest": initial,
            "budget": {"round": self.rounds, "max_rounds": self.limits.max_rounds,
                       "calls_remaining": self.limits.max_calls - self.calls,
                       "tool_calls_remaining": self.limits.max_tool_calls - self.tool_calls,
                       "seconds_remaining": max(0, round(self.limits.max_seconds - (monotonic() - self.started))),
                       "max_steps_per_round": self.limits.max_steps_per_round,
                       "steps_remaining_in_round": self.limits.max_steps_per_round - self.step_in_round},
            "notes": self.notes, "feedback": feedback,
            "research_notes": self.memory.markdown(evidence_label=self.presentation.evidence_id),
            "read_progress": [{key: item[key] for key in ("range_id", "selector", "complete", "total")}
                              for item in self.ranges.values()],
            "observations": [],
            "manifest_text": self.presentation.catalog_text(initial),
            "read_progress_text": self.presentation.progress_text(self.ranges.values()),
        }
        if self.output_language:
            result["ui_language"] = self.output_language
        result["web_search_available"] = bool(self.web and self.web.available)
        if self.web:
            web_report = self.web.report()
            result["web_research"] = {
                "searches": web_report["searches"],
                "sources": [{"reference": item["metadata"]["reference"],
                             "title": item["metadata"].get("title", ""), "url": item["source_id"]}
                            for item in web_report["evidence"]],
                "findings": web_report["findings"],
                "history": [{"request_id": key, "query": entry["result"]["query"]}
                            for key, entry in self.web_results.items()],
            }
        if finalizing:
            result["research_finalizing"] = True
            result["feedback"] = [*feedback, "Further reading has stopped. Now call only finish to consolidate originals actually read and saved research notes. "
                                  "Submit outline requirements citing E source evidence and remaining gaps. Do not claim unread material was read, "
                                  "or invent exclusion reasons to pass validation."]
        # Long inputs stay available verbatim through a paged, read-only tool.
        for field in ("requirements", "template"):
            if runtime is None and len(self.context_text[field]) > self.limits.max_context_chars:
                result[field] = _page(self.context_text[field], limit=self.limits.max_context_chars,
                                      note=f"Preview truncated; read complete content with fs_read(scope='context',field='{field}')")
                pages = self.context_pages.setdefault(field, [])
                visible = (0, len(result[field]["text"]))
                if visible not in pages:
                    pages.append(visible)
        for item in self.observations:
            self.memory.record_observation(item)
        result["history"] = {"total": len(self.observations), "shown": 0}
        result["history_text"] = self.memory.history_text(evidence_label=self.presentation.evidence_id)

        def fits(value):
            return self.fits_context(value, runtime, schema)

        # Keep the complete call/result sequence while it fits. Summarized or
        # navigational results become removable only when capacity requires it.
        self.result_budget = self.limits.max_result_chars
        retained = list(self.observations)
        checkpoint = not finalizing and force_checkpoint
        candidate = result | {"observations": retained}
        if preserve_history:
            candidate["history"]["shown"] = len(retained)
            return candidate
        if not checkpoint and fits(candidate):
            result = candidate
        else:
            for item in list(retained):
                if item["request_id"] in self.delivered_observations and self.memory.can_drop(item["request_id"]):
                    retained.remove(item)
                    candidate = result | {"observations": retained}
                    if not checkpoint and fits(candidate):
                        result = candidate
                        break
            else:
                candidate = None
            if candidate is None or checkpoint:
                required = [self.pending_view(item) for item in retained
                            if item["request_id"] not in self.delivered_observations
                            or not self.memory.can_drop(item["request_id"])]
                pending = set(self.memory.pending_requests())
                choices = [item for item in required if item["request_id"] in pending]
                # Navigation/errors cannot satisfy a source-backed save schema.
                if required and not choices:
                    result |= {"observations": [self.fit_latest(required[-1], result, fits)]}
                elif choices:
                    base = result | {
                        "memory_checkpoint_required": True,
                        "memory_checkpoint_message": (
                            "Summarize the source batch supplied here and call research_save. Other unsummarized batches remain in history; "
                            "they were not deleted and research is not complete. Continue after saving."
                        ),
                    }
                    for item in choices:
                        selected = base | {"observations": [item], "memory_checkpoint_request_ids": [item["request_id"]]}
                        if fits(selected):
                            result = selected
                            break
                    else:
                        # A partial original must remain readable; it cannot
                        # be marked summarized before its unseen tail arrives.
                        base = result | {
                            "memory_checkpoint_message": (
                                "Only part of this source batch can be shown. Summarize passages actually received, then use continuation to read the rest. "
                                "Unshown content must not be recorded as read or summarized."
                            ),
                        }
                        newest = [item for item in required if item["request_id"] not in self.delivered_observations]
                        item = self.fit_latest(newest[-1] if newest else choices[0], base, fits)
                        result = base | {"observations": [item]}
        result["history"]["shown"] = len(result["observations"])
        self.presentation.save()
        return result

    def fit_latest(self, entry, base, fits):
        """Keep the latest result; shrink only by readable units with a next page."""
        from copy import deepcopy

        entry = deepcopy(entry)
        result = entry["result"]
        web_search = entry["tool"] == "search.web" and entry["request_id"] in self.web_results
        web_history = (entry["tool"] == "fs.read" and entry.get("arguments", {}).get("scope") == "history"
                       and entry["arguments"].get("request_id") in self.web_results and "text" in result)
        if web_search or web_history:
            if fits(base | {"observations": [entry]}):
                return entry
            low, high, best = 1, (len(self.web_results[entry["request_id"]]["model_text"])
                                  if web_search else len(result["text"])), None
            while low <= high:
                middle = (low + high) // 2
                page = (self.web_page(entry["request_id"], middle) if web_search else
                        self.history_page(result, middle))
                candidate = entry | {"result": page, "model_text": self.result_text(entry["tool"], entry["arguments"], page)}
                if fits(base | {"observations": [candidate]}):
                    best, low = candidate, middle + 1
                else:
                    high = middle - 1
            if best is None:
                raise WorkspaceError('Remaining context cannot fit web-result continuation positions. Results are saved')
            return best
        if (entry["tool"] in {"fs.read", "fs.search"}
                and entry.get("arguments", {}).get("scope", "sources") == "sources"
                and not fits(base | {"observations": [entry]})):
            def fits_read(candidate):
                text = self.preview_result_text(entry["tool"], entry.get("arguments", {}), candidate)
                return fits(base | {"observations": [entry | {"result": candidate, "model_text": text}]})

            entry["result"] = self.fit_read(
                entry.get("arguments", {}), result, fits_read, tool=entry["tool"])
            entry["model_text"] = self.presentation.result_text(
                entry["tool"], entry.get("arguments", {}), entry["result"])
            return entry
        while not fits(base | {"observations": [entry]}):
            if "evidence" in result and len(result["evidence"]) > 1:
                result["evidence"].pop()
                end = result["offset"] + len(result["evidence"])
                result.update(next_offset=end if end < result["total"] else None,
                              complete=end >= result["total"])
            elif entry["tool"] == "fs.inspect" and len(result.get("structure", [])) > 1:
                args = {key: value for key, value in entry.get("arguments", {}).items()
                        if key not in {"max_chars", "limit", "scope"}}
                result = self.workspace.inspect_navigation(**args, limit=min(100, len(result["structure"]) - 1),
                                                           max_chars=self.limits.max_result_chars)
                entry["result"] = result
            else:
                # Explicitly stop, instead of silently issuing a request with no tool result.
                raise WorkspaceError('Remaining context cannot fit the latest tool result. Records are preserved; it cannot be marked as delivered to the model')
            entry["model_text"] = self.presentation.result_text(entry["tool"], entry.get("arguments", {}), result)
        return entry


async def _finalize_research(state, runtime, initial, schema):
    """One finish-only request within the original budget; always retain partial work."""
    from folio.research.native_tools import native_result, tool_definitions
    from folio.research.transport import research_messages
    from folio.runtime.base import prompt_chars

    state.checkpoint("partial")
    remaining = state.limits.max_seconds - (monotonic() - state.started)
    if (state.finalization_attempted or not state.evidence
            or state.conclusion_submitted and not state.validation()
            or state.calls >= state.limits.max_calls or state.tool_calls >= state.limits.max_tool_calls
            or remaining <= 0):
        return state.checkpoint()
    state.finalization_attempted = True
    request_id = None
    try:
        context = state.context(initial, [state.reason] if state.reason else [], runtime, schema, finalizing=True)
        if not state.fits_context(context, runtime, schema):
            return state.checkpoint()
        capacity = None
        if hasattr(runtime, "measure"):
            capacity = await asyncio.wait_for(
                runtime.measure("directory_research", context, schema, state.limits.max_context_chars),
                timeout=min(state.limits.call_timeout_seconds, remaining))
            if not capacity["fits"]:
                return state.checkpoint()
        remaining = state.limits.max_seconds - (monotonic() - state.started)
        if remaining <= 0:
            return state.checkpoint()
        state.calls += 1
        request_id = f"research_{state.calls:03d}"
        parts = getattr(runtime, "request_parts", None)
        messages, wire_schema = parts("directory_research", context, schema)[:2] if parts else (
            research_messages(context, schema), schema)
        state.event(request_id + "-request.json", {
            "task": "directory_research", "phase": "finalization", "context": context,
            "internal_result_schema": wire_schema, "messages": messages, "round": state.rounds,
            "tools": tool_definitions("directory_research", context, schema),
            "tool_choice": "required", "parallel_tool_calls": True, "capacity": capacity,
            "messages_source": "runtime" if parts else "reference_renderer",
            "prompt_characters": prompt_chars(runtime, "directory_research", context, schema),
        })
        WorkspaceManager._atomic_text(state.events / (request_id + "-messages.txt"), "\n\n".join(
            message["role"].upper() + "\n" + (_json(message) if message.get("tool_calls")
            or message.get("tool_call_id") else str(message.get("content", ""))) for message in messages) + "\n")
        await state.notify("正在整理已读资料和研究笔记，提交已确认的目录依据，并保留未完成事项。")
        remaining = state.limits.max_seconds - (monotonic() - state.started)
        if remaining <= 0:
            state.calls -= 1
            state.event(request_id + "-skipped.json", {"reason": "time_limit", "request_sent": False})
            return state.checkpoint()
        result = await asyncio.wait_for(runtime.complete(
            "directory_research", context, schema,
            "directory_research:" + digest(str(state.output.resolve())) + ":" + request_id,
        ), timeout=min(state.limits.call_timeout_seconds, remaining))
        state.delivered(context, request_id)
        state.usage.append({"request_id": request_id, "usage": result.usage})
        state.event(request_id + "-response.json", {"content": result.content, "tool_calls": result.tool_calls,
                                                   "reasoning_content": result.reasoning_content,
                                                   "usage": result.usage, "stop_reason": result.stop_reason})
        if result.stop_reason != "completed":
            raise ValueError('Research wrap-up output did not complete')
        batch = PlanningResearchBatch.model_validate(native_result("directory_research", result, context, schema))
        if len(batch.steps) != 1 or batch.steps[0].tool != "finish":
            raise ValueError('Research wrap-up must call finish alone')
        state.tool_calls += 1
        step = batch.steps[0]
        conclusion = state.parse_conclusion(step.arguments)
        state.memory.apply(step.research_notes, evidence=state.evidence,
                           resolve_evidence=state.presentation.resolve_evidence_id, call_id=request_id)
        state.conclusion = conclusion
        state.conclusion_submitted = True
        if step.notes.strip():
            state.notes = step.notes
        if not state.validation() and not state.conclusion.blocking_gaps:
            state.conclude_notes(state.report(), request_id)
            state.reason = None
            if state.failure:
                state.failure["recovered"] = True
        report = state.report()
        assessment = {key: report[key] for key in ("status", "blocking_gaps", "unresolved")}
        state.event(request_id + "-assessment.json", assessment)
        state.record_tool({"request_id": request_id, "batch_id": request_id,
                           "batch_tool_calls": result.tool_calls, "assistant_content": result.content,
                           **({"reasoning_content": result.reasoning_content} if result.reasoning_content is not None else {}),
                           "tool": "finish", "tool_call": step.tool_call, "arguments": step.arguments}, assessment)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Closing is a single bounded attempt, not another research/retry loop.
        failure = {"code": "finalization_error", "message": str(exc)[:1000], "request_id": request_id}
        state.issues.append('Research wrap-up is incomplete. Valid notes and original evidence are preserved for outline planning.')
        if state.failure:
            state.failure["finalization"] = failure
        else:
            state.failure = failure
        if request_id:
            state.event(request_id + "-error.json", failure)
    return state.checkpoint()


async def research_for_directory(
    runtime, sources: Path, output: Path, requirements: str, *, cache_root=None,
    template=None, on_progress=None, limits=None, previous=None, replay=None,
    output_language=None,
) -> dict:
    """Research original inputs before planning; never fabricate a job or engine.

    ``ready`` means sufficient source research for a directory, not that every
    eventual writing detail is known. ``previous`` is audit context only: changed
    requirements must trigger a new selection and cannot inherit trusted readiness.
    ``replay`` is the research folder of an interrupted run with the same inputs;
    its model responses are reused only for requests identical to those it sent.
    """
    from folio.research.native_tools import native_result, tool_definitions
    from folio.runtime.base import prompt_chars
    from folio.runtime.diagnostics import safe_failure_diagnostics
    from folio.runtime.errors import ModelCallFailed
    from folio.runtime.tool_calls import native_retry_context

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    settings = _limits(limits)
    if on_progress:
        notification = on_progress("正在整理上传资料的文件清单和内部结构，准备选择需要阅读的内容。")
        if inspect.isawaitable(notification):
            await notification
    workspace = await _local(WorkspaceManager, Path(sources), output / "workspace",
                             cache_root=cache_root)
    if hasattr(template, "planning_context"):
        template = template.planning_context()
    if replay is not None:
        replay = replay if isinstance(replay, DirectoryResearchReplay) else DirectoryResearchReplay(replay)
        replay.check(requirements, template, workspace.source_snapshot_id)
    web = DirectoryWebResearch(runtime, output)
    if replay is not None:
        web.replay = replay
    state = _DirectoryResearch(
        workspace, output, requirements, template, settings, on_progress,
        web=web, output_language=output_language,
    )
    state.replay = replay
    state.save("input.json", {"requirements": requirements, "template": template,
                              "source_snapshot_id": workspace.source_snapshot_id,
                              "previous_snapshot_id": (previous or {}).get("source_snapshot_id"),
                              "limits": settings.model_dump(),
                              **({"resumed_from": str(replay.root)} if replay is not None else {})})
    initial = workspace.list(limit=20)
    state.register("fs.list", {}, initial)
    state.event("000-manifest.json", initial)
    if not state.files:
        state.conclusion.summary = 'No ordinary sources are available. The outline can only be based on user requirements and selected template constraints.'
        await state.notify("没有需要研究的普通资料，将依据你的编制要求和已选择的模板安排目录。")
        return state.checkpoint()
    schema = PlanningResearchBatch.model_json_schema()
    feedback = []
    retry_context = {}
    state.waiting_message = f"正在分析 {len(state.files)} 份资料的文件清单，选择与本次文档有关的资料。"
    if replay is not None:
        await state.notify(f"正在恢复上次中断的资料研究：复用已保存的 {len(replay.steps)} 次模型结果，"
                           "重新执行本地读取，不重新调用模型。", force=True)
    try:
        for round_number in range(1, settings.max_rounds + 1):
            state.rounds = round_number
            for step_index in range(settings.max_steps_per_round):
                state.step_in_round = step_index
                remaining = settings.max_seconds - (monotonic() - state.started)
                # Reserve one call and some existing wall time to hand off work;
                # neither reserve increases the original hard budgets.
                reserve = bool(state.delivered_evidence) and settings.max_calls >= 6
                call_limit = settings.max_calls - int(reserve)
                tool_limit = settings.max_tool_calls - int(reserve)
                closing_seconds = min(60, settings.max_seconds / 10) if reserve else 0
                if (state.calls >= call_limit or state.tool_calls >= tool_limit
                        or remaining <= closing_seconds):
                    code = "time_limit" if remaining <= closing_seconds else "call_limit"
                    state.stop(code, 'Source reading reached its call or time budget. Organizing existing research and unfinished items')
                    await state.notify("本次资料研究已达到调用或时间上限，正在保存已有成果和未完成事项。")
                    return await _finalize_research(state, runtime, initial, schema)
                try:
                    context = state.context(initial, feedback, runtime, schema) | retry_context
                except WorkspaceError as exc:
                    state.stop("context_limit", str(exc))
                    await state.notify("剩余上下文无法容纳最新读取结果，已保留记录和未读范围，本次研究暂停收尾。")
                    return await _finalize_research(state, runtime, initial, schema)
                if not state.fits_context(context, runtime, schema):
                    state.stop("context_limit", 'Outline research context exceeds the budget. Key requirements cannot be silently discarded while claiming research is complete')
                    await state.notify("本次研究内容已超过可处理的长度，已读资料会保留；仍需核清的要求将标记为资料缺口。")
                    return await _finalize_research(state, runtime, initial, schema)
                capacity = None
                measure = getattr(runtime, "measure", None)
                if measure is not None:
                    async def measured(value, measure=measure):
                        available = settings.max_seconds - (monotonic() - state.started)
                        return await asyncio.wait_for(
                            measure("directory_research", value, schema, settings.max_context_chars),
                            timeout=max(0, min(settings.call_timeout_seconds, available)))

                    try:
                        capacity = await measured(context)
                        if not capacity["fits"] and context.get("observations"):
                            context = state.context(initial, feedback, runtime, schema, force_checkpoint=True) | retry_context
                            capacity = await measured(context)
                    except TimeoutError:
                        state.stop("model_timeout", 'Outline research capacity checking timed out. No oversized request was sent and no source text awaiting synthesis was discarded')
                        await state.notify("本次模型容量检查等待超时，已保存研究笔记和未完成的阅读范围。")
                        return state.checkpoint()
                    if not capacity["fits"]:
                        state.stop("context_limit", 'The outline research request exceeds actual model capacity. Source text awaiting synthesis was not discarded or falsely marked as read')
                        await state.notify("本次原文归纳所需上下文已超过模型容量，已保存笔记、原文位置和待核实问题。")
                        return await _finalize_research(state, runtime, initial, schema)
                remaining = settings.max_seconds - (monotonic() - state.started)
                if remaining <= 0:
                    state.stop("time_limit", 'The outline research time budget is exhausted. No further model requests were sent')
                    return state.checkpoint()
                state.calls += 1
                request_id = f"research_{state.calls:03d}"
                from folio.research.transport import research_messages

                # The exact visible payload is recorded independently of the canonical audit ledger.
                parts = getattr(runtime, "request_parts", None)
                if parts:
                    messages, wire_schema, *_ = parts("directory_research", context, schema)
                else:
                    messages, wire_schema = research_messages(context, schema), schema
                state.event(request_id + "-request.json", {"task": "directory_research", "context": context,
                            "internal_result_schema": wire_schema, "messages": messages, "round": round_number,
                            "tools": tool_definitions("directory_research", context, schema),
                            "tool_choice": "required", "parallel_tool_calls": True,
                            "capacity": capacity,
                            "messages_source": "runtime" if parts else "reference_renderer",
                            "prompt_characters": prompt_chars(runtime, "directory_research", context, schema)})
                (state.events / (request_id + "-messages.txt")).write_text(
                    "\n\n".join(message["role"].upper() + "\n" + (
                        _json(message) if message.get("tool_calls") or message.get("tool_call_id")
                        else str(message.get("content", "")))
                                for message in messages) + "\n", encoding="utf-8")
                step = None
                args = {}
                raw_call = {}
                raw_calls = []
                batch_metadata = {}
                notes_added = 0
                progressed = False
                result = None
                try:
                    result = replay.take(request_id, context) if replay is not None else None
                    replayed = result is not None
                    if replay is not None and replay.continued_at == request_id:
                        await state.notify(
                            f"已恢复上次 {replay.replayed} 次模型调用的研究进度，从第 {state.calls} 次研究请求继续。"
                            if replay.replayed else "上次的研究记录与当前状态不一致，无法复用，已重新开始研究。",
                            force=True)
                    await state.notify(state.waiting_message)
                    try:
                        if not replayed:
                            result = await asyncio.wait_for(runtime.complete(
                                "directory_research", context, schema,
                                "directory_research:" + digest(str(output.resolve())) + ":" + request_id,
                            ), timeout=min(settings.call_timeout_seconds, remaining))
                    except Exception as exc:
                        from folio.runtime.diagnostics import (
                            partial_output_for_audit,
                            safe_failure_diagnostics,
                        )

                        # Keep model failures linked to the exact request even
                        # when they abort research before a result is returned.
                        diagnostics = safe_failure_diagnostics(exc) | {"research_request_id": request_id}
                        exc.diagnostics = diagnostics
                        try:
                            state.event(request_id + "-error.json", {"error": type(exc).__name__,
                                                                   "diagnostics": diagnostics})
                            partial = partial_output_for_audit(exc)
                            if partial is not None:
                                (state.output / "failed_outputs").mkdir(exist_ok=True)
                                state.save("failed_outputs/" + request_id + ".json", {
                                    "task": "directory_research", "request_id": request_id,
                                    "request_file": "events/" + request_id + "-request.json",
                                    "diagnostics": diagnostics, "partial_output": partial,
                                })
                        except OSError:
                            # A diagnostic write failure must not turn the model
                            # failure into an ordinary, retryable tool error.
                            logging.getLogger(__name__).warning(
                                "directory_research_diagnostics_write_failed request_id=%s", request_id)
                        raise
                    state.delivered(context, request_id)
                    state.usage.append({"request_id": request_id, "usage": result.usage}
                                       | ({"replayed": True} if replayed else {}))
                    state.event(request_id + "-response.json", {"content": result.content,
                                                               "tool_calls": result.tool_calls,
                                                               "reasoning_content": result.reasoning_content,
                                                               "usage": result.usage,
                                                               "stop_reason": result.stop_reason,
                                                               **({"replayed_from": str(replay.root)}
                                                                  if replayed else {})})
                    if result.stop_reason != "completed":
                        raise ValueError('Model output did not complete')
                    for candidate in result.tool_calls:
                        if (isinstance(candidate, dict) and isinstance(candidate.get("id"), str)
                                and candidate.get("id") and candidate.get("type") == "function"
                                and isinstance(candidate.get("function"), dict)
                                and isinstance(candidate["function"].get("name"), str)
                                and isinstance(candidate["function"].get("arguments"), str)):
                            raw_calls.append(candidate)
                    raw_call = raw_calls[0] if len(raw_calls) == 1 else {}
                    batch_metadata = {"batch_id": request_id, "batch_tool_calls": raw_calls,
                                      "assistant_content": result.content,
                                      **({"reasoning_content": result.reasoning_content} if result.reasoning_content is not None else {})}
                    batch = PlanningResearchBatch.model_validate(
                        native_result("directory_research", result, context, schema))
                    retry_context = {}
                    if len(batch.steps) > settings.max_tool_calls - state.tool_calls:
                        raise ValueError('This batch exceeds the remaining tool-call budget. Reduce the call count and retry')
                    state.tool_calls += len(batch.steps)
                    if any(item.tool == "finish" for item in batch.steps) and len(batch.steps) != 1:
                        raise ValueError('finish must be called alone. No tools in this batch were executed; obtain reading results before completing research')
                    if len(batch.steps) == 1 and batch.steps[0].tool == "finish":
                        step = batch.steps[0]
                        args = step.arguments
                        notes_added = state.memory.apply(
                            step.research_notes, evidence=state.evidence,
                            resolve_evidence=state.presentation.resolve_evidence_id, call_id=request_id)
                        if step.notes.strip():
                            state.notes = step.notes
                        await state.notify("正在核对资料研究结果：检查目录要求是否引用了已读原文、资料来源是否一致。")
                        state.accept_conclusion(args)
                        state.conclusion_submitted = True
                        state.issues = []
                        if not state.validation() and not state.conclusion.blocking_gaps:
                            notes_added += state.conclude_notes(state.report(), request_id)
                        report = state.checkpoint()
                        state.event(request_id + "-assessment.json", {
                            "status": report["status"], "blocking_gaps": report["blocking_gaps"],
                            "unresolved": report["unresolved"], "selected_ranges": report["selected_ranges"],
                        })
                        assessment = {"status": report["status"], "blocking_gaps": report["blocking_gaps"],
                                      "unresolved": report["unresolved"]}
                        state.history[request_id] = assessment
                        state.observe(batch_metadata | {"request_id": request_id, "tool": "finish",
                                                   "tool_call": step.tool_call, "arguments": args,
                                                   "result": assessment, "model_text": _json(assessment)})
                        if report["status"] == "ready":
                            state.attempt(request_id, True)
                            await state.notify(f"目录资料研究完成：实际读取 {sum(f['read'] for f in state.files.values())} 份资料，提取 {len(report['requirements'])} 项有依据的目录要求。")
                            return state.checkpoint()
                        if (state.conclusion.local_research_actions == []
                                and not state.validation(include_source_gaps=False)):
                            # A valid finish is the end of available local research.
                            # Unavailable source content or unresolved scope choices
                            # remain explicit blockers, never another blind read loop.
                            state.attempt(request_id, True)
                            await state.notify("已保存资料研究结论；仍需补充资料或确认目录范围，现有依据和待确认项均已保留。")
                            return state.checkpoint()
                        if not state.attempt(request_id, notes_added > 0):
                            await state.notify("连续研究调用没有形成新的资料依据或有效笔记，已停止重复请求并保留缺口。")
                            return await _finalize_research(state, runtime, initial, schema)
                        feedback = report["blocking_gaps"]
                        state.waiting_message = gap_message(report, round_number, settings.max_rounds)
                        await state.notify(state.waiting_message)
                        break
                    progressed, notes_added, feedback = await state.execute_batch(
                        batch, request_id, initial=initial, runtime=runtime, schema=schema, current_context=context)
                except TimeoutError:
                    state.stop("model_timeout", 'The outline research model call timed out. Unfinished reading ranges cannot be treated as read')
                    state.event(request_id + "-error.json", {"error": "timeout"})
                    await state.notify("AI 本次资料分析等待超时，已完成的阅读记录会保留；未核清的内容将标记为资料缺口。")
                    return state.checkpoint()
                except (ValueError, TypeError, KeyError, ValidationError, OSError, ModelCallFailed) as exc:
                    if isinstance(exc, ModelCallFailed):
                        diagnostics = safe_failure_diagnostics(exc)
                        if not (diagnostics.get("protocol_issue") == "tool_finish_invalid"
                                and diagnostics.get("tool_calls_received") == 0
                                and diagnostics.get("finish_reason") == "stop"):
                            raise
                    # Source/model errors are feedback for another bounded action, not facts.
                    message = str(exc)[:3000]
                    feedback = [message]
                    correction = native_retry_context(
                        "directory_research", context, result=result, error=exc, feedback=message,
                    )
                    retry_context = {key: correction[key] for key in (
                        "format_feedback", "native_retry_response", "native_retry_reasoning_content",
                    )
                                     if key in correction}
                    state.event(request_id + "-error.json", {"error": type(exc).__name__, "message": message,
                                                              "tool": step.tool if step else None,
                                                              "tool_call": raw_call,
                                                              "diagnostics": safe_failure_diagnostics(exc)})
                    for index, call in enumerate(raw_calls or [{}]):
                        item_id = request_id if len(raw_calls) <= 1 else f"{request_id}_tool_{index + 1:03d}"
                        state.record_tool(batch_metadata | {
                            "request_id": item_id, "tool": step.tool if step else "model",
                            "tool_call": call, "arguments": args,
                        }, {"error": message})
                    state.waiting_message = error_message(step.tool if step else None,
                                                         args if isinstance(args, dict) else {}, state.files, exc)
                    await state.notify(state.waiting_message)
                if not state.attempt(request_id, progressed or notes_added > 0):
                    await state.notify("连续研究调用没有形成新的资料依据或有效笔记，已停止重复请求并保留缺口。")
                    return await _finalize_research(state, runtime, initial, schema)
                state.checkpoint("partial")
            feedback = list(dict.fromkeys([*feedback, *state.validation()]))
        state.stop("round_limit", 'Three local outline-research rounds ended with key source or citation-validation gaps remaining' if settings.max_rounds == 3 else 'The outline research round budget is exhausted')
        await state.notify("资料研究已达到本次轮次上限，仍有关键内容未核清；已保留阅读记录和缺口，目录依据尚未确认完整。")
        return await _finalize_research(state, runtime, initial, schema)
    except asyncio.CancelledError:
        state.stop("cancelled", 'Outline source research was interrupted. Completed reading and model records are saved')
        state.checkpoint("partial")
        raise
    except Exception as exc:
        state.stop("model_error", 'Outline source research encountered an execution error. Completed reading and model records are saved')
        state.failure = {"code": getattr(exc, "code", "model_error"), "message": str(exc)[:1000]}
        state.checkpoint("partial")
        raise
