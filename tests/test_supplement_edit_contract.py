import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from test_request_context import engine_for
from test_supplement_integration import current_content, proposal, source_context

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Block, Limits
from folio.orchestration.supplement_integration import SupplementIntegration
from folio.runtime.base import RuntimeResult
from folio.runtime.business_transport import business_payload, translate_ids
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed
from folio.runtime.llm import LLMRuntime


def context():
    return source_context() | {
        "integration_item": proposal(),
        "existing_evidence_ids": ["source_alpha", "source_beta"],
    }


def test_edit_schema_limits_deletion_and_resolved_questions_without_losing_citations(store, inputs):
    engine = engine_for(store, inputs, DemoRuntime())
    schema = engine.request_schema("integrate_supplement", context(), SupplementIntegration)
    props = schema["properties"]
    assert props["remove_block_ids"]["items"]["enum"] == ["old", "duplicate"]
    assert props["resolved_pending_items"]["items"]["enum"] == current_content().pending_items
    for branch in schema["$defs"]["Block"]["anyOf"]:
        fields = branch["properties"]
        ids = set(fields["block_id"]["enum"])
        if "si1" in ids:
            assert "si20" in ids and not ids & {"fact", "table", "old"}
            assert fields["origin"]["const"] == "model_proposal"
            assert fields["evidence_ids"]["maxItems"] == 0
        else:
            assert {"fact", "table", "old"} <= ids
            assert set(fields["evidence_ids"]["items"]["enum"]) == {"source_alpha", "source_beta"}


@pytest.mark.parametrize("evidence_refresh", [False, True])
def test_new_block_provenance_contract_matches_the_integration_mode(
    store, inputs, evidence_refresh
):
    engine = engine_for(store, inputs, DemoRuntime())
    source = context()
    source["integration_item"]["evidence_refresh"] = evidence_refresh
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    for branch in schema["$defs"]["Block"]["anyOf"]:
        if "si1" not in branch["properties"]["block_id"]["enum"]:
            continue
        description = branch["properties"]["block_id"]["description"]
        if evidence_refresh:
            assert (
                "origin=evidence" in description
                and "exact IDs from this call's evidence" in description
            )
            assert (
                "origin=model_proposal" not in description and "evidence_ids=[]" not in description
            )
        else:
            assert "origin=model_proposal with evidence_ids=[]" in description
    added = Block(
        block_id="si1",
        text="未经审批不得继续处理。",
        origin="evidence" if evidence_refresh else "model_proposal",
        evidence_ids=["source_alpha"] if evidence_refresh else [],
    )
    retained = current_content().blocks[0]
    engine.check_result(SupplementIntegration(updates=[retained, added]), source)
    invalid = added.model_copy(
        update={
            "origin": "evidence",
            "evidence_ids": [] if evidence_refresh else ["source_alpha"],
        }
    )
    with pytest.raises(
        ValueError, match="must cite original evidence|must retain model provenance"
    ):
        engine.check_result(SupplementIntegration(updates=[invalid]), source)


@pytest.mark.parametrize("evidence_refresh", [False, True])
@pytest.mark.parametrize("kind", ["paragraph", "heading", "table"])
def test_wire_keeps_existing_facts_and_separately_constrains_new_provenance(
    store, inputs, evidence_refresh, kind
):
    engine = engine_for(store, inputs, DemoRuntime())
    source = context()
    source["integration_item"]["evidence_refresh"] = evidence_refresh
    # Existing facts may keep sources outside this one item's selected originals.
    source["evidence"] = [source["evidence"][0]]
    before = deepcopy(source)
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    _, wire_schema, refs = business_payload(source, schema)
    Draft202012Validator.check_schema(wire_schema)
    wire_validator = Draft202012Validator(wire_schema)
    old_table = current_content().blocks[1].model_dump()
    assert wire_validator.is_valid({"updates": [old_table]})
    engine.check_result(SupplementIntegration(updates=[Block.model_validate(old_table)]), source)

    added = {
        "block_id": "si1",
        "kind": kind,
        "origin": "evidence" if evidence_refresh else "model_proposal",
        **(
            {"rows": [["条件", "结果"], ["未审批", "不得处理"]]}
            if kind == "table"
            else {"text": "未经审批不得继续处理。"}
        ),
    }
    if evidence_refresh:
        added["evidence_ids"] = ["source_alpha"]
    # Empty arrays are omitted on the real wire, then supplied by Block's default.
    valid = {"updates": [added]}
    assert wire_validator.is_valid(valid)
    restored = SupplementIntegration.model_validate(translate_ids(valid, refs))
    engine.check_result(restored, source)
    assert restored.updates[0].evidence_ids == (["source_alpha"] if evidence_refresh else [])
    invalid = deepcopy(valid)
    invalid["updates"][0]["origin"] = "model_proposal" if evidence_refresh else "evidence"
    assert not wire_validator.is_valid(invalid)
    for bad_refs in (["source_beta"], ["unknown"]):
        invalid = deepcopy(valid)
        invalid["updates"][0]["evidence_ids"] = bad_refs
        assert not wire_validator.is_valid(invalid)
    if evidence_refresh:
        for bad_refs in ([], None):
            invalid = deepcopy(valid)
            if bad_refs is None:
                invalid["updates"][0].pop("evidence_ids")
            else:
                invalid["updates"][0]["evidence_ids"] = bad_refs
            assert not wire_validator.is_valid(invalid)
    assert source == before


