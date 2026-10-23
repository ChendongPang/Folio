"""Source-backed research notes over the existing evidence and execution ledgers.

This stores notes and navigation, never a second copy of source text. A fetched
batch is not considered reviewed until its actual model delivery is recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from folio.research.planning_progress import source_name


def normalize_research_note(value):
    """Derive only a navigation label; never invent note text or provenance."""
    if not isinstance(value, dict):
        return value
    normalized = {key: item for key, item in value.items() if key != "request_ids"}
    topic = normalized.get("topic")
    text = normalized.get("text")
    if (topic is None or isinstance(topic, str) and not topic.strip()) and isinstance(text, str):
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line:
            normalized["topic"] = first_line[:80]
    return normalized


class ResearchNoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: str | None = Field(default=None, max_length=80)
    topic: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    kind: Literal["source_statement", "inference", "no_relevance"]
    evidence_ids: list[str] = Field(
        min_length=1,
        description="Evidence IDs for source text actually read and summarized in this note. Supply only the corresponding E IDs; the program associates sources and reading records.",
    )
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_request_ids(cls, value):
        # Older tool replies and checkpoints supplied receipt IDs. They remain
        # readable, but only the delivery ledger can establish note provenance.
        return normalize_research_note(value)


def _dict(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _selector(tool, args):
    # Page size is significant: expanding a deliberately small read can yield
    # new evidence. Cursors must be resolved by the caller before this check.
    return _digest([tool, {k: v for k, v in args.items()
                          if k not in {"summary", "notes", "research_notes", "review_reason"} and v is not None}])


def _same_references(batch, other, evidence_ids):
    """Evidence IDs alone cannot establish that two batches read the same source."""
    return {key for key in evidence_ids
            if key in batch["references"] and key in other["references"]
            and all(batch["references"][key].get(field) for field in ("source_id", "source_version", "locator"))
            and batch["references"][key] == other["references"][key]}


def _inherit_delivery(batch, batches):
    delivered = set(batch.get("delivered_evidence_ids", []))
    reviewed = set(batch.get("reviewed_evidence_ids", [])) & delivered
    for other in batches:
        same = _same_references(batch, other, other.get("delivered_evidence_ids", []))
        delivered.update(same)
        reviewed.update(same & set(other.get("reviewed_evidence_ids", [])))
    batch["delivered_evidence_ids"] = sorted(delivered)
    batch["reviewed_evidence_ids"] = sorted(reviewed)
    if batch["evidence_ids"]:
        batch["delivered"] = set(batch["evidence_ids"]) <= delivered
        batch["reviewed"] = set(batch["evidence_ids"]) <= reviewed


class ResearchMemory:
    def __init__(self, saved_dict=None):
        self.state = deepcopy(saved_dict or {})
        for key, default in (("version", 1), ("batches", {}), ("notes", []),
                             ("applied_calls", {}), ("attempts", {}), ("stalled_count", 0)):
            self.state.setdefault(key, default)
        # Repair older checkpoints where a reread inherited "reviewed" but not
        # the matching delivery record. A matching prior batch is still required.
        for batch in self.state["batches"].values():
            _inherit_delivery(batch, self.state["batches"].values())

    def dump(self):
        return deepcopy(self.state)

    @property
    def stalled_count(self):
        return self.state["stalled_count"]

    def record_observation(self, observation):
        request_id = observation.get("request_id")
        if not request_id or request_id in self.state["batches"]:
            return
        result = observation.get("result", {})
        result = result if isinstance(result, dict) else {}
        args = observation.get("arguments", {})
        tool = observation.get("tool", "")
        # Search snippets and structure previews are navigation, not read evidence.
        evidence = [_dict(item) for item in result.get("evidence", [])] if tool == "fs.read" else []
        evidence = [item for item in evidence if isinstance(item, dict) and item.get("evidence_id")]
        path = args.get("path") or result.get("path") or (evidence[0].get("source_id", "") if evidence else "")
        batch = {
            "request_id": request_id, "tool": tool, "path": path,
            "scope": args.get("scope", "sources"), "selector": _selector(tool, args),
            "position": {key: args[key] for key in ("node_id", "page", "slide", "sheet", "cell_range") if key in args},
            "offset": result.get("offset", args.get("offset", 0)), "next_offset": result.get("next_offset"),
            "character_offset": result.get("character_offset", args.get("character_offset", 0)),
            "next_character_offset": result.get("next_character_offset"),
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "references": {item["evidence_id"]: {key: item.get(key, "")
                           for key in ("source_id", "source_version", "locator")} for item in evidence},
            "delivered_evidence_ids": [], "reviewed_evidence_ids": [],
            "delivered": False, "reviewed": False,
            "original_request_id": result.get("original_request_id", args.get("request_id", "")),
        }
        _inherit_delivery(batch, self.state["batches"].values())
        self.state["batches"][request_id] = batch

    def record_delivery_evidence(self, request_id, evidence):
        """Associate canonical capacity slices with their existing read receipt.

        The caller resolves every slice against the immutable source first.
        Adding a slice does not mark the original block or its unread tail as
        delivered, and does not replace the archived tool result.
        """
        batch = self.state["batches"].get(request_id)
        if batch is None or batch["tool"] != "fs.read":
            raise ValueError("Capacity slices require an existing source read")
        additions = {}
        for raw in evidence:
            item = _dict(raw)
            key = item["evidence_id"]
            reference = {field: item.get(field, "")
                         for field in ("source_id", "source_version", "locator")}
            if key in batch["references"]:
                if reference != batch["references"][key]:
                    raise ValueError("Capacity slice source identity changed")
                continue
            location = re.fullmatch(r"(.+)/characters (\d+):(\d+)", reference["locator"])
            if location is None:
                raise ValueError("Capacity evidence must be a slice of the original read")
            base, start, end = location[1], int(location[2]), int(location[3])
            contained = False
            for original in batch["references"].values():
                if any(reference[field] != original[field] for field in ("source_id", "source_version")):
                    continue
                parent = re.fullmatch(r"(.+)/characters (\d+):(\d+)", original["locator"])
                if (original["locator"] == base
                        or parent and parent[1] == base and int(parent[2]) <= start < end <= int(parent[3])):
                    contained = True
                    break
            if not contained:
                raise ValueError("Capacity slice is outside the original source read")
            additions[key] = reference
        batch["references"].update(additions)
        batch["evidence_ids"].extend(additions)
        batch["delivered"] = set(batch["evidence_ids"]) <= set(batch["delivered_evidence_ids"])

    def record_delivery(self, request_id, evidence_ids=(), complete=True, _seen=None):
        seen = set() if _seen is None else _seen
        if request_id in seen:
            return
        seen.add(request_id)
        batch = self.state["batches"].get(request_id)
        if batch is None:
            return
        known = set(batch["evidence_ids"])
        delivered = set(batch["delivered_evidence_ids"]) | (set(evidence_ids) & known)
        batch["delivered_evidence_ids"] = sorted(delivered)
        batch["delivered"] = bool(known <= delivered) if known else batch["delivered"] or complete
        for other in self.state["batches"].values():
            same = _same_references(batch, other, delivered)
            other["delivered_evidence_ids"] = sorted(set(other["delivered_evidence_ids"]) | same)
            if other["evidence_ids"]:
                other["delivered"] = set(other["evidence_ids"]) <= set(other["delivered_evidence_ids"])
        if batch["original_request_id"]:
            original = self.state["batches"].get(batch["original_request_id"])
            if original is not None:
                self.record_delivery(batch["original_request_id"], _same_references(batch, original, delivered),
                                     complete=False, _seen=seen)

    def can_drop(self, request_id):
        batch = self.state["batches"].get(request_id)
        if batch is None:
            return False
        return not batch["evidence_ids"] or (batch["delivered"] and batch["reviewed"])

    def pending_requests(self):
        return [key for key, batch in self.state["batches"].items()
                if batch["evidence_ids"] and not self.can_drop(key)]

    def apply(self, updates, *, evidence, resolve_evidence=lambda key: key, call_id=""):
        """Validate the entire delta before changing anything; replay is idempotent."""
        if not updates:
            return 0
        updates = [item if isinstance(item, ResearchNoteUpdate) else ResearchNoteUpdate.model_validate(item)
                   for item in updates]
        payload = [item.model_dump(exclude_unset=True) for item in updates]
        fingerprint = _digest(payload)
        prior = self.state["applied_calls"].get(call_id) if call_id else None
        if prior:
            if prior != fingerprint:
                raise ValueError('The same tool call cannot overwrite saved research notes')
            return 0
        work = deepcopy(self.state)
        changes = 0
        for update in updates:
            refs = list(dict.fromkeys(resolve_evidence(key) for key in update.evidence_ids))
            batch_refs = {}
            references = {}
            for ref in refs:
                raw = _dict(evidence.get(ref))
                candidates = [
                    (request, batch["references"][ref])
                    for request, batch in work["batches"].items()
                    if batch["tool"] == "fs.read" and ref in batch["delivered_evidence_ids"]
                    and ref in batch["references"]
                ]
                if not isinstance(raw, dict) or not candidates:
                    raise ValueError(f'Note citations must refer to original text actually delivered to the model: {ref}')
                matching = [
                    (request, candidate) for request, candidate in candidates
                    if all(raw.get(key) and raw[key] == candidate.get(key)
                           for key in ("source_id", "source_version", "locator"))
                ]
                if not matching:
                    raise ValueError(f"A note citation's source version changed or its location does not match: {ref}")
                references[ref] = matching[0][1]
                for request, _ in matching:
                    batch_refs.setdefault(request, set()).add(ref)
            requests = list(batch_refs)
            entry = update.model_dump(exclude={"note_id"}) | {
                "evidence_ids": refs, "request_ids": requests, "references": references,
            }
            existing = next((item for item in work["notes"] if item["note_id"] == update.note_id), None)
            if update.note_id and existing is None:
                raise ValueError('The note ID to revise does not exist: ' + update.note_id)
            if existing and "unresolved" not in update.model_fields_set:
                entry["unresolved"] = deepcopy(existing["unresolved"])
            comparable = {k: v for k, v in entry.items() if k != "request_ids"}
            if existing is None:
                existing = next((item for item in work["notes"] if all(item.get(k) == v for k, v in comparable.items())), None)
            if existing is None:
                work["notes"].append(entry | {"note_id": f"M{len(work['notes']) + 1}", "revision": 1, "revisions": []})
                changes += 1
            elif any(existing.get(k) != v for k, v in comparable.items()):
                existing["revisions"].append({k: v for k, v in existing.items() if k != "revisions"})
                existing.update(entry | {"revision": existing["revision"] + 1})
                changes += 1
            else:
                existing["request_ids"] = list(dict.fromkeys(existing["request_ids"] + requests))
            for request, cited in batch_refs.items():
                batch = work["batches"][request]
                reviewed = set(batch.get("reviewed_evidence_ids", [])) | cited
                if reviewed != set(batch.get("reviewed_evidence_ids", [])):
                    changes += 1
                batch["reviewed_evidence_ids"] = sorted(reviewed)
                batch["reviewed"] = set(batch["evidence_ids"]) <= reviewed
                # Re-reading an archived page does not create a new source.
                # Its reviewed evidence also covers that part of the original batch.
                for other in work["batches"].values():
                    same = _same_references(batch, other, cited & set(other["delivered_evidence_ids"]))
                    other["reviewed_evidence_ids"] = sorted(set(other.get("reviewed_evidence_ids", [])) | same)
                    other["reviewed"] = bool(other["evidence_ids"]) and set(other["evidence_ids"]) <= set(other["reviewed_evidence_ids"])
        if call_id:
            work["applied_calls"][call_id] = fingerprint
        self.state = work
        return changes

    def markdown(self, evidence_label=None):
        label = evidence_label or (lambda value: value)
        lines = ["# Research notes", "", "Notes are source summaries or model assessments, not original evidence, and do not automatically become global facts."]
        kinds = {"source_statement": "Source statement", "inference": "Model inference", "no_relevance": "Irrelevant to this task"}
        for note in self.state["notes"]:
            lines += ["", f"## {note['note_id']} · {note['topic']}", "", f"Kind: {kinds[note['kind']]}; revision: {note['revision']}", "", note["text"], "", "Evidence:"]
            for key in note["evidence_ids"]:
                ref = note["references"][key]
                lines.append(f"- {label(key)} · {source_name(ref['source_id'])} · {ref['locator']}")
            if note["unresolved"]:
                lines += ["To verify:", *(f"- {value}" for value in note["unresolved"])]
        if not self.state["notes"]:
            lines.append("\nNo research notes with provenance yet.")
        return "\n".join(lines) + "\n"

    def context_text(self, max_chars=None, evidence_label=None):
        full = self.markdown(evidence_label)
        if max_chars is None or len(full) <= max_chars:
            return full
        label = evidence_label or (lambda value: value)
        rows = ["Research note index (original text and complete notes are preserved):",
                "Complete notes: fs_read(scope='context',field='research_notes',offset=0,limit=4000); continue from the returned position."]
        for note in self.state["notes"]:
            rows.append(f"{note['note_id']} | {note['topic']} | {note['kind']} | "
                        + ", ".join(label(key) for key in note["evidence_ids"][:3])
                        + " | " + note["text"][:120].replace("\n", " ")
                        + ("…" if len(note["text"]) > 120 else "")
                        + (f" | {len(note['unresolved'])} unresolved items" if note["unresolved"] else ""))
        return self._bounded("\n".join(rows), max_chars, "research_notes")

    def history_text(self, max_chars=None, evidence_label=None):
        label = evidence_label or (lambda value: value)
        rows = ["Reading history index (retrieve original results with fs_read(scope='history',request_id=...)):"]
        batches = list(self.state["batches"].items())
        if max_chars is not None:
            batches.reverse()  # The currently visible tool result must keep a usable batch ID.
        for key, batch in batches:
            refs = list(batch["references"].values())
            locators = list(dict.fromkeys(ref["locator"] for ref in refs))
            position = " — ".join(locators[:1] + locators[-1:]) if len(locators) > 1 else (locators[0] if locators else str(batch["position"]))
            state = "Summarized by the model" if batch["reviewed"] else "Sent; not summarized" if batch["delivered"] and refs else "Not yet fully sent" if refs else "Navigation result"
            row = f"{key} | {batch['tool']} | {source_name(batch['path']) or batch['scope']} | {position} | {state}"
            if batch["original_request_id"]:
                row += " | reread " + batch["original_request_id"]
            if batch["evidence_ids"]:
                row += " | " + ", ".join(label(ref) for ref in batch["evidence_ids"][:3])
                row += f" ({len(batch['evidence_ids'])} passages total)"
            rows.append(row)
        full = "\n".join(rows)
        return full if max_chars is None else self._bounded(full, max_chars, "research_history")

    @staticmethod
    def _bounded(text, maximum, field):
        footer = f"\nComplete content: fs_read(scope='context',field='{field}',offset=0,limit=4000); continue from the returned position."
        if len(text) <= maximum:
            return text
        room = max(0, maximum - len(footer) - 24)
        lines = text[:room].rsplit("\n", 1)[0]
        return lines + "\n(Index not fully displayed)" + footer

    def check_repeat(self, tool, args, review_reason=""):
        if tool != "fs.read":
            return None
        signature = _selector(tool, args)
        matches = [batch for batch in self.state["batches"].values()
                   if batch["selector"] == signature and batch["delivered"] and batch["evidence_ids"]]
        if matches and not review_reason.strip():
            batch = matches[-1]
            return (f"This range was already sent to the model: {batch['request_id']}, {source_name(batch['path'])}. "
                    "Use saved notes first. If verification is needed, state the specific question in review_reason or read relevant passages by evidence ID.")
        return None

    def record_attempt(self, call_id, progress):
        if call_id not in self.state["attempts"]:
            self.state["attempts"][call_id] = bool(progress)
            self.state["stalled_count"] = 0 if progress else self.state["stalled_count"] + 1
        return self.stalled_count
