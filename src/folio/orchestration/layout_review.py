"""Post-render visual review; commit only bounded, visually rechecked format edits."""

import asyncio
import json
import shutil
from pathlib import Path

from docx import Document
from pydantic import Field

from folio.document.formatting import (
    LayoutPatch,
    apply_patches,
    collect_targets,
    document_signature,
    protected_signature,
    save_document,
    target_view,
)
from folio.document.page_preview import (
    immutable_page_images,
    materialize_page_images,
    prepare_page_preview,
)
from folio.document.reader import visible_text, xpath
from folio.document.style_layout import layout_review_style, style_application
from folio.models import Model
from folio.runtime.errors import ModelCallBudgetExceeded

LAYOUT_REVIEW_POLICY = 1
MAX_REVIEW_PAGES = 120
MAX_REVIEW_CALLS = 180
MAX_REVIEW_SECONDS = 1800
MAX_CORRECTION_ROUNDS = 2
MAX_SCORED_CORRECTION_ROUNDS = 3
MAX_SCORED_REVIEW_CALLS = 660
PAGE_PASS_SCORE = 8.0


class PageLayoutIssue(Model):
    page: int = Field(ge=1)
    target_ids: list[str] = Field(default_factory=list, max_length=16)
    description: str = Field(min_length=1, max_length=500)


class PageLayoutReview(Model):
    issues: list[PageLayoutIssue] = Field(default_factory=list, max_length=24)
    patches: list[LayoutPatch] = Field(default_factory=list, max_length=24)
    summary: str = Field(default="", max_length=500)


async def _local(function, *args, **kwargs):
    # Cancellation must retain file ownership until the renderer has stopped.
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _normalized(text):
    return "".join(text.split())


def _page_targets(document, targets, pages):
    """Provide text-located candidates; never invent exact page coordinates."""
    result = []
    size = 0
    for target in targets:
        text = visible_text(target.element)
        needle = _normalized(text)
        if not needle:
            continue
        matching = [
            page["page"] for page in pages if needle[:40] in _normalized(page.get("text", ""))
        ]
        if not matching:
            continue
        view = target_view(document, target) | {"matching_pages": matching}
        view["allowed_properties"] = [
            key for key in view["allowed_properties"] if key != "font_delta_pt"
        ]
        if target.kind == "table":
            view["text"] = text
        length = len(json.dumps(view, ensure_ascii=False))
        if size + length > 600000:
            continue
        result.append(view)
        size += length
    return result


def _typography(document):
    # These are file declarations, not a claim about installed fonts or substitution.
    roots = [document.element, document.styles.element]
    families = sorted(
        {
            name
            for root in roots
            for name in xpath(
                root, ".//w:rFonts/@w:ascii | .//w:rFonts/@w:eastAsia | .//w:rFonts/@w:hAnsi"
            )
        }
    )
    return {"declared_font_families": families, "font_substitution_check": "not_performed"}


def _style_reference(plan, writing_profile):
    reference = layout_review_style(writing_profile, template=bool(plan.template)) or {}
    if plan.template:
        reference["template_requirements"] = plan.template.planning_context()
        reference["priority"] = "Preserve the user's template structure, formatting and completion requirements; change only attributes permitted by the target."
    return reference


