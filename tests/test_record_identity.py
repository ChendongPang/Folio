from copy import deepcopy

import pytest
from test_request_context import engine_for

from folio.orchestration.record_identity import normalize_record_ids
from folio.orchestration.work import BusinessRecord
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


def record():
    return {
        "summary": "冻结与正常状态必须分别处理。",
        "facts": [
            {"fact_id": "f1", "statement": "冻结时不能继续提交。", "evidence_ids": ["e1"]},
            {"fact_id": "f1", "statement": "解除冻结后可以继续提交。", "evidence_ids": ["e2"]},
            {"fact_id": "f2", "statement": "操作必须留痕。", "evidence_ids": ["e3"]},
        ],
        "open_questions": ["解除冻结的授权角色尚未明确。"],
    }


def test_collided_labels_preserve_different_conditions_and_their_sources():
    value = record()
    before = deepcopy(value)
    restored = normalize_record_ids(value)
    parsed = BusinessRecord.model_validate(restored)
    assert value == before
    assert len({f.fact_id for f in parsed.facts}) == 3
    assert [{k: v for k, v in f.items() if k != "fact_id"} for f in restored["facts"]] == [
        {k: v for k, v in f.items() if k != "fact_id"} for f in value["facts"]
    ]
    assert restored["facts"][2]["fact_id"] == "f2"
    assert normalize_record_ids(value) == restored
    assert normalize_record_ids(restored) == restored


def test_only_exact_repetition_inside_the_collided_id_is_deduplicated():
    value = record()
    value["facts"].append(deepcopy(value["facts"][0]))
    value["facts"].append(value["facts"][0] | {"evidence_ids": ["different-source"]})
    normalized = normalize_record_ids(value)
    assert len(normalized["facts"]) == 4
    assert normalized["open_questions"] == value["open_questions"]


def test_invalid_sources_are_not_silently_repaired():
    value = record()
    value["facts"][0]["evidence_ids"] = []
    assert normalize_record_ids(value) == value
    with pytest.raises(ValueError):
        BusinessRecord.model_validate(value)


async def test_new_model_record_does_not_retry_a_label_collision(store, inputs):
    import json

    class Reader(DemoRuntime):
        calls = 0

        async def complete(self, task, context, schema, session):
            assert task == "understand_work"
            self.calls += 1
            return RuntimeResult(content=json.dumps(record(), ensure_ascii=False))

    runtime = Reader()
    engine = engine_for(store, inputs, runtime)
    context = {"evidence": [{"evidence_id": eid} for eid in ["e1", "e2", "e3"]]}
    result = await engine._call("understand_work", context, BusinessRecord)
    assert len(result.facts) == 3 and runtime.calls == 1
    replayed = await engine._call("understand_work", context, BusinessRecord)
    assert replayed == result and runtime.calls == 1
