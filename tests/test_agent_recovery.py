"""Exercise real coordinator boundaries with offline model substitutes."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from docx import Document
from test_request_context import engine_for

from folio.api.workspace import job_events
from folio.models import DrillDown, Finding, JobInput, Review
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted


@pytest.fixture(autouse=True)
def no_office(monkeypatch):
    monkeypatch.setattr("folio.document.pagination.office_runtime", lambda: None)


@pytest.mark.parametrize("method", ["_call", "call"])
async def test_completed_request_replays_without_online_capacity_service(store, inputs, method):
    runtime = DemoRuntime()
    engine = engine_for(store, inputs, runtime)
    context = {"sections": []}
    expected = await getattr(engine, method)("review", context, Review)
    used = store.get(engine.job_id)["calls"]
    runtime.measure = AsyncMock(side_effect=ModelCallFailed("offline", "connection"))
    runtime.complete = AsyncMock(side_effect=AssertionError("cache must be sufficient"))
    assert await getattr(engine, method)("review", context, Review) == expected
    assert store.get(engine.job_id)["calls"] == used
    runtime.measure.assert_not_awaited()
    runtime.complete.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ModelCallFailed("provider unavailable", "connection"),
        TimeoutError(),
        TaskRetriesExhausted("review:test", "service unavailable"),
    ],
)
async def test_service_interruption_preserves_full_body_without_extra_calls(store, inputs, error):
    class Interrupted(Engine):
        async def global_review(self, states):
            self.saved = [s.model_dump() for s in self.store.states(self.job_id)]
            self.used = self.store.get(self.job_id)["calls"]
            self.runtime.complete = AsyncMock(side_effect=AssertionError("no more model calls"))
            raise error

    engine = Interrupted(store, DemoRuntime())
    job_id = store.create(JobInput(**inputs, runtime="llm"))
    job = await engine.run(job_id)
    assert job["status"] == "needs_confirmation"
    assert job["snapshot"]["review_passed"] is False
    assert job["snapshot"]["review_interruption"]["error_type"] == type(error).__name__
    assert [s.model_dump() for s in store.states(job_id)] == engine.saved
    assert job["calls"] == engine.used
    engine.runtime.complete.assert_not_awaited()
    text = "\n".join(p.text for p in Document(job["artifact"]).paragraphs)
    for state in store.states(job_id):
        assert all(b.text in text for b in state.content.blocks if b.text)
    assert Path(job["artifact"]).name == "draft.docx"


async def test_programming_failure_does_not_become_completed_draft(store, inputs):
    class Broken(Engine):
        async def global_review(self, states):
            raise ValueError("invalid internal state")

    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(ValueError, match="invalid internal state"):
        await Broken(store, DemoRuntime()).run(job_id)
    job = store.get(job_id)
    assert job["status"] == "failed" and not job["artifact"]
    assert store.states(job_id)


@pytest.mark.parametrize("runtime_kind", ["demo", "llm"])
@pytest.mark.parametrize("allow_model_calls", [False, True])
async def test_legacy_page_review_setting_does_not_replace_normal_formatting(
    store, inputs, monkeypatch, runtime_kind, allow_model_calls
):
    page_review = AsyncMock(side_effect=AssertionError("page scoring has been retired"))
    monkeypatch.setattr(
        "folio.orchestration.layout_review.review_rendered_document", page_review
    )
    formatter = AsyncMock(return_value={"status": "complete", "changed": False})
    monkeypatch.setattr("folio.orchestration.formatting.format_document", formatter)
    runtime = DemoRuntime()
    runtime.config = {"supports_image_input": True, "layout_review_enabled": True}

    class ExportEngine(Engine):
        async def finish(self, states, **kwargs):
            return await super().finish(
                states, **(kwargs | {"allow_model_calls": allow_model_calls})
            )

    engine = ExportEngine(store, runtime)
    job_id = store.create(JobInput(**inputs, runtime=runtime_kind))
    job = await engine.run(job_id)
    page_review.assert_not_awaited()
    formatter.assert_awaited_once()
    assert formatter.call_args.kwargs["enabled"] is (
        runtime_kind == "llm" and allow_model_calls
    )
    if runtime_kind == "demo":
        assert formatter.call_args.kwargs["disabled_reason"] == "offline_demo"
    elif not allow_model_calls:
        assert formatter.call_args.kwargs["disabled_reason"] == "model_calls_disabled"
    assert job["status"] == ("draft" if runtime_kind == "demo" else "published")
    assert job["snapshot"]["review_passed"] is True
    assert "page_review" not in job["snapshot"]
    folder = Path(job["artifact"]).parent
    assert not (folder / "layout-review.json").exists()
    assert "page_review" not in json.loads((folder / "layout.json").read_text())
    events = "".join([event async for event in job_events(store, job_id, after=10**9)])
    assert "artifact.ready" in events and '"layout_note"' in events
    assert "Jev" not in events


async def test_readonly_exhausted_chapter_does_not_block_another_chapters_repair(store, inputs):
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    initial = Engine(store, DemoRuntime())
    await initial.run(job_id)
    states = store.states(job_id)
    readonly, target = states
    readonly.revision = initial.limits.max_repairs + 1
    store.save_section(job_id, readonly)
    store.update(job_id, status="failed", stage="review")
    store.resume_job(job_id)
    question = Finding(
        kind="conflict",
        key="scope-check",
        description="只需修改第二节的表达",
        section_ids=[readonly.section_id, target.section_id],
    )
    engine = Engine(store, DemoRuntime())
    engine.global_review = AsyncMock(side_effect=[[question], []])
    engine.business_findings = AsyncMock(return_value=[])
    engine.adjudicate_findings = AsyncMock(
        return_value=[
            (
                question,
                DrillDown(
                    confirmed=True,
                    reason="第一节为正确参考",
                    affected_section_ids=[target.section_id],
                    repair_instruction="修正第二节",
                ),
                None,
            )
        ]
    )
    engine.generate = AsyncMock(
        side_effect=lambda section, previous, *_: previous.model_copy(
            update={"revision": previous.revision + 1}
        )
    )
    # Inspect the coordinator outcome without requiring a fresh mocked task review.
    engine.finish = AsyncMock()
    await engine.run(job_id)
    engine.generate.assert_awaited_once()
    assert {s.section_id: s.revision for s in store.states(job_id)} == {
        readonly.section_id: readonly.revision,
        target.section_id: target.revision + 1,
    }
