"""Bounded, source-grounded checks before integrating model-origin supplements.

Every material/proposal pair is checked against one immutable candidate version.
Corrections are global, then checked again against every original material. Notes
remain typed references to originals; they never become evidence themselves.
"""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from pydantic import Field

from folio.models import Model, digest
from folio.orchestration.review import parallel_reviews
from folio.orchestration.review_trace import review_calls
from folio.orchestration.work import GapProposal
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted

POLICY_VERSION = 1
MAX_CALLS = 32
MAX_CORRECTIONS = 2
MAX_CONSTRAINTS = 48
TIMEOUT_SECONDS = 120
CHECK_TASK = "check_supplement_conflicts"
REPAIR_TASK = "repair_supplement_conflicts"
TASK_INSTRUCTIONS = {
    CHECK_TASK: (
        "Check each supplement on this page against catalog sources for explicit conflicts under the same scope and conditions. Return compatible/conflict/uncertain exactly once per gap_id. An omission, lack of detail or missing citation is not itself a conflict. Check trailing negations, exceptions and restrictions. A fragment is a lossless part of its source; absence within it does not prove absence in the complete source. Return uncertain when cross-fragment relationships cannot be resolved. A conflict requires verbatim proposal_quote and source_quote plus this page's source_ref/source_kind for the corresponding gap_id. Only original evidence can establish a source conflict; facts, proposals and risks remain indexes, suggestions or unresolved issues, not project facts. Public sources do not prove internal project choices. Record only necessary negative rules, applicability conditions and conflicts for these supplements, retaining source kinds. Even compatible results must include applicable established constraints with verbatim quotations for later integration; return empty constraints only when none apply. Do not edit supplements or silently skip candidates to save space; return uncertain if checking cannot be completed."
    ),
    REPAIR_TASK: (
        "Correct the conflicting supplements together using the source-backed constraints collected from all batches. changes contains only gap_ids within conflicted_gap_ids that need editing, with complete replacement content. Do not change other gap_ids, their assigned sections, disposition or provenance. Supplements remain model proposals; reading constraints does not make them source-verified. Satisfy all supplied rules and conditions simultaneously, rather than letting the last source override earlier ones. If originals conflict, constraints are incompatible or correction would require guessing, set unresolved=true and explain without choosing a version. Do not add out-of-scope business designs. All originals will be checked again after correction."
    ),
}


class SupplementConstraint(Model):
    gap_ids: list[str] = Field(min_length=1, max_length=8)
    source_ref: str = Field(min_length=1)
    source_kind: Literal["evidence", "fact", "proposal", "risk"]
    source_quote: str = Field(min_length=1, max_length=1200)
    proposal_quote: str = Field(default="", max_length=1200)
    reason: str = Field(min_length=1, max_length=300)


class SupplementVerdict(Model):
    gap_id: str = Field(min_length=1)
    status: Literal["compatible", "conflict", "uncertain"]
    reason: str = Field(default="", max_length=300)


class SupplementPageCheck(Model):
    checks: list[SupplementVerdict] = Field(min_length=1, max_length=4)
    constraints: list[SupplementConstraint] = Field(default_factory=list, max_length=16)


class SupplementChange(Model):
    gap_id: str = Field(min_length=1)
    content: str = Field(min_length=20, max_length=5000)


class SupplementCorrection(Model):
    changes: list[SupplementChange] = Field(default_factory=list, max_length=8)
    unresolved: bool
    reason: str = Field(default="", max_length=500)


@dataclass
class SupplementCheckResult:
    status: Literal["checked", "pending"]
    proposals: list[dict]
    constraints: list[dict] = field(default_factory=list)
    reason: str = ""
    calls: int = 0


