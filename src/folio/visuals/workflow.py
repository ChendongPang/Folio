"""Render reviewed section illustrations with durable, content-addressed artifacts."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Annotated

from docx.image.image import Image
from pydantic import Field, model_validator

from folio.models import Model, SectionContent, digest
from folio.runtime.call_budget import VisualPrefetchDeferred
from folio.runtime.token_estimate import DEFAULT_OUTPUT_TOKEN_BUDGET
from folio.visuals.review import (
    REVIEW_CONTRACT,
    LiveVisualReview,
    VisualFinding,
    VisualReview,
    assess_review,
)
from folio.visuals.semantic import (
    SEMANTIC_CONTRACT,
    SemanticReview,
    reconcile_semantics,
    semantic_cases,
)

VISUAL_ATTEMPTS = 3  # Initial draft and at most two revisions per figure.


class VisualSource(Model):
    lines: list[Annotated[str, Field(max_length=DEFAULT_OUTPUT_TOKEN_BUDGET * 8)]] = Field(min_length=1)

    @model_validator(mode="after")
    def source_budget(self):
        if not self.source.strip() or len(self.source) > DEFAULT_OUTPUT_TOKEN_BUDGET * 8:
            raise ValueError('Figure source code must be complete and no longer than 1,024,000 characters.')
        return self

    @property
    def source(self):
        return "\n".join(self.lines)


class VisualBuildResult(dict):
    @property
    def issues(self):
        return [
            {
                "section_id": section_id,
                **{key: item[key] for key in ("figure_id", "caption", "reason")},
            }
            for section_id, items in self.items()
            for item in items
            if item.get("status") == "unresolved"
        ]


class VisualSession:
    """One job shares drawing slots, cache locks and an attempt budget per identity."""

    def __init__(self):
        self.slots = asyncio.Semaphore(4)
        self.identity_locks = {}
        self.attempts = {}
        self.failures = {}


def _visual_context(section, state, visual):
    selected = [b for b in state.content.blocks if b.block_id in visual.source_block_ids]
    origin = "model_proposal" if any(b.origin == "model_proposal" for b in selected) else "evidence"
    context = {
        "section": {"title": section.title, "number": section.display_number},
        "visual": visual.model_dump(),
        "source_blocks": [b.model_dump(exclude={"evidence_ids"}) for b in selected],
        "origin": origin,
    }
    identity = digest(
        json.dumps(
            [context, [b.evidence_ids for b in selected]], ensure_ascii=False, sort_keys=True
        )
        + ":renderer-v3"
    )
    return context, selected, identity


def visual_identity(plan, state, visual):
    section = next(s for s in plan.sections if s.section_id == state.section_id)
    return _visual_context(section, state, visual)[2]


class VisualMaterial(Model):
    requirements: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(max_length=16)


async def visual_context(context, call, measure, *, force=False):
    """Read oversized sources in bounded pages; retain every original in the audit log."""

    async def fits(task, value, model):
        if measure:
            return (await measure(task, value, model))["fits"]
        return len(json.dumps(value, ensure_ascii=False)) <= 600000

    if (
        not force
        and await fits("design_ui", context, VisualSource)
    ):
        return context
    parts = []
    for block in context["source_blocks"]:
        if block.get("rows"):
            for index, row in enumerate(block["rows"]):
                # Cells stay intact. No oversized table row is silently truncated.
                parts.append({"block_id": block["block_id"], "row": index, "cells": row})
        else:
            text = block.get("text", "")
            for offset in range(0, len(text), 3000):
                parts.append(
                    {
                        "block_id": block["block_id"],
                        "offset": offset,
                        "text": text[offset : offset + 3000],
                    }
                )
    notes = []

    async def read(items):
        request = {"visual": context["visual"], "source_parts": items}
        if not await fits("read_visual_materials", request, VisualMaterial):
            if len(items) == 1:
                raise ValueError(
                    'The source material for one figure exceeds model capacity. Split its table or business scope. The original text is preserved.'
                )
            middle = len(items) // 2
            await read(items[:middle])
            await read(items[middle:])
            return
        result = await call("read_visual_materials", request, VisualMaterial)
        notes.extend(result.requirements)

    for start in range(0, len(parts), 4):
        await read(parts[start : start + 4])
    projected = {k: v for k, v in context.items() if k != "source_blocks"}
    projected["source_requirements"] = list(dict.fromkeys(notes))
    if not await fits("design_ui", projected, VisualSource):
        raise ValueError('The figure contains too much business content. Split it into an overview and detail diagrams; the complete source material is preserved.')
    return projected


def state_key(states):
    return digest(
        json.dumps(
            [{"section_id": s.section_id, "content": s.content.model_dump()} for s in states],
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def checked_file(folder, relative, sha256):
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('The figure file is missing or outside the job directory.')
    if checksum(path) != sha256:
        raise ValueError('The figure file has changed and must be regenerated before export.')
    return str(path)


def resolve_asset(folder, item):
    if not isinstance(item, dict):
        raise ValueError('The figure cache format is invalid.')
    result = dict(item)
    if result.get("status") == "unresolved":
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ValueError('An incomplete figure must retain its specific unresolved issues.')
        if any(key in result for key in ("image_path", "source_path", "width", "height")):
            raise ValueError('A figure that has not passed review cannot be inserted as a finished output.')
        return result
    for field in ("image_path", "source_path"):
        result[field] = checked_file(folder, result[field], result[field + "_sha256"])
    picture = Image.from_file(result["image_path"])
    if (result["width"], result["height"]) != (picture.px_width, picture.px_height):
        raise ValueError('The cached figure dimensions do not match the image.')
    return result


def failed_draft(directory, context):
    """Resume a failed drawing without losing its corrections or overwriting audit files."""
    attempts = sorted(
        (
            (int(p.stem.split("-")[-1]), p)
            for p in directory.glob("attempt-*.json")
            if p.stem.split("-")[-1].isdigit()
        ),
        reverse=True,
    )
    offset = attempts[0][0] if attempts else 0
    try:
        saved = json.loads((directory / "source-context.json").read_text())
        # A prompt-policy revision may change, but the underlying material must not.
        keys = ("section", "visual", "source_blocks", "origin")
        if any(saved.get(k) != context.get(k) for k in keys):
            return "", [], offset
    except (OSError, ValueError, TypeError, AttributeError):
        return "", [], offset
    for number, path in attempts:
        try:
            draft = json.loads(path.read_text())
            source = VisualSource(lines=draft["source"].splitlines()).source
            # Previous policy may contain the exact false report that triggered
            # this repair. Keep its source, but never replay unverified old advice.
            if saved.get("review_contract") != context.get("review_contract"):
                return source, [], offset
            feedback = draft.get("repair_feedback", "")
            if not isinstance(feedback, str):
                continue
            problems = [feedback] if feedback else []
            review = directory / f"review-{number}.json"
            analysis = directory / f"review-analysis-{number}.json"
            if analysis.is_file():
                checked = json.loads(analysis.read_text())
                problems.extend(checked["problems"])
                problems.extend(
                    "Unverified; check before deciding whether to modify: " + text for text in checked["unverified"]
                )
            elif review.is_file():
                problems.extend(VisualReview.model_validate_json(review.read_text()).problems)
            return source, list(dict.fromkeys(problems)), offset
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return "", [], offset


def load_visuals(folder: Path, states):
    """Reformat never invokes a model or silently drops previously requested figures."""
    if not any(s.content.visuals for s in states):
        return VisualBuildResult()
    path = folder / "visuals.json"
    if not path.is_file():
        raise ValueError('Document figures are incomplete. They cannot be omitted during reformatting.')
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") not in {1, 2} or data.get("state_key") != state_key(states):
        raise ValueError('The figure version does not match the current body. Resume the job to regenerate figures.')
    expected = {(s.section_id, v.figure_id) for s in states for v in s.content.visuals}
    actual = [(sid, v["figure_id"]) for sid, items in data["sections"].items() for v in items]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('The figure manifest does not cover all figure requirements in the current body.')
    expected_items = {
        (state.section_id, visual.figure_id): visual
        for state in states
        for visual in state.content.visuals
    }
    for sid, items in data["sections"].items():
        for item in items:
            if item.get("status") == "unresolved":
                visual = expected_items[sid, item["figure_id"]]
                if data["version"] != 2 or any(
                    item.get(key) != getattr(visual, key)
                    for key in ("caption", "kind", "after_block_id", "source_block_ids")
                ):
                    raise ValueError('The incomplete figure placeholder does not match the current figure request.')
    return VisualBuildResult(
        {
            sid: [resolve_asset(folder, item) for item in items]
            for sid, items in data["sections"].items()
        }
    )


async def build_visuals(
    plan, states, folder: Path, call, event, measure=None, *, session=None, write_manifest=True
):
    """Each figure reads only the prose/table blocks that its author explicitly selected."""
    requests = [(s, v) for s in states for v in s.content.visuals]
    if not requests:
        return VisualBuildResult()
    from folio.visuals.render import render_flowchart, render_ui

    for state in states:
        SectionContent.model_validate(state.content.model_dump())
    folder.mkdir(parents=True, exist_ok=True)
    definitions = {s.section_id: s for s in plan.sections}
    session = session or VisualSession()
    completed = 0

    async def one(state, visual):
        nonlocal completed
        section = definitions[state.section_id]
        context, selected, identity = _visual_context(section, state, visual)
        origin = context["origin"]
        incomplete_attempt = False

        async def call_visual(*args):
            nonlocal incomplete_attempt
            try:
                return await call(*args)
            except VisualPrefetchDeferred:
                # Deferral is neither a rejected picture nor a model attempt.
                # Keep previous real failures, but let final export finish this
                # interrupted design/review cycle using its normal allowance.
                if incomplete_attempt:
                    session.attempts[identity] -= 1
                    incomplete_attempt = False
                raise

        # Identical figures share a durable directory. Serialize its audit writes
        # and recheck the completed asset without occupying another workflow slot.
        async with session.identity_locks.setdefault(identity, asyncio.Lock()), session.slots:
            if identity in session.failures:
                return state.section_id, session.failures[identity]
            directory = folder / "visuals" / identity
            directory.mkdir(parents=True, exist_ok=True)
            metadata = directory / "asset.json"
            if metadata.is_file():
                try:
                    asset = json.loads(metadata.read_text(encoding="utf-8"))
                    if not isinstance(asset, dict):
                        raise ValueError('The figure cache format is invalid.')
                    if asset["identity"] != identity:
                        raise ValueError('The figure cache version does not match.')
                    if asset.get("review_contract_version") != REVIEW_CONTRACT["version"]:
                        raise ValueError('The figure must be checked against the current review contract.')
                    expected = visual.model_dump(
                        include={
                            "figure_id",
                            "kind",
                            "caption",
                            "after_block_id",
                            "source_block_ids",
                        }
                    )
                    expected["origin"] = origin
                    expected["evidence_ids"] = sorted({e for b in selected for e in b.evidence_ids})
                    if any(asset.get(k) != v for k, v in expected.items()):
                        raise ValueError('The cached figure description does not match the current body.')
                    resolve_asset(folder, asset)
                except (ValueError, KeyError, OSError, TypeError):
                    asset = None
                else:
                    completed += 1
                    event(f"已恢复配图 {completed}/{len(requests)}：{visual.caption}。")
                    return state.section_id, asset
            # Completed assets retain their identity. New or previously failed
            # figures share an explicit design/review scope instead of treating
            # every sentence in the supporting prose as mandatory diagram detail.
            context = context | {
                "review_contract": REVIEW_CONTRACT,
                "visual_scope": {
                    "version": 2,
                    "kind": visual.kind,
                    "purpose": visual.description,
                    "required": (
                        "Represent the agreed business actions, sequence and key conditional branches; do not add unsupported paths or bypass approval levels."
                        if visual.kind == "flowchart"
                        else "Represent the agreed page controls, groups, states and actions; static illustrations need no working interactions."
                    ),
                    "source_policy": (
                        "source_blocks/source_requirements are complete supporting materials, not a sentence-by-sentence drawing checklist. "
                        "A flowchart need not show auxiliary UI capabilities such as queries, exports, dialogs or fields that do not alter the flow, "
                        "unless visual explicitly requests them. Details of other pages or figures are not omissions from this figure."
                    ),
                    "branch_policy": (
                        "When a review has no explicit failure destination, show only the review action. Do not invent termination, retry or rollback just to fill two decision exits. "
                        "Represent independent optional actions using annotations or dashed associations; do not bind them to review failure. "
                        "For freeze/suspension states that block progress, explicitly show blocked progress and restricted operations; do not depict termination. "
                        "Checks determining the next step must be inside the relevant processing node or on a directed path, not merely an unrelated note. "
                        "Connect parallel optional actions individually to their triggering state; do not invent ordering between them. "
                        "Preserve conditional choice for alternatives such as warning or termination; do not turn them all into termination or require simultaneous execution. "
                        "Repair only affected nodes and edges, retaining other correct structure. A branch may end at an existing source-supported action. "
                        "Each edge in branches, merges and cycles will be checked against explicit source_blocks evidence. "
                        "Absence of a prohibition does not justify a new failure path; never fabricate a source for a design addition. "
                        "Existing body text marked model_proposal may support the diagram while retaining proposal provenance."
                    ),
                },
            }
            event(
                f"正在生成{'界面原型' if visual.kind == 'ui' else '业务流程图'}：{visual.caption}。"
            )
            previous, feedback, attempt_offset = failed_draft(directory, context)
            write_json(directory / "source-context.json", context)
            original_context = context
            context = await visual_context(context, call_visual, measure)
            constraints_path = directory / "review-constraints.json"
            constraints = []
            if constraints_path.is_file():
                try:
                    saved = json.loads(constraints_path.read_text())
                    if saved["contract"] == REVIEW_CONTRACT or saved["contract"].get("version") in {
                        1,
                        2,
                        3,
                        4,
                        5,
                    }:
                        # v2 adds advisory classification; v1's grounded forbidden
                        # paths remain violations and must survive that upgrade.
                        constraints = [
                            VisualFinding.model_validate(value) for value in saved["findings"]
                        ]
                except (ValueError, OSError, KeyError, TypeError):
                    # A corrupt review audit must not quietly forget a known rule.
                    raise ValueError('The figure constraint audit record is damaged. Check it before resuming.') from None
            task = "design_ui" if visual.kind == "ui" else "design_flowchart"
            source_model = VisualSource
            renderer = render_ui if visual.kind == "ui" else render_flowchart
            for attempt in range(VISUAL_ATTEMPTS):
                if session.attempts.get(identity, 0) >= VISUAL_ATTEMPTS:
                    raise ValueError('All three generation and repair attempts for this figure are exhausted. It remains pending confirmation.')
                repair_feedback = "；".join(feedback)
                request = context | (
                    {"previous_source": previous, "repair_feedback": repair_feedback}
                    if previous
                    else {}
                )
                if measure and not (await measure(task, request, VisualSource))["fits"]:
                    # Retry this single drawing from its requirements when the old code no longer fits.
                    request = context | {"repair_feedback": repair_feedback}
                    if not (await measure(task, request, VisualSource))["fits"]:
                        raise ValueError(
                            'The figure description still exceeds model capacity. Narrow its scope. Body text and original sources are saved.'
                        )
                session.attempts[identity] = session.attempts.get(identity, 0) + 1
                incomplete_attempt = True
                source = await call_visual(task, request, source_model)
                write_json(
                    directory / f"attempt-{attempt_offset + attempt + 1}.json",
                    request | {"source": source.source},
                )
                previous = source.source
                try:
                    rendered = await renderer(source.source, directory)
                except ValueError as exc:
                    incomplete_attempt = False
                    if getattr(exc, "code", None) == "dependency_missing":
                        raise
                    feedback = list(dict.fromkeys([*feedback, str(exc)[:1500]]))
                    if attempt + 1 < VISUAL_ATTEMPTS:
                        continue
                    raise ValueError(f'Rendering figure "{visual.caption}" failed: {str(exc)[:1500]}') from exc
                review_context = context | {"source": source.source}
                if visual.kind == "flowchart":
                    review_context["graph_index"] = rendered.get("graph_index", {"complete": False})
                if (
                    measure
                    and not (await measure("validate_visual", review_context, LiveVisualReview))[
                        "fits"
                    ]
                ):
                    context = await visual_context(original_context, call_visual, measure, force=True)
                    review_context = context | {"source": source.source}
                    if visual.kind == "flowchart":
                        review_context["graph_index"] = rendered.get(
                            "graph_index", {"complete": False}
                        )
                    if not (await measure("validate_visual", review_context, LiveVisualReview))[
                        "fits"
                    ]:
                        raise ValueError(
                            'The source code and business constraints for one figure exceed review capacity. Split it into smaller diagrams. Existing output is saved.'
                        )
                review = await call_visual("validate_visual", review_context, LiveVisualReview)
                write_json(
                    directory / f"review-{attempt_offset + attempt + 1}.json", review.model_dump()
                )
                checked = assess_review(review, original_context, rendered.get("graph_index"))
                cases = semantic_cases(
                    review, checked, original_context, rendered.get("graph_index")
                )
                if cases:
                    semantic_context = original_context | {
                        "source": source.source,
                        "review_contract": SEMANTIC_CONTRACT,
                        "semantic_cases": cases,
                    }
                    if visual.kind == "flowchart":
                        semantic_context["graph_index"] = rendered.get(
                            "graph_index", {"complete": False}
                        )
                    if (
                        measure
                        and not (
                            await measure("validate_visual", semantic_context, SemanticReview)
                        )["fits"]
                    ):
                        raise ValueError(
                            'Reviewing figure semantic differences exceeds model capacity. Complete sources and code are preserved; unresolved issues were not marked as passed.'
                        )
                    semantic_review = await call_visual(
                        "validate_visual", semantic_context, SemanticReview
                    )
                    write_json(
                        directory / f"semantic-review-{attempt_offset + attempt + 1}.json",
                        semantic_review.model_dump(),
                    )
                    checked = reconcile_semantics(
                        semantic_review,
                        cases,
                        checked,
                        original_context,
                        source.source,
                        rendered.get("graph_index"),
                    )
                    structural = assess_review(
                        VisualReview(findings=[f for f in review.findings if f.kind != "semantic"]),
                        original_context,
                        rendered.get("graph_index"),
                    )
                    # A semantic adjudication may never waive a structural fault,
                    # even if two model findings happen to reuse its description.
                    for field in ("problems", "unverified"):
                        checked[field] = list(dict.fromkeys(checked[field] + structural[field]))
                for finding in review.findings:
                    if (
                        finding.kind.startswith("forbidden")
                        and finding.description in checked["problems"]
                        and finding not in constraints
                    ):
                        constraints.append(
                            VisualFinding.model_validate(
                                finding.model_dump(include=set(VisualFinding.model_fields))
                            )
                        )
                if constraints:
                    # A later empty model response cannot waive an earlier grounded
                    # prohibited edge/path which is still present in the same source.
                    enforced = assess_review(
                        VisualReview(findings=constraints),
                        original_context,
                        rendered.get("graph_index"),
                    )
                    checked["problems"] = list(
                        dict.fromkeys(checked["problems"] + enforced["problems"])
                    )
                    checked["unverified"] = list(
                        dict.fromkeys(checked["unverified"] + enforced["unverified"])
                    )
                    write_json(
                        constraints_path,
                        {
                            "contract": REVIEW_CONTRACT,
                            "findings": [finding.model_dump() for finding in constraints],
                        },
                    )
                write_json(
                    directory / f"review-{attempt_offset + attempt + 1}.json", review.model_dump()
                )
                write_json(
                    directory / f"review-analysis-{attempt_offset + attempt + 1}.json", checked
                )
                current_problems = checked["problems"] + [
                    "Unverified; check before deciding whether to modify: " + text for text in checked["unverified"]
                ]
                incomplete_attempt = False
                if current_problems:
                    feedback = list(dict.fromkeys([*feedback, *current_problems]))
                    if attempt + 1 < VISUAL_ATTEMPTS:
                        continue
                    raise ValueError(
                        f'''Figure "{visual.caption}" failed content validation: {'；'.join(current_problems)}'''
                    )
                asset = {
                    "identity": identity,
                    "figure_id": visual.figure_id,
                    "kind": visual.kind,
                    "caption": visual.caption,
                    "origin": origin,
                    "after_block_id": visual.after_block_id,
                    "source_block_ids": visual.source_block_ids,
                    "evidence_ids": sorted({e for b in selected for e in b.evidence_ids}),
                    "width": rendered["width"],
                    "height": rendered["height"],
                    "review_contract_version": REVIEW_CONTRACT["version"],
                }
                if visual.kind == "flowchart" and rendered.get("layout", {}).get("font_size"):
                    asset["text_font_px"] = rendered["layout"]["font_size"]
                for name in ("image_path", "source_path"):
                    path = Path(rendered[name]).resolve()
                    asset[name] = str(path.relative_to(folder.resolve()))
                    asset[name + "_sha256"] = checksum(path)
                write_json(metadata, asset)
                completed += 1
                event(f"配图已完成 {completed}/{len(requests)}：{visual.caption}。")
                return state.section_id, asset
            raise RuntimeError('Figure repair attempts are exhausted.')

    async def isolated(state, visual):
        try:
            return await one(state, visual)
        except VisualPrefetchDeferred:
            # An optional budget yielding to the writer must not poison the
            # shared failure cache or produce a final unresolved figure.
            raise
        except Exception as exc:
            # Cancellation remains a cancellation. A failed figure is an explicit
            # deliverable issue, never a reason to lose other figures or the body.
            reason = (
                str(exc)[:2400]
                if isinstance(exc, ValueError)
                else f"配图生成未完成（{type(exc).__name__}）"
            )
            selected = [b for b in state.content.blocks if b.block_id in visual.source_block_ids]
            unresolved = visual.model_dump() | {
                "status": "unresolved",
                "reason": reason or "配图生成未完成，请核对后重试",
                "origin": "model_proposal"
                if any(b.origin == "model_proposal" for b in selected)
                else "evidence",
                "evidence_ids": sorted({eid for block in selected for eid in block.evidence_ids}),
            }
            session.failures[visual_identity(plan, state, visual)] = unresolved
            event(f"配图待确认：{visual.caption}；其他正文和配图继续处理。")
            return state.section_id, unresolved

    workers = [asyncio.create_task(isolated(s, v)) for s, v in requests]
    try:
        results = await asyncio.gather(*workers)
    finally:
        for worker in workers:
            # gather cancellation already reached its children. A second cancel
            # can interrupt browser/client cleanup still running in their finally.
            if not worker.done() and not worker.cancelling():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    sections = {}
    for sid, asset in results:
        sections.setdefault(sid, []).append(asset)
    if write_manifest:
        write_json(
            folder / "visuals.json",
            {"version": 2, "state_key": state_key(states), "sections": sections},
        )
        return load_visuals(folder, states)
    return VisualBuildResult(
        {sid: [resolve_asset(folder, item) for item in items] for sid, items in sections.items()}
    )
