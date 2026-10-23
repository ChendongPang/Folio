"""The preflight contract must be the exact contract sent to the model."""

from copy import deepcopy

import pytest

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Extraction, JobInput, LocalReview
from folio.orchestration.context import ContextSelection
from folio.orchestration.engine import Engine
from folio.orchestration.work import GapCompletion
from folio.runtime.demo import DemoRuntime
from folio.runtime.llm import LLMRuntime


class RecordingRuntime(DemoRuntime):
    """Use the production wire serializer without sending network requests."""

    def __init__(self, wire):
        self.wire = wire
        self.measured = []
        self.sent = []

    def prompt_chars(self, task, context, schema):
        return self.wire.prompt_chars(task, context, schema)

    async def measure(self, task, context, schema, char_limit):
        size = self.prompt_chars(task, context, schema)
        self.measured.append((task, deepcopy(context), deepcopy(schema), size, char_limit))
        return {"characters": size, "fits": size <= char_limit}

    async def complete(self, task, context, schema, session):
        messages, wire_schema, *_ = self.wire.request_parts(task, context, schema)
        size = sum(len(message["content"]) for message in messages)
        matching = [call for call in self.measured if call[:3] == (task, context, schema)]
        assert matching, "No preflight measured the actual request contract"
        assert size == matching[-1][3] <= matching[-1][4]
        self.sent.append((task, deepcopy(context), deepcopy(schema), wire_schema, size))
        return await super().complete(task, context, schema, session)


@pytest.fixture
async def measured_engine(store, inputs, tmp_path):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True))
    wire = LLMRuntime(settings.path)
    engine = Engine(store, RecordingRuntime(wire))
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    try:
        yield engine
    finally:
        await wire.aclose()


def request_case(task):
    evidence = [{"evidence_id": "source_A", "text": "必须审批", "source_id": "document"}]
    section = {"section_id": "section_A", "title": "审批"}
    blocks = [{"block_id": "block_A", "text": "必须审批", "evidence_ids": ["source_A"]}]
    if task == "complete_gaps":
        return (
            GapCompletion,
            {
                "sections": [section],
                "gaps": [{"gap_id": "gap_A", "question": "审批规则"}],
            },
            [("GapProposal", "gap_id", ["gap_A"]), ("GapProposal", "section_ids", ["section_A"])],
        )
    if task == "select_context":
        return (
            ContextSelection,
            {
                "catalog": [
                    {"context_ref": "fact_A", "kind": "fact"},
                    {"context_ref": "risk_A", "kind": "risk"},
                ]
            },
            [(None, "selected_refs", ["fact_A"]), (None, "risk_refs", ["risk_A"])],
        )
    common = {"section": section, "content": {"blocks": blocks}, "evidence": evidence}
    if task == "extract":
        return (
            Extraction,
            common,
            [("Claim", "block_ids", ["block_A"]), ("Claim", "evidence_ids", ["source_A"])],
        )
    common["extraction"] = {
        "summary": "审批",
        "claims": [
            {
                "claim_id": "claim_A",
                "statement": "必须审批",
                "evidence_ids": ["source_A"],
                "block_ids": ["block_A"],
            }
        ],
    }
    return (
        LocalReview,
        common,
        [("Verdict", "claim_id", ["claim_A"]), ("Finding", "section_ids", ["section_A"])],
    )


@pytest.mark.parametrize("task", ["complete_gaps", "select_context", "extract", "validate"])
async def test_preflight_measures_all_dynamic_enums_sent_to_runtime(measured_engine, task):
    engine = measured_engine
    model, context, enums = request_case(task)
    before = deepcopy(context)
    capacity = await engine.measure_request(task, context, model)
    measured_schema = engine.runtime.measured[-1][2]
    for definition, field, expected in enums:
        fields = measured_schema["$defs"][definition] if definition else measured_schema
        value = fields["properties"][field]
        assert value.get("items", value)["enum"] == expected
    await engine._call(task, context, model)
    sent = engine.runtime.sent[-1]
    assert sent[2] == measured_schema
    assert sent[4] == capacity["characters"]
    assert context == before
    # Passing the already-built schema must not append required fields twice.
    await engine.measure_request(task, context, model, measured_schema)
    assert engine.runtime.measured[-1][2] == sent[2]


async def test_gap_schema_growth_partitions_before_any_over_budget_send(measured_engine):
    engine = measured_engine
    context = {
        "sections": [{"section_id": "section_" + "s" * 100, "title": "审批"}],
        "gaps": [
            {"gap_id": f"gap_{i}_" + "g" * 100, "question": "缺口业务规则" * 500} for i in range(2)
        ],
    }
    effective = engine.request_context("complete_gaps", context)
    old_size = engine.runtime.prompt_chars(
        "complete_gaps", effective, GapCompletion.model_json_schema()
    )
    actual = await engine.measure_request("complete_gaps", context, GapCompletion)
    assert old_size < actual["characters"]
    limit = (old_size + actual["characters"]) // 2
    assert old_size <= limit < actual["characters"]
    engine.limits = engine.limits.model_copy(update={"max_prompt_chars": limit})

    result = await engine.call_bounded("complete_gaps", context, GapCompletion)

    assert {proposal.gap_id for proposal in result.proposals} == {
        gap["gap_id"] for gap in context["gaps"]
    }
    sent = [call for call in engine.runtime.sent if call[0] == "complete_gaps"]
    assert len(sent) == 2
    assert all(len(call[1]["gaps"]) == 1 and call[4] <= limit for call in sent)