@dataclass
class SupplementCheckBudget:
    """Share runtime-attempt allowance and one monotonic deadline across a section."""

    max_calls: int = field(default_factory=lambda: MAX_CALLS)
    timeout_seconds: float = field(default_factory=lambda: TIMEOUT_SECONDS)
    deadline: float | None = None
    calls: int = field(default=0, init=False)
    _reserved: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        if type(self.max_calls) is not int or self.max_calls < 1 or self.timeout_seconds <= 0:
            raise ValueError("Supplement check budgets must be positive")
        if self.deadline is None:
            self.deadline = monotonic() + self.timeout_seconds

    @property
    def remaining(self):
        return max(0, self.max_calls - self.calls - self._reserved)

    def reserve(self):
        # No await between checking and reserving; parallel page workers cannot
        # oversubscribe the shared budget while token measurement is awaiting.
        if self.remaining < 1:
            raise _Pending('Supplement verification reached its request limit. Unfinished checks remain pending confirmation.')
        self._reserved += 1

    def settle(self, calls):
        self._reserved -= 1
        self.calls += calls


class _Pending(Exception):
    pass


class _Capacity(Exception):
    pass


def _encoded(value):
    return (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )


def _public_constraints(constraints):
    return [
        {key: value for key, value in item.items() if key != "source_quote"}
        | {"quote": item["source_quote"]}
        for item in constraints
    ]


