import json

from folio.orchestration.rule_conflicts import conflict_candidates


def record(i, *, value=None, cardinality="single"):
    return {
        "section_id": "s" + str(i),
        "claim_id": "c" + str(i),
        "block_ids": ["b" + str(i)],
        "statement": f"协同对象{i}应提供对应资料。",
        "value": value if value is not None else f"对象{i}提供资料{i}",
        "cardinality": cardinality,
        "evidence_ids": ["e" + str(i)],
    }


def view(records):
    return {
        "disputed_keys": ["scope|subject|predicate"],
        "fact_index": {"scope|subject|predicate": records},
    }


def test_small_same_key_compares_all_records_in_one_question():
    records = [record(i) for i in range(5)]
    result = conflict_candidates(view(records))
    assert len(result) == 1
    finding = result[0]
    assert finding.section_ids == [r["section_id"] for r in records]
    assert finding.evidence_ids == [r["evidence_ids"][0] for r in records]
    assert {(r.section_id, r.block_id) for r in finding.block_refs} == {
        (r["section_id"], r["block_ids"][0]) for r in records
    }
    assert all(
        r["statement"] in finding.description and r["value"] in finding.description for r in records
    )
    assert "single marker is not proof of conflict" in finding.description


def test_large_key_retains_every_record_in_one_investigation():
    records = [record(i) for i in range(40)]
    result = conflict_candidates(view(records))
    assert len(result) == 1
    assert set(result[0].section_ids) == {r["section_id"] for r in records}
    # A short finding must defer to all original sections, not truncate their refs.
    assert result[0].block_refs == [] and result[0].evidence_ids == []


def test_multivalued_or_identical_values_do_not_become_conflicts():
    assert conflict_candidates(view([record(0), record(1, cardinality="multiple")])) == []
    assert conflict_candidates(view([record(0, value="相同"), record(1, value="相同")])) == []


def test_large_statements_keep_all_target_blocks_without_truncating_sources():
    records = [record(0), record(1)]
    for r in records:
        r["statement"] = "完整范围和例外；" * 200
        r["value"] = str(r["claim_id"]) * 333
    before = json.dumps(records, ensure_ascii=False)
    result = conflict_candidates(view(records))
    assert len(result) == 1 and len(result[0].description) <= 3500
    assert len(result[0].block_refs) == 2
    assert result[0].evidence_ids == ["e0", "e1"]
    assert json.dumps(records, ensure_ascii=False) == before


def test_expanded_candidate_description_survives_downstream_finding_validation():
    records = [record(0), record(1)]
    for r in records:
        r["statement"] = "原始范围" + "详" * 1400
    finding = conflict_candidates(view(records))[0]
    assert 3000 < len(finding.description) <= 3500
    assert all(r["statement"] in finding.description for r in records)


def test_dispersed_key_keeps_full_section_scope_when_anchors_exceed_contract():
    records = [record(i) for i in range(3)]
    for r in records:
        r["block_ids"] = ["b" + str(i) for i in range(5)]
    result = conflict_candidates(view(records))
    assert len(result) == 1
    assert result[0].section_ids == ["s0", "s1", "s2"]
    assert result[0].block_refs == []


def test_group_identity_covers_every_record_even_when_anchors_are_shared():
    records = [record(i, value=str(i)) for i in range(7)]
    for i, r in enumerate(records):
        r.update(section_id="s" + str(i % 2), block_ids=["b1"], evidence_ids=["e1"])
    candidates = conflict_candidates(view(records))
    assert len(candidates) == 1
    assert all(r["value"] in candidates[0].description for r in records)
    assert conflict_candidates(view(records[:-1]))[0].fingerprint != candidates[0].fingerprint
    reordered = conflict_candidates(view(list(reversed(records))))
    assert {f.fingerprint for f in candidates} == {f.fingerprint for f in reordered}


def test_long_fact_key_does_not_exceed_finding_contract():
    records = [record(0), record(1)]
    key = "完整限定范围" * 500
    candidates = conflict_candidates({"disputed_keys": [key], "fact_index": {key: records}})
    assert len(candidates) == 1
    assert len(candidates[0].key) <= 300
    assert all(r["statement"] in candidates[0].description for r in records)