async def review_rendered_document(
    path,
    plan,
    states,
    folder,
    call,
    progress,
    *,
    enabled=False,
    supports_images=False,
    renderer=None,
    max_rounds=None,
    max_calls=None,
    stage_seconds=None,
    writing_profile=None,
    score_call=None,
):
    """Review actual pages, preserving the input on any incomplete correction.

    ``renderer`` is injectable for offline tests and returns rendered PDF pages,
    their extracted text, a paginated DOCX copy and its layout mapping. A patch
    never touches the deliverable until every resulting page has been inspected
    and no issues remain. The shared engine call owns model budgets and caching.
    """
    path, folder = Path(path), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "layout-review.json"
    # A resumed/revised export must never expose an older document's PDF preview.
    (folder / "layout-preview.pdf").unlink(missing_ok=True)
    report = {
        "policy": LAYOUT_REVIEW_POLICY,
        "status": "skipped",
        "changed": False,
        "visual_checked": False,
        "calls": 0,
        "rounds": [],
        "issues": [],
    }
    if writing_profile:
        report["style_application"] = style_application(
            writing_profile, template=bool(plan.template)
        )
    if not enabled or not supports_images:
        report["reason"] = "disabled" if not enabled else "image_input_not_enabled"
        _write_report(manifest, report)
        return report
    directory = folder / "layout-review"
    directory.mkdir(parents=True, exist_ok=True)
    renderer = renderer or prepare_page_preview
    correction_limit = MAX_SCORED_CORRECTION_ROUNDS if score_call else MAX_CORRECTION_ROUNDS
    max_rounds = (
        correction_limit if max_rounds is None else min(max(0, max_rounds), correction_limit)
    )
    hard_calls_limit = MAX_SCORED_REVIEW_CALLS if score_call else MAX_REVIEW_CALLS
    calls_limit = min(max(0, max_calls), hard_calls_limit) if max_calls is not None else 0
    if score_call:
        report.update(
            scoring_provider="jev",
            threshold=PAGE_PASS_SCORE,
            score_scale=10,
            score_calls=0,
            correction_calls=0,
            page_scores=[],
            delivered_round=0,
        )
    seconds_limit = (
        min(max(0, stage_seconds), MAX_REVIEW_SECONDS)
        if stage_seconds is not None
        else MAX_REVIEW_SECONDS
    )
    report["budget"] = {
        "mode": "explicit_calls" if max_calls is not None else "page_count",
        "max_calls": calls_limit,
        "max_correction_rounds": max_rounds,
        "max_pages_per_round": MAX_REVIEW_PAGES,
        "stage_seconds": seconds_limit,
        "hard_max_calls": hard_calls_limit,
        "hard_max_seconds": MAX_REVIEW_SECONDS,
    }
    candidate = directory / "candidate.docx"
    shutil.copyfile(path, candidate)
    original_issues = []
    original_checked = False
    try:
        report["status"] = "running"
        _write_report(manifest, report)
        started = asyncio.get_running_loop().time()
        async with asyncio.timeout(seconds_limit) as deadline:
            document = Document(candidate)
            report["source_document_signature"] = document_signature(candidate)
            report["typography"] = _typography(document)
            targets = collect_targets(document, plan, states)
            signature = protected_signature(document, targets)
            style_reference = _style_reference(plan, writing_profile)
            for round_number in range(max_rounds + 1):
                preview = await _local(
                    renderer,
                    candidate,
                    directory / f"round-{round_number}",
                    max_pages=MAX_REVIEW_PAGES,
                )
                if preview["status"] != "rendered":
                    report.update(
                        status="skipped", reason=preview.get("reason", "preview_unavailable")
                    )
                    break
                pages = preview["pages"]
                if not pages:
                    raise ValueError("Rendered document has no pages")
                # A correction can change pagination. Reserve complete remaining
                # passes against the newly observed count, within fixed ceilings.
                calls_per_pass = (
                    preview["total_pages"] if score_call else (preview["total_pages"] + 1) // 2
                )
                required_calls = report["calls"] + calls_per_pass * (max_rounds - round_number + 1)
                if score_call:
                    required_calls += ((preview["total_pages"] + 1) // 2) * (
                        max_rounds - round_number
                    )
                if max_calls is None:
                    calls_limit = min(hard_calls_limit, max(calls_limit, required_calls))
                if stage_seconds is None:
                    seconds_limit = min(
                        MAX_REVIEW_SECONDS, max(180, calls_limit * 15 + (max_rounds + 1) * 60)
                    )
                    deadline.reschedule(started + seconds_limit)
                report["budget"].update(
                    max_calls=calls_limit,
                    required_calls=required_calls,
                    stage_seconds=seconds_limit,
                )
                result = {
                    "round": round_number,
                    "reviewed_pages": [],
                    "issues": [],
                    "patches": [],
                    "total_pages": preview["total_pages"],
                    "complete": False,
                    "remaining_pages": preview["total_pages"],
                }
                report["rounds"].append(result)
                if round_number == 0:
                    original_issues = result["issues"]
                patches = {}
                correction_pages = pages
                if score_call:
                    from folio.document.layout_scoring import PageLayoutScore

                    result["page_scores"] = []
                    for page in pages:
                        if report["calls"] >= calls_limit:
                            break
                        batch = await _local(materialize_page_images, preview, [page])
                        images = immutable_page_images(batch, directory / "pages")
                        context = {
                            "layout_policy": LAYOUT_REVIEW_POLICY,
                            "page_images": images,
                            "page_number": page["page"],
                            "total_pages": preview["total_pages"],
                            "threshold": PAGE_PASS_SCORE,
                            "typography": report["typography"],
                        }
                        if style_reference:
                            context["style_reference"] = style_reference
                        progress(
                            f"正在用 Jev 评价 Word 第 {page['page']}/{preview['total_pages']} 页：满分 10 分，8 分通过。"
                        )
                        report["calls"] += 1
                        report["score_calls"] += 1
                        grade = await score_call("score_layout", context, PageLayoutScore)
                        if not isinstance(grade, PageLayoutScore):
                            grade = PageLayoutScore.model_validate(grade)
                        if grade.page != page["page"]:
                            raise ValueError("Layout score references an uninspected page")
                        recorded = grade.model_dump() | {
                            "passed": grade.score >= PAGE_PASS_SCORE,
                            "image_sha256": images[0]["sha256"],
                        }
                        result["page_scores"].append(recorded)
                        if round_number == 0:
                            # Keep already obtained scores visible even when a
                            # later page fails, times out or exhausts the job budget.
                            report["page_scores"] = result["page_scores"]
                        result["reviewed_pages"].append(grade.page)
                        result["remaining_pages"] = preview["total_pages"] - len(
                            set(result["reviewed_pages"])
                        )
                        progress(
                            f"Word 第 {grade.page} 页版式评分 {grade.score:g}/10，"
                            + ("已通过。" if recorded["passed"] else "低于 8 分，将检查可调整项。")
                        )
                        if not recorded["passed"]:
                            result["issues"].append(
                                PageLayoutIssue(
                                    page=grade.page,
                                    description=f"版式评分 {grade.score:g}/10，低于 8 分：{grade.reason}"[
                                        :500
                                    ],
                                ).model_dump()
                            )
                        _write_report(manifest, report)
                    result["complete"] = (
                        len(set(result["reviewed_pages"])) == preview["total_pages"]
                    )
                    if round_number == 0:
                        original_checked = result["complete"]
                        original_issues = result["issues"]
                        report["page_scores"] = result["page_scores"]
                    if not result["complete"]:
                        report.update(
                            status="partial",
                            reason=(
                                "page_limit"
                                if preview["total_pages"] > MAX_REVIEW_PAGES
                                else "call_limit"
                            ),
                        )
                        break
                    failing = {
                        grade["page"] for grade in result["page_scores"] if not grade["passed"]
                    }
                    correction_pages = [page for page in pages if page["page"] in failing]
                    if failing and round_number >= max_rounds:
                        report.update(status="needs_attention", reason="score_below_threshold")
                        break
                    # Do not start a correction when its resulting full-page score
                    # pass cannot fit in the remaining local model-call allowance.
                    correction_calls = (len(correction_pages) + 1) // 2
                    if (
                        failing
                        and report["calls"] + correction_calls + calls_per_pass > calls_limit
                    ):
                        report.update(status="partial", reason="recheck_call_limit")
                        break
                for start in range(0, len(correction_pages), 2):
                    if report["calls"] >= calls_limit:
                        break
                    batch = correction_pages[start : start + 2]
                    batch = await _local(materialize_page_images, preview, batch)
                    views = _page_targets(document, targets, batch)
                    allowed = {view["target_id"] for view in views}
                    numbers = {page["page"] for page in batch}
                    context = {
                        "layout_policy": LAYOUT_REVIEW_POLICY,
                        "page_images": immutable_page_images(batch, directory / "pages"),
                        "targets": views,
                        "total_pages": preview["total_pages"],
                        "evaluation": 'Actual PDF page images; candidate locations are based on page-text matching, not exact coordinates.',
                    }
                    if style_reference:
                        context["style_reference"] = style_reference
                    if score_call:
                        context["page_scores"] = [
                            grade for grade in result["page_scores"] if grade["page"] in numbers
                        ]
                        context["correction_round"] = round_number + 1
                        context["max_correction_rounds"] = max_rounds
                        context["correction_instruction"] = (
                            "Jev scored these rendered pages below 8/10. Use the screenshots and scoring reasons "
                            "to propose formatting patches only within the allowed properties, improving readability and neatness. "
                            "Preserve body content, order, citations and user template/style requirements. Explain any issue that cannot be repaired safely."
                        )
                        report["correction_calls"] += 1
                        progress(
                            f"正在调整 Word 第 {min(numbers)}—{max(numbers)} 页的版式（第 {round_number + 1}/{max_rounds} 次）。"
                        )
                    else:
                        progress(f"正在检查 Word 第 {min(numbers)}—{max(numbers)} 页的实际版式。")
                    report["calls"] += 1
                    proposal = await call("review_layout", context, PageLayoutReview)
                    if not isinstance(proposal, PageLayoutReview):
                        proposal = PageLayoutReview.model_validate(proposal)
                    for issue in proposal.issues:
                        if issue.page not in numbers or not set(issue.target_ids) <= allowed:
                            raise ValueError(
                                "Layout issue references an uninspected page or target"
                            )
                    for patch in proposal.patches:
                        if patch.target_id not in allowed or patch.font_delta_pt is not None:
                            raise ValueError("Layout patch changes a protected or unlocated target")
                        if patch.target_id in patches and patches[patch.target_id] != patch:
                            raise ValueError("Conflicting layout edits from different page batches")
                        patches[patch.target_id] = patch
                    if not score_call:
                        result["reviewed_pages"].extend(sorted(numbers))
                        result["remaining_pages"] = preview["total_pages"] - len(
                            set(result["reviewed_pages"])
                        )
                        result["issues"].extend(issue.model_dump() for issue in proposal.issues)
                    else:
                        result.setdefault("correction_issues", []).extend(
                            issue.model_dump() for issue in proposal.issues
                        )
                    _write_report(manifest, report)
                result["patches"] = [
                    patch.model_dump(exclude_none=True) for patch in patches.values()
                ]
                complete = len(set(result["reviewed_pages"])) == preview["total_pages"]
                result["complete"] = complete
                if round_number == 0:
                    original_checked = complete
                    original_issues = result["issues"]
                if not complete:
                    report.update(
                        status="partial",
                        reason=(
                            "page_limit"
                            if preview["total_pages"] > MAX_REVIEW_PAGES
                            else "call_limit"
                        ),
                    )
                    break
                if not patches and not result["issues"]:
                    # Trusted pagination only updates cached page references. The
                    # candidate was content-checked before rendering; no Office save.
                    pdf = folder / "layout-preview.pdf"
                    shutil.copyfile(preview["pdf"], pdf)
                    published = directory / "approved.docx"
                    shutil.copyfile(preview["document"], published)
                    published.replace(path)
                    if score_call:
                        report.update(
                            page_scores=result["page_scores"], delivered_round=round_number
                        )
                    report.update(
                        status="passed",
                        visual_checked=True,
                        changed=round_number > 0,
                        layout=preview["layout"],
                        pdf=str(pdf),
                        issues=[],
                        target_document_signature=document_signature(candidate),
                    )
                    break
                if not patches or round_number >= max_rounds:
                    report.update(status="needs_attention", reason="unresolved_layout")
                    break
                if report["calls"] + calls_per_pass > calls_limit:
                    report.update(status="partial", reason="recheck_call_limit")
                    break
                if not apply_patches(document, targets, list(patches.values())):
                    report.update(status="needs_attention", reason="ineffective_patches")
                    break
                if protected_signature(document, targets) != signature:
                    raise ValueError("Layout changed protected content")
                save_document(document, candidate)
                result["applied"] = True
                if score_call:
                    progress(
                        f"第 {round_number + 1}/{max_rounds} 次版式调整已完成，正在重新分页、截图并逐页复评。"
                    )
            if report["status"] != "passed":
                report.update(visual_checked=original_checked, issues=original_issues)
    except asyncio.CancelledError:
        report.update(
            status="cancelled",
            changed=False,
            visual_checked=original_checked,
            issues=original_issues,
        )
        raise
    except Exception as exc:
        report.update(
            status="timeout" if isinstance(exc, TimeoutError) else "failed",
            changed=False,
            visual_checked=original_checked,
            issues=original_issues,
            error_type=type(exc).__name__,
        )
        progress("页面版式审校未完成，已保留原 Word 排版。")
        if isinstance(exc, ModelCallBudgetExceeded):
            raise
    finally:
        candidate.unlink(missing_ok=True)
        _write_report(manifest, report)
    return report
