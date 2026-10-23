"""A repair's new-material selection must not erase its existing citations."""

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import Block, JobInput, SectionContent
from folio.orchestration.engine import Engine
from folio.runtime.business_transport import translate_ids, unpack_rows
from folio.runtime.llm import LLMRuntime
from folio.runtime.writing_transport import validate_writing_provenance


@pytest.mark.parametrize("capacity_retry", [False, True])
async def test_unselected_repair_keeps_complete_cited_originals_and_wire_fields(
    store, inputs, tmp_path, capacity_retry
):
    settings = ModelSettingsStore(tmp_path / "model")
    settings.save(ModelSettingsInput(base_url="http://example.test/v1", model="test", no_auth=True))
    runtime = LLMRuntime(settings.path)
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    eid = "ev_existing_citation_with_a_long_identifier"
    original = {
        "evidence_id": eid,
        "source_id": "existing-source",
        "locator": "section 4",
        "text": "已核实的清算含义。" * 1000 + "尾部保留的特殊适用条件。",
    }
    current = SectionContent(
        blocks=[Block(block_id="b1", text="已核实的清算含义。", evidence_ids=[eid])]
    ).model_dump()
    context = {
        "section": {"section_id": "settlement"},
        "current": current,
        "repair_instruction": "整理另一段的重复内容，已有事实及其引用原样保留。",
        "evidence": [original, original | {"evidence_id": "unused", "text": "另一主题"}],
    }
    before = deepcopy(context)
    scored = []

    async def select(_purpose, catalog, **_kwargs):
        scored.extend(catalog)
        return []  # No new source is relevant to this particular edit.

    engine.select_reads = select
    try:
        selected = await engine.select_writing_materials(context, capacity_retry=capacity_retry)
        assert selected["evidence"] == [original]
        assert all(item["value"].get("evidence_id") != eid for item in scored)
        selected = await engine.select_writing_materials(selected, capacity_retry=True)
        assert selected["evidence"] == [original]
        messages, schema, _, _, references = runtime.request_parts(
            "repair", selected, engine.request_schema("repair", selected, SectionContent)
        )
        received = translate_ids(unpack_rows(json.loads(messages[1]["content"])), references)
        assert received["evidence"] == [original]
        assert received["current"] == current
        aliases = {canonical: wire for wire, canonical in references.items()}
        candidate = {
            "blocks": [
                {
                    k: v
                    for k, v in current["blocks"][0].items()
                    if k in {"block_id", "kind", "text", "origin", "evidence_ids"}
                }
            ]
        }
        wire_candidate = translate_ids(candidate, aliases)
        Draft202012Validator(schema).validate(wire_candidate)
        assert "evidence_ids" in json.dumps(schema)
        validate_writing_provenance("repair", selected, candidate)
        candidate["blocks"][0]["evidence_ids"] = []
        with pytest.raises(ValueError, match="without valid evidence_ids"):
            validate_writing_provenance("repair", selected, candidate)
        assert context == before
    finally:
        await runtime.aclose()
