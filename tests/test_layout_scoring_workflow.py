"""Standalone scoring contracts and Folio exports without automatic page scoring."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_layout_review import preview
from test_layout_review import source as source_fixture

from folio.document.formatting import LayoutPatch
from folio.document.layout_scoring import PageLayoutScore
from folio.document.template_contract import TemplateContract, TemplateSection
from folio.models import Finding, JobInput
from folio.orchestration.engine import Engine
from folio.orchestration.layout_review import PageLayoutReview, review_rendered_document
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallBudgetExceeded


@pytest.fixture(name="source")
def source_document(tmp_path):
    return source_fixture.__wrapped__(tmp_path)


def pages_renderer(counts):
    passes = []

    def render(path, directory, *, max_pages):
        result = preview(path, directory, max_pages=max_pages)
        count = counts[min(len(passes), len(counts) - 1)]
        passes.append(count)
        result["total_pages"] = count
        result["pages"] = [
            result["pages"][0] | {"page": number} for number in range(1, min(max_pages, count) + 1)
        ]
        result["layout"]["total_pages"] = count
        return result

    return render, passes


async def review(source, folder, score, correct, **kwargs):
    path, plan, states = source
    return await review_rendered_document(
        path,
        plan,
        states,
        folder,
        correct,
        lambda _: None,
        enabled=True,
        supports_images=True,
        score_call=score,
        **kwargs,
    )


async def forbidden(*_args):
    pytest.fail("No correction call is needed")


async def test_every_page_is_scored_and_exactly_eight_passes(source, tmp_path):
    renderer, passes = pages_renderer([3])
    contexts = []

    async def score(task, context, model):
        assert task == "score_layout" and model is PageLayoutScore
        contexts.append(context)
        assert len(context["page_images"]) == 1
        assert Path(context["page_images"][0]["path"]).is_file()
        return model(page=context["page_number"], score=8)

    result = await review(source, tmp_path, score, forbidden, renderer=renderer)
    assert result["status"] == "passed" and result["visual_checked"]
    assert result["score_calls"] == result["calls"] == 3
    assert result["correction_calls"] == 0 and passes == [3]
    assert [item["page_number"] for item in contexts] == [1, 2, 3]
    assert result["delivered_round"] == 0
    assert all(grade["passed"] and grade["image_sha256"] for grade in result["page_scores"])
    assert (
        json.loads((tmp_path / "layout-review.json").read_text())["page_scores"]
        == result["page_scores"]
    )


async def test_only_failed_pages_corrected_but_every_new_page_rescored(source, tmp_path):
    renderer, passes = pages_renderer([3, 4])
    seen = []
    corrections = []

    async def score(task, context, model):
        page = context["page_number"]
        seen.append((len(passes), page))
        return model(page=page, score=7.99 if len(passes) == 1 and page == 2 else 9)

    async def correct(task, context, model):
        assert task == "review_layout"
        corrections.append(context)
        assert [image["page"] for image in context["page_images"]] == [2]
        assert context["page_scores"][0]["score"] == 7.99
        return PageLayoutReview(
            patches=[
                LayoutPatch(
                    target_id=context["targets"][0]["target_id"],
                    space_after_pt=9,
                )
            ]
        )

    result = await review(source, tmp_path, score, correct, renderer=renderer)
    assert result["status"] == "passed" and result["changed"]
    assert seen == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (2, 4)]
    assert len(corrections) == 1 and result["score_calls"] == 7
    assert result["correction_calls"] == 1 and result["calls"] == 8
    assert result["delivered_round"] == 1 and len(result["page_scores"]) == 4


async def test_three_corrections_max_and_failed_candidates_never_replace_original(source, tmp_path):
    original = source[0].read_bytes()
    renderer, passes = pages_renderer([1])
    count = 0

    async def score(task, context, model):
        return model(page=context["page_number"], score=7)

    async def correct(task, context, model):
        nonlocal count
        count += 1
        assert context["correction_round"] == count
        return model(
            patches=[
                LayoutPatch(
                    target_id=context["targets"][0]["target_id"],
                    space_after_pt=[9, 12, 6][count - 1],
                )
            ]
        )

    result = await review(source, tmp_path, score, correct, renderer=renderer, max_rounds=99)
    assert result["status"] == "needs_attention" and result["reason"] == "score_below_threshold"
    assert result["score_calls"] == 4 and result["correction_calls"] == 3
    assert len(passes) == 4 and count == 3
    assert result["delivered_round"] == 0 and not result["changed"]
    assert source[0].read_bytes() == original
    assert len(result["rounds"]) == 4
    assert result["page_scores"] == result["rounds"][0]["page_scores"]
    assert all(round_["complete"] for round_ in result["rounds"])


async def test_global_budget_exhaustion_preserves_partial_scores_and_stops(source, tmp_path):
    renderer, _ = pages_renderer([3])
    count = 0

    async def score(task, context, model):
        nonlocal count
        count += 1
        if count == 2:
            raise ModelCallBudgetExceeded(3500, 3500)
        return model(page=context["page_number"], score=9)

    with pytest.raises(ModelCallBudgetExceeded):
        await review(source, tmp_path, score, forbidden, renderer=renderer)
    result = json.loads((tmp_path / "layout-review.json").read_text())
    assert result["status"] == "failed" and not result["visual_checked"]
    assert result["error_type"] == "ModelCallBudgetExceeded" and count == 2
    assert [item["page"] for item in result["page_scores"]] == [1]
    assert result["rounds"][0]["remaining_pages"] == 2


async def test_no_correction_when_full_recheck_cannot_fit(source, tmp_path):
    renderer, passes = pages_renderer([2])

    async def score(task, context, model):
        return model(page=context["page_number"], score=6)

    result = await review(source, tmp_path, score, forbidden, renderer=renderer, max_calls=4)
    assert result["status"] == "partial" and result["reason"] == "recheck_call_limit"
    assert result["correction_calls"] == 0 and passes == [2]


@pytest.mark.parametrize(
    "bad",
    [{"page": 2, "score": 8}, {"page": 1, "score": 10.01}, {"page": 1, "score": float("nan")}],
)
async def test_invalid_score_never_claims_pass(source, tmp_path, bad):
    renderer, _ = pages_renderer([1])

    async def score(*_args):
        return bad

    result = await review(source, tmp_path, score, forbidden, renderer=renderer)
    assert result["status"] == "failed" and not result["visual_checked"]
    assert not result["page_scores"]


async def test_template_and_user_style_reach_both_evaluators(source, tmp_path):
    path, plan, states = source
    plan.template = TemplateContract(
        path="/private/template.docx",
        name="模板",
        source_hash="hash",
        sections=[TemplateSection(section_id="s1", title="审批规则", level=1, body_index=0)],
    )
    profile = {"style_markdown": "遵循用户示例，保持专业整洁。"}
    renderer, passes = pages_renderer([1])
    references = []

    async def score(task, context, model):
        references.append(context["style_reference"])
        return model(page=1, score=7 if len(passes) == 1 else 8)

    async def correct(task, context, model):
        references.append(context["style_reference"])
        return model(
            patches=[
                LayoutPatch(
                    target_id=context["targets"][0]["target_id"],
                    keep_together=True,
                )
            ]
        )

    result = await review(
        (path, plan, states), tmp_path, score, correct, renderer=renderer, writing_profile=profile
    )
    assert result["status"] == "passed"
    assert len(references) == 3 and references[0] == references[1] == references[2]
    assert references[0]["style_markdown"] == profile["style_markdown"]
    assert references[0]["template_requirements"]["sections"][0]["title"] == "审批规则"
    assert "/private" not in json.dumps(references)


async def test_engine_ignores_legacy_scoring_flag_and_uses_normal_formatter(
    store, inputs, monkeypatch
):
    renderer, passes = pages_renderer([2])
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)
    monkeypatch.setattr("folio.orchestration.layout_review.prepare_page_preview", renderer)

    class FormattingRuntime(DemoRuntime):
        config = {"supports_image_input": True, "layout_review_enabled": True}

        async def complete(self, task, context, schema, key):
            assert task not in {"score_layout", "review_layout"}
            if task == "format_document":
                return RuntimeResult(json.dumps({
                    "patches": [], "done": True, "summary": "保留现有排版",
                }))
            result = await super().complete(task, context, schema, key)
            if task == "work_plan":
                tasks = json.loads(result.content)["tasks"]
                combined = tasks[0] | {
                    "section_ids": [sid for item in tasks for sid in item["section_ids"]]
                }
                return RuntimeResult(json.dumps({"tasks": [combined]}))
            return result

    job_id = store.create(JobInput(**inputs, runtime="llm"))
    job = await Engine(store, FormattingRuntime()).run(job_id)
    assert job["status"] == "published" and passes == []
    assert "page_review" not in job["snapshot"]
    folder = Path(job["artifact"]).parent
    assert not (folder / "layout-review.json").exists()
    formatting = json.loads((folder / "formatting.json").read_text())
    assert formatting["status"] == "complete"
    assert sum(group["calls"] for group in formatting["groups"]) > 0
    with store.connect() as connection:
        calls = connection.execute(
            "SELECT task_key FROM tasks WHERE job_id = ?", (job_id,)
        ).fetchall()
    assert not any(
        row["task_key"].startswith(("score_layout:", "review_layout:")) for row in calls
    )
    assert any(row["task_key"].startswith("format_document:") for row in calls)


@pytest.mark.parametrize(
    ("business_issue", "review_interrupted"), [(False, False), (True, False), (False, True)]
)
async def test_reexport_retires_old_page_blockers_but_keeps_history_and_business_issues(
    store, inputs, monkeypatch, business_issue, review_interrupted
):
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)
    page_review = AsyncMock(side_effect=AssertionError("old scores must not be resumed"))
    monkeypatch.setattr(
        "folio.orchestration.layout_review.review_rendered_document", page_review
    )
    runtime = DemoRuntime()
    runtime.config = {"supports_image_input": True, "layout_review_enabled": True}
    job_id = store.create(JobInput(**inputs, runtime="llm"))

    class InitialDraftEngine(Engine):
        async def finish(self, states, **kwargs):
            return await super().finish(states, **(kwargs | {"draft": True}))

    engine = InitialDraftEngine(store, runtime)
    job = await engine.run(job_id)
    states = store.states(job_id)
    old_report = {"status": "needs_attention", "page_scores": [{"page": 1, "score": 7}]}
    folder = Path(job["artifact"]).parent
    report_path = folder / "layout-review.json"
    report_path.write_text(json.dumps(old_report), encoding="utf-8")
    historical_bytes = report_path.read_bytes()
    old_issue = Finding(
        kind="pending", key="page-layout:old", description="旧页面评分未通过",
        section_ids=[states[0].section_id],
    ).model_dump()
    other_issue = Finding(
        kind="pending", key="work-gap:business", description="业务审批角色仍待确认",
        section_ids=[states[0].section_id],
    ).model_dump()
    store.update(
        job_id, status="running", stage="formatting",
        snapshot=job["snapshot"] | {
            "page_review": old_report, "review_passed": False,
            "blocking_issues": [old_issue, *([other_issue] if business_issue else [])],
            **({"review_interruption": {"code": "connection"}} if review_interrupted else {}),
        },
    )
    runtime.complete = AsyncMock(side_effect=AssertionError("local export makes no model calls"))
    needs_confirmation = business_issue or review_interrupted
    await Engine.finish(
        engine, states, draft=needs_confirmation, needs_confirmation=needs_confirmation,
        allow_model_calls=False,
    )
    final = store.get(job_id)
    assert final["status"] == ("needs_confirmation" if needs_confirmation else "published")
    assert final["snapshot"]["blocking_issues"] == ([other_issue] if business_issue else [])
    assert final["snapshot"]["review_passed"] is not needs_confirmation
    assert "page_review" not in final["snapshot"]
    assert "page_review" not in json.loads((folder / "layout.json").read_text())
    assert report_path.read_bytes() == historical_bytes
    runtime.complete.assert_not_awaited()
    page_review.assert_not_awaited()
