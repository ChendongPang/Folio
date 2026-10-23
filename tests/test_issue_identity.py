"""A dismissed issue cannot hide another concern when review batches change."""

import json
from itertools import permutations
from types import SimpleNamespace

import pytest

from folio.models import Finding, digest
from folio.orchestration.issues import IssueLedger, effective_findings
from folio.orchestration.local_checks import finding_version
from folio.orchestration.review_candidates import deduplicate_review_findings


def issue(description="审批角色缺失", *, anchored=False):
    return Finding(
        kind="coverage",
        description=description,
        section_ids=["approval"],
        key="approval-rules",
        block_refs=[{"section_id": "approval", "block_id": "rules"}] if anchored else [],
    )


@pytest.mark.parametrize("anchored", [False, True])
def test_same_key_different_issue_has_identity_before_any_merge(anchored):
    first = issue(anchored=anchored)
    second = issue("审批超时处理缺失", anchored=anchored)
    originals = [first.model_dump(), second.model_dump()]
    states = [SimpleNamespace(section_id="approval", revision=1)]
    dismissed = {finding_version(first, states): "正文已经明确审批角色"}

    assert first.fingerprint != second.fingerprint
    for batch in ([first], [second], [first, second], [second, first]):
        retained = deduplicate_review_findings(batch).findings
        assert retained == batch
        assert [f.fingerprint for f in retained] == [f.fingerprint for f in batch]
        assert effective_findings(retained, states, dismissed=dismissed) == (
            [second] if second in batch else []
        )
    assert [first.model_dump(), second.model_dump()] == originals


def test_duplicate_fingerprint_survives_key_aliases_order_and_serialization():
    first = issue(anchored=True)
    alias = first.model_copy(update={"key": "another-model-key", "description": "审批角色 缺失"})
    other = issue("审批超时处理缺失", anchored=True)
    states = [SimpleNamespace(section_id="approval", revision=1)]
    dismissed = {finding_version(first, states): "已经明确角色"}
    assert alias.fingerprint == first.fingerprint
    for order in permutations([first, alias, other]):
        retained = deduplicate_review_findings(order).findings
        restored = [Finding.model_validate_json(f.model_dump_json()) for f in retained]
        assert len(restored) == 2
        assert {f.fingerprint for f in restored} == {first.fingerprint, other.fingerprint}
        assert effective_findings(restored, states, dismissed=dismissed) == [other]


def test_unlocated_same_description_different_requirement_keys_stay_distinct():
    first = issue()
    second = first.model_copy(update={"key": "refund-rules"})
    assert first.fingerprint != second.fingerprint
    assert deduplicate_review_findings([first, second]).findings == [first, second]


@pytest.mark.parametrize(
    "change",
    [
        {"basis": "guidance"},
        {"rule": "coverage"},
        {"evidence_ids": ["other-policy"]},
        {"comparison_scope": "退款审批"},
        {"incompatibility": "两处审批角色不同"},
        {"description": "审批超时处理缺失"},
    ],
)
def test_change_to_issue_meaning_cannot_reuse_dismissal(change):
    first = issue()
    changed = first.model_copy(update=change)
    states = [SimpleNamespace(section_id="approval", revision=1)]
    dismissed = {finding_version(first, states): "已经明确角色"}
    assert effective_findings([changed], states, dismissed=dismissed) == [changed]


def test_historical_ambiguous_waiver_stays_audit_only_and_current_waiver_persists(tmp_path):
    first, second = issue(), issue("审批超时处理缺失")
    states = [SimpleNamespace(section_id="approval", revision=1)]
    # This is the historical v1 identity shared by both issues. The persisted
    # entry contains no description, so there is no safe automatic migration.
    legacy_version = digest("coverage|approval|approval-rules") + ":" + digest(
        str([[('approval', 1)], []])
    )
    path = tmp_path / "adjudications.json"
    historical = {legacy_version: "当时认为角色已经说明"}
    path.write_text(json.dumps(historical), encoding="utf-8")
    ledger = IssueLedger(path)
    assert effective_findings([first, second], states, dismissed=ledger.dismissed()) == [
        first,
        second,
    ]

    ledger.dismiss(finding_version(first, states), "重新精查后确认角色已说明")
    restored = IssueLedger(path).dismissed()
    assert restored[legacy_version] == historical[legacy_version]
    assert effective_findings([second], states, dismissed=restored) == [second]
    assert effective_findings([first, second], states, dismissed=restored) == [second]
    assert json.loads(path.read_text(encoding="utf-8")) == restored


def test_merge_audit_preserves_original_keys_and_points_to_stable_identity():
    first, second = issue(), issue("审批超时处理缺失")
    result = deduplicate_review_findings([first, second, second])
    assert result.findings == [first, second]
    distinct, duplicate = result.audit
    assert distinct["action"] == "distinct_issue"
    assert distinct["original"] == second.model_dump()
    assert distinct["retained_fingerprint"] == second.fingerprint
    assert distinct["other_fingerprint"] == first.fingerprint
    assert duplicate["action"] == "duplicate"
    assert duplicate["retained_fingerprint"] == second.fingerprint
