"""Repair preserves existing pictures unless an explicit instruction forbids them."""

import pytest
from jsonschema import Draft202012Validator

from folio.models import Block, JobInput, SectionContent, VisualRequest
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime


@pytest.mark.parametrize("prohibited", [False, True])
def test_repair_schema_and_validation_agree_on_preserving_or_removing_old_picture(
    store, inputs, prohibited
):
    engine = Engine(store, DemoRuntime())
    engine.request = JobInput(**inputs, runtime="demo")
    engine.job_id = store.create(engine.request)
    current = SectionContent(
        blocks=[Block(block_id="b1", text="交易先录入，再完成审批。", origin="model_proposal")],
        visuals=[
            VisualRequest(
                figure_id="old",
                kind="flowchart",
                caption="流程",
                description="录入后审批",
                source_block_ids=["b1"],
                after_block_id="b1",
            )
        ],
    )
    context = {
        "current": current.model_dump(),
        "evidence": [],
        "required_visual_kinds": [],
        "figure_policy": {
            "allowed_visual_kinds": [],
            "prohibited_visual_kinds": ["flowchart"] if prohibited else [],
        },
    }
    candidate = current.model_copy(deep=True)
    if prohibited:
        candidate.visuals = []
    schema = engine.request_schema("repair", context, SectionContent)
    Draft202012Validator(schema).validate(candidate.model_dump())
    engine.check_result(candidate, context, task="repair")
    wrong = current.model_copy(deep=True)
    if not prohibited:
        wrong.visuals = []
    with pytest.raises(ValueError, match="outside confirmed|must be preserved"):
        engine.check_result(wrong, context, task="repair")