def test_new_retrieved_refs_stay_narrow_after_repeated_schema_encoding_and_aliasing(store, inputs):
    engine = engine_for(store, inputs, DemoRuntime())
    source = context()
    source["integration_item"]["evidence_refresh"] = True
    aliases = {"source_alpha": "source_" + "a" * 60, "source_beta": "source_" + "b" * 60}
    source = translate_ids(source, aliases)
    source["evidence"] = [source["evidence"][0]]
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    _, wire, reverse = business_payload(source, schema)
    for branch in wire["$defs"]["Block"]["anyOf"]:
        fields = branch["properties"]
        actual = {reverse.get(ref, ref) for ref in fields["evidence_ids"]["items"]["enum"]}
        assert actual == (
            {aliases["source_alpha"]}
            if "si1" in fields["block_id"]["enum"]
            else set(aliases.values())
        )
    # Running canonical source restriction again must not reopen the new branch.
    from folio.runtime.business_transport import evidence_schema

    assert evidence_schema(schema, source) == schema


@pytest.mark.parametrize("empty_current", [False, True])
def test_refresh_without_originals_never_offers_new_fact_blocks(store, inputs, empty_current):
    engine = engine_for(store, inputs, DemoRuntime())
    source = context()
    source["integration_item"]["evidence_refresh"] = True
    source["evidence"] = []
    if empty_current:
        source["current"]["blocks"] = []
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    _, wire, _ = business_payload(source, schema)
    validator = Draft202012Validator(wire)
    assert validator.is_valid({})
    assert not validator.is_valid(
        {
            "updates": [
                {
                    "block_id": "si1",
                    "text": "新事实",
                    "origin": "evidence",
                    "evidence_ids": ["source_alpha"],
                }
            ]
        }
    )


async def test_split_edit_contract_is_measured_and_used_in_real_wire_path(store, inputs, tmp_path):
    settings = ModelSettingsStore(tmp_path / "model")
    settings.save(ModelSettingsInput(base_url="http://unused.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    runtime.capacity.ready = True  # No external tokenizer or model calls.
    engine = engine_for(store, inputs, runtime)
    source = engine.request_context("integrate_supplement", context())
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    expected_messages, expected_schema, *_ = runtime.request_parts(
        "integrate_supplement", source, schema
    )
    received = []

    async def stream(messages, *, schema, **kwargs):
        received.append((messages, schema))
        output = {
            "updates": [
                {"block_id": "si1", "text": "应支持审批拒绝后的查询。", "origin": "model_proposal"}
            ]
        }
        assert Draft202012Validator(schema).is_valid(output)
        yield "completed", {"text": json.dumps(output), "usage": {}}

    runtime.stream = stream
    try:
        measured = await engine.measure_request(
            "integrate_supplement", source, SupplementIntegration
        )
        assert measured["characters"] == sum(
            len(message["content"]) for message in expected_messages
        )
        result = await engine._call("integrate_supplement", source, SupplementIntegration)
        assert received == [(expected_messages, expected_schema)]
        assert result.updates[0].origin == "model_proposal" and result.updates[0].evidence_ids == []
        smaller = engine.limits.model_copy(update={"max_prompt_chars": measured["characters"] - 1})
        engine.limits = smaller
        # Runtime token measurement replaces the old character fallback.
        assert (
            await engine.measure_request("integrate_supplement", source, SupplementIntegration)
        )["fits"]
        runtime.config["max_input_tokens"] = measured["estimated_input_tokens"] - 1
        assert not (
            await engine.measure_request("integrate_supplement", source, SupplementIntegration)
        )["fits"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("correct_second", [False, True])
@pytest.mark.parametrize("call_attempts", [1, 2, 5])
async def test_invalid_integration_gets_one_corrective_attempt_without_adopting_invalid_edits(
    store, inputs, correct_second, call_attempts
):
    class InvalidFirst(DemoRuntime):
        def __init__(self):
            self.calls = []

        async def complete(self, task, received, schema, session):
            self.calls.append(received)
            if len(self.calls) == 1 or not correct_second:
                return RuntimeResult(json.dumps({"remove_block_ids": ["table"]}))
            return RuntimeResult(
                SupplementIntegration(
                    updates=[
                        Block(
                            block_id="si1",
                            text="应支持审批拒绝后的查询和原因记录。",
                            origin="model_proposal",
                        )
                    ]
                ).model_dump_json()
            )

    runtime = InvalidFirst()
    engine = engine_for(store, inputs, runtime)
    engine.limits = Limits(call_attempts=call_attempts)
    original = context()
    before = json.dumps(original, sort_keys=True)
    if correct_second and call_attempts > 1:
        result = await engine._call("integrate_supplement", original, SupplementIntegration)
        assert [b.block_id for b in result.updates] == ["si1"]
        assert not result.remove_block_ids
    else:
        with pytest.raises(ValueError, match="cannot remove established"):
            await engine._call("integrate_supplement", original, SupplementIntegration)
    assert len(runtime.calls) == min(call_attempts, 2)
    assert store.get(engine.job_id)["calls"] == min(call_attempts, 2)
    if call_attempts > 1:
        assert "cannot remove established" in runtime.calls[1]["format_feedback"]
    assert json.dumps(original, sort_keys=True) == before


async def test_integration_transport_failure_does_not_use_edit_correction_retry(store, inputs):
    class Unavailable(DemoRuntime):
        async def complete(self, task, received, schema, session):
            raise ModelCallFailed("Unavailable", "upstream")

    engine = engine_for(store, inputs, Unavailable())
    with pytest.raises(ModelCallFailed):
        await engine._call("integrate_supplement", context(), SupplementIntegration)
    assert store.get(engine.job_id)["calls"] == 1
