"""Re-layout a saved job without model calls or any changes to its business state."""

import json
from pathlib import Path

from folio.document.formatting import replay_formatting, replay_page_review
from folio.document.pagination import paginate
from folio.document.presentation import provenance_summary
from folio.document.references import ReferenceDocuments
from folio.document.template_layout import validate_template_output
from folio.document.word import render
from folio.models import DocumentPlan, JobInput
from folio.visuals.workflow import load_visuals


def reformat_job(store, job_id: str, output: Path, *, require_pagination=False):
    job = store.get(job_id)
    if job["status"] not in {"published", "draft", "needs_confirmation"} or not job["plan"]:
        raise ValueError("Only completed document jobs can be reformatted")
    request = JobInput.model_validate(job["input"])
    plan = DocumentPlan.model_validate(job["plan"])
    states = store.states(job_id)
    references = ReferenceDocuments(request.reference_paths, request.reference_revision_view)
    output.mkdir(parents=True, exist_ok=True)
    target = output / ("Document-reformatted.docx" if request.language == "en" else "需求说明书-排版修正版.docx")
    if job.get("artifact") and target.resolve() == Path(job["artifact"]).resolve():
        raise ValueError("Choose an output directory separate from the original artifact")
    pdf = target.with_suffix(".pdf")
    layout = render(
        target,
        plan,
        states,
        title=plan.document_title or request.title,
        draft=job["status"] != "published",
        demo=request.runtime == "demo",
        cover=references.cover_values() | request.cover,
        comments=references.comments(),
        include_evidence_appendix=request.include_evidence_appendix,
        pdf=pdf,
        require_pagination=require_pagination,
        figures=load_visuals(store.root / "jobs" / job_id, states),
        defer_pagination=True,
        language=request.language,
    )
    original = target.read_bytes()
    try:
        formatting = replay_formatting(
            target, plan, states, store.root / "jobs" / job_id / "formatting.json"
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        target.write_bytes(original)
        formatting = {"status": "failed", "changed": False, "error_type": type(exc).__name__}
    if layout["status"] == "deferred":
        try:
            layout = paginate(target, pdf=pdf, required=require_pagination)
        except Exception:
            if not formatting["changed"]:
                raise
            target.write_bytes(original)
            formatting.update(status="discarded", changed=False, reason="pagination_failed")
            layout = paginate(target, pdf=pdf, required=require_pagination)
    layout["formatting"] = formatting
    # Page-review baselines contain the original stable PAGEREF values. Recreate
    # those before checking the full package signature; never guess around a
    # different figure, typography, or body revision.
    before_page_review = target.read_bytes()
    try:
        page_review = replay_page_review(
            target, plan, states, store.root / "jobs" / job_id / "layout-review.json"
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        target.write_bytes(before_page_review)
        page_review = {
            "status": "failed",
            "changed": False,
            "visual_checked": False,
            "error_type": type(exc).__name__,
        }
    if page_review["changed"]:
        try:
            layout = paginate(target, pdf=pdf, required=require_pagination)
        except Exception:
            target.write_bytes(before_page_review)
            page_review.update(status="discarded", changed=False, reason="pagination_failed")
            layout = paginate(target, pdf=pdf, required=require_pagination)
    layout["formatting"] = formatting
    layout["page_review"] = page_review
    validate_template_output(target, plan)
    report = {
        "job_id": job_id,
        "artifact": str(target.resolve()),
        "pdf": str(pdf.resolve()) if layout["status"] == "paginated" else None,
        "layout": layout,
        "model_calls": 0,
        "business_state_changed": False,
        "provenance": provenance_summary(states),
    }
    (output / ("layout-inspection.json" if request.language == "en" else "排版检查.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