def _source_strings(value):
    """Citation IDs/metadata alone cannot ground a business contradiction."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _source_strings(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            if (
                key == "metadata"
                or key.endswith(("_id", "_ids", "_ref", "_refs"))
                or key in {"source_uri", "url", "path", "locator", "kind", "origin", "disposition"}
            ):
                continue
            yield from _source_strings(child)


def _quote_present(quote, material, original):
    if not quote.strip() or not any(quote in text for text in _source_strings(original["value"])):
        return False
    if "fragment" not in material:
        return True
    # JSON fragments carry escaped string contents; preserve the original quote
    # while checking that its exact transmitted form was actually inspected.
    encoded_quote = json.dumps(quote, ensure_ascii=False)[1:-1]
    return quote in material["value"] or encoded_quote in material["value"]


def _split_material(material):
    raw = _encoded(material["value"])
    if len(raw) <= 256:
        raise _Pending('An individual original source plus shared checking requirements still exceeds capacity. Sources were not truncated.')
    source = material.get("fragment", {})
    offset, total = source.get("start", 0), source.get("total_characters", len(raw))
    mid = len(raw) // 2
    return [
        material
        | {
            "value": raw[start:end],
            "fragment": {
                "parent_ref": material["context_ref"],
                "start": offset + start,
                "end": offset + end,
                "total_characters": total,
                "encoding": source.get(
                    "encoding", "text" if isinstance(material["value"], str) else "json"
                ),
            },
        }
        for start, end in ((0, mid), (mid, len(raw)))
    ]


class _Checker:
    def __init__(self, engine, section, proposals, materials, budget=None):
        self.engine, self.section = engine, deepcopy(section)
        self.original = deepcopy(proposals)
        self.materials = deepcopy(materials)
        self.originals = {m["context_ref"]: m for m in materials}
        self.calls = 0
        self.constraints = []
        self.budget = budget or SupplementCheckBudget()

    def context(self, proposals, materials):
        return {
            "supplement_check_policy": POLICY_VERSION,
            "section": self.section,
            "proposals": proposals,
            "catalog": materials,
        }

    async def invoke(self, task, context, model):
        if self.budget.remaining < 1:
            raise _Pending('Supplement verification reached its request limit. Unfinished checks remain pending confirmation.')
        if not (await self.engine.measure_request(task, context, model))["fits"]:
            raise _Capacity
        # Measurement may await a tokenizer while sibling workers reserve calls.
        self.budget.reserve()
        try:
            # The engine gives these two task names one durable attempt. This
            # retains normal task caching/accounting without shared-limit changes.
            with review_calls() as stats:
                try:
                    return await self.engine._call(task, context, model)
                finally:
                    measured = stats.snapshot()
                    calls = measured["model_calls"]
                    if (
                        not calls
                        and not measured["cache_hits"]
                        and not hasattr(self.engine, "_call_unlocked")
                    ):
                        # Lightweight embedding engines without the normal
                        # accounting hook conservatively spend one reservation.
                        calls = 1
                    self.budget.settle(calls)
                    self.calls += calls
        except ModelCallFailed as exc:
            if exc.code in {"context_limit", "length_limit"}:
                raise _Capacity from exc
            raise _Pending("The model service did not complete supplement verification normally. This round's result was not adopted.") from exc
        except TaskRetriesExhausted as exc:
            if any(
                marker in (exc.last_error or "")
                for marker in ("[context_limit]", "[length_limit]", "finish_reason='length'")
            ):
                raise _Capacity from exc
            raise _Pending('A previous supplement verification request is incomplete. Original text is preserved pending confirmation.') from exc

    async def pages(self, proposals, materials, *, force_split=False):
        context = self.context(proposals, materials)
        if not force_split and len(proposals) <= 4 and len(materials) <= 8:
            if (await self.engine.measure_request(CHECK_TASK, context, SupplementPageCheck))[
                "fits"
            ]:
                return [context]
        # Split the larger input axis; retain the complete opposite axis. This
        # preserves the full proposal/material cross-product without last-wins.
        proposal_size = len(_encoded(proposals))
        material_size = len(_encoded(materials))
        if len(proposals) > 4:
            mid = len(proposals) // 2
            halves = [(proposals[:mid], materials), (proposals[mid:], materials)]
        elif len(materials) > 1 and (len(proposals) == 1 or material_size >= proposal_size):
            mid = len(materials) // 2
            halves = [(proposals, materials[:mid]), (proposals, materials[mid:])]
        elif len(proposals) > 1:
            mid = len(proposals) // 2
            halves = [(proposals[:mid], materials), (proposals[mid:], materials)]
        else:
            halves = [(proposals, [fragment]) for fragment in _split_material(materials[0])]
        result = []
        for selected_proposals, selected_materials in halves:
            result += await self.pages(selected_proposals, selected_materials)
            if len(result) > self.budget.remaining:
                raise _Pending('Checking all complete sources requires more batches than allowed. No sources were skipped; confirmation is required.')
        return result

    def validate(self, result, context):
        candidates = {p["gap_id"]: p for p in context["proposals"]}
        verdict_ids = [v.gap_id for v in result.checks]
        if len(verdict_ids) != len(set(verdict_ids)) or set(verdict_ids) != set(candidates):
            raise _Pending('The model did not return a check result for every supplement in this batch. Absence of conflicts cannot be established.')
        available = {m["context_ref"]: m for m in context["catalog"]}
        constraints = []
        for item in result.constraints:
            source = available.get(item.source_ref)
            if (
                source is None
                or source["kind"] != item.source_kind
                or not set(item.gap_ids) <= candidates.keys()
                or not _quote_present(item.source_quote, source, self.originals[item.source_ref])
                or (
                    item.proposal_quote
                    and any(
                        item.proposal_quote not in candidates[gap]["content"]
                        for gap in item.gap_ids
                    )
                )
            ):
                raise _Pending('Source, quote, or proposal references in supplement verification could not be verified. The conclusion was not adopted.')
            constraints.append(
                item.model_dump()
                | ({"source_span": source["fragment"]} if "fragment" in source else {})
            )
        for verdict in result.checks:
            if verdict.status == "uncertain":
                raise _Pending('Applicability or conflict relationships in some original text remain unclear. Original body text is preserved pending confirmation.')
            if verdict.status == "conflict":
                support = [
                    c
                    for c in constraints
                    if verdict.gap_id in c["gap_ids"] and c["proposal_quote"].strip()
                ]
                if not support or not any(c["source_kind"] == "evidence" for c in support):
                    raise _Pending('The suspected conflict was not located in an original evidence quote. Index entries or model suggestions cannot be used to rewrite the body.')
        return {v.gap_id for v in result.checks if v.status == "conflict"}, constraints

    async def check_all(self, candidates, round_number):
        pages = await self.pages(candidates, self.materials)
        if len(pages) > self.budget.remaining:
            raise _Pending('Complete rechecking requires more requests than the remaining budget. Original body text is preserved pending confirmation.')
        completed = 0

        async def check(context):
            try:
                result = await self.invoke(CHECK_TASK, context, SupplementPageCheck)
            except _Capacity:
                children = await self.pages(
                    context["proposals"], context["catalog"], force_split=True
                )
                results = []
                for child in children:
                    results.extend(await check(child))
                return results
            return [self.validate(result, context)]

        def progress(*_):
            nonlocal completed
            completed += 1
            self.engine.store.work_event(
                self.engine.job_id,
                f"《{self.section.get('title', 'Current section')}》补写冲突核对：第 {round_number} 轮已完成 {completed}/{len(pages)} 批。",
            )

        results = await parallel_reviews(pages, 4, check, progress)
        conflicts, constraints = set(), {}
        for batch in results:
            for found, notes in batch:
                conflicts.update(found)
                for note in notes:
                    constraints[digest(json.dumps(note, sort_keys=True))] = note
        if len(constraints) > MAX_CONSTRAINTS:
            raise _Pending('Too many source constraints must be checked together. Rules cannot be omitted to continue supplementation.')
        return conflicts, list(constraints.values())

    async def run(self):
        candidates = deepcopy(self.original)
        if not candidates:
            return SupplementCheckResult("checked", candidates)
        if not self.materials:
            raise _Pending('No relevant original sources are available for checking. Supplement conflicts remain unchecked.')
        if len(self.originals) != len(self.materials):
            raise _Pending('Original source references are not unique. Supplements cannot yet be checked safely.')
        ids = [p["gap_id"] for p in candidates]
        if len(ids) != len(set(ids)):
            raise _Pending('Supplement references are not unique. Revisions cannot yet proceed safely.')
        for item in candidates:
            GapProposal.model_validate(item)
        for item in self.materials:
            if item["kind"] not in {"evidence", "fact", "proposal", "risk"} or "fragment" in item:
                raise _Pending('Inputs must preserve complete original sources and explicit source types.')
        for round_index in range(MAX_CORRECTIONS + 1):
            conflicts, self.constraints = await self.check_all(candidates, round_index + 1)
            if not conflicts:
                return SupplementCheckResult(
                    "checked", candidates, _public_constraints(self.constraints), calls=self.calls
                )
            if round_index == MAX_CORRECTIONS:
                raise _Pending('Supplements still conflict with original text after two correction rounds. Original body text is preserved pending confirmation.')
            context = {
                "supplement_check_policy": POLICY_VERSION,
                "section": self.section,
                "proposals": candidates,
                "conflicted_gap_ids": sorted(conflicts),
                "constraints": self.constraints,
            }
            try:
                correction = await self.invoke(REPAIR_TASK, context, SupplementCorrection)
            except _Capacity as exc:
                # Opposing rules may not be split into independent last-wins edits.
                raise _Pending(
                    'Combined conflicts and constraints cannot be safely corrected in one request. Original body text is preserved.'
                ) from exc
            if correction.unresolved:
                raise _Pending('Source constraints remain inconsistent or ambiguous. This supplement revision was not adopted.')
            changes = {c.gap_id: c.content for c in correction.changes}
            if (
                len(changes) != len(correction.changes)
                or not changes
                or not set(changes) <= conflicts
            ):
                raise _Pending('The supplement revision exceeded the confirmed conflict scope. The result was not adopted.')
            revised = [
                p | {"content": changes[p["gap_id"]]} if p["gap_id"] in changes else p
                for p in candidates
            ]
            if revised == candidates:
                raise _Pending('The supplement revision made no effective changes. Repeated checking stopped; original body text is preserved.')
            candidates = revised
        raise AssertionError("Unreachable supplement correction state")


async def check_supplement_conflicts(engine, section, proposals, materials, *, budget=None):
    """Return checked candidates, or unchanged originals plus an explicit pending reason.

    The caller owns integration/validation/persistence of document content. External
    cancellation propagates; this optional operation's own errors preserve originals.
    """
    checker = _Checker(engine, section, proposals, materials, budget)
    try:
        async with asyncio.timeout(max(0, checker.budget.deadline - monotonic())):
            return await checker.run()
    except TimeoutError:
        reason = 'Supplement conflict checking reached its time limit. Incomplete checks remain pending confirmation.'
    except _Pending as exc:
        reason = str(exc)
    except Exception:
        reason = 'Supplement conflict checking was incomplete or unverifiable. Original body text and sources are preserved.'
    engine.store.work_event(engine.job_id, f"《{section.get('title', 'Current section')}》{reason}")
    return SupplementCheckResult(
        "pending",
        deepcopy(proposals),
        _public_constraints(checker.constraints),
        reason,
        checker.calls,
    )
