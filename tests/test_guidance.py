"""Document preferences must stay isolated, bounded and out of the evidence ledger."""

import json

import pytest
from pydantic import ValidationError

from folio.document.guidance import (
    WritingGuide,
    guidance_context,
    guidance_schema,
    load_guide,
)
from folio.models import JobInput, Review
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult


@pytest.mark.parametrize("identifier", ["urd", "srs"])
def test_projected_guides_are_small_isolated_and_idempotent(identifier):
    guide = load_guide(identifier)
    original = guide.model_dump()
    context = {"section": {"section_id": "a"}, "evidence": [{"text": "真实材料"}]}
    projected = guidance_context("generate", context, guide)
    assert guide.name == {"urd": "用户需求说明书", "srs": "软件需求规格说明书"}[identifier]
    assert projected["writing_guidance"]["name"] == {
        "urd": "User Requirements Document", "srs": "Software Requirements Specification",
    }[identifier]
    assert len(json.dumps(projected["writing_guidance"], ensure_ascii=False)) < 4000
    assert guidance_context("generate", projected, guide) == projected
    assert projected["evidence"] == context["evidence"]
    for key in ("avoid", "visuals", "source_policy"):
        projected["writing_guidance"][key].append("仅修改本次请求")
    next(iter(projected["writing_guidance"]["focus"].values())).append("仅修改本次请求")
    assert guide.model_dump() == original
    assert "writing_guidance" not in context


def test_legacy_context_and_action_specific_views():
    context = {"evidence": [{"text": "业务依据"}]}
    assert guidance_context("generate", context, None) is context
    guide = load_guide("srs")
    writing = guidance_context("generate", context, guide)
    review = guidance_context("review", writing, guide)
    assert "focus" not in review["writing_guidance"]
    assert "writing" not in review["writing_guidance"]
    assert "avoid" not in review["writing_guidance"]
    assert guidance_context("extract", writing, guide) == context
    query = guidance_context("query", context, guide)
    assert "focus" in query["writing_guidance"]
    assert "visuals" not in query["writing_guidance"]


@pytest.mark.parametrize("field", ["extra_review", "retry_for_guidance", "block_delivery"])
def test_config_cannot_turn_preferences_into_delivery_gates(field):
    data = load_guide("urd").model_dump()
    data["guidance_policy"][field] = True
    with pytest.raises(ValidationError):
        WritingGuide.model_validate(data)


def test_unknown_guide_cannot_select_an_arbitrary_path():
    with pytest.raises(ValueError, match="Unknown document type"):
        load_guide("../../settings/model")


def test_review_attribution_is_explicit_in_measured_and_sent_schema():
    schema = Review.model_json_schema()
    assert "basis" not in schema["$defs"]["Finding"]["required"]
    context = guidance_context("review", {}, load_guide("srs"))
    guided = guidance_schema(schema, context)
    assert guided["$defs"]["Finding"]["required"].count("basis") == 1
    assert guidance_schema(guided, context) == guided
    assert guidance_schema(schema, {}) is schema
    assert "basis" not in schema["$defs"]["Finding"]["required"]


async def test_snapshot_resume_and_cache_isolation(store, inputs, monkeypatch):
    class Runtime:
        calls = []
        measured = None

        async def measure(self, task, context, schema, limit):
            assert "basis" in schema["$defs"]["Finding"]["required"]
            self.measured = (context, schema)
            return {"fits": True}

        async def complete(self, task, context, schema, session):
            assert self.measured == (context, schema)
            self.calls.append(context["writing_guidance"])
            return RuntimeResult('{"findings": []}')

    request = JobInput(**inputs, writing_guide=load_guide("urd"))
    job_id = store.create(request)
    snapshot = store.root / "jobs" / job_id / "inputs" / "writing-guide.json"
    assert json.loads(snapshot.read_text()) == request.writing_guide.model_dump()
    # Resuming must not reload the editable configuration file.
    monkeypatch.setattr("folio.document.guidance.load_guide", lambda _: pytest.fail("reloaded"))
    runtime = Runtime()
    engine = Engine(store, runtime)
    engine.job_id = job_id
    engine.request = JobInput.model_validate(store.get(job_id)["input"])
    engine.limits = engine.request.limits
    context = {"sections": [{"section_id": "a"}]}
    await engine._call("review", context, Review)
    await engine._call("review", context, Review)
    assert len(runtime.calls) == 1

    # Even an edit without bumping version changes the snapshot/cache identity.
    engine.request.writing_guide.purpose = "修改后的写作目的"
    await engine._call("review", context, Review)
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["snapshot"] != runtime.calls[1]["snapshot"]
    assert json.loads(snapshot.read_text())["purpose"] != engine.request.writing_guide.purpose
