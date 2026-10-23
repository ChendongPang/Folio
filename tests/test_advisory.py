import pytest

from folio.models import Finding, LocalReview, Review, Verdict
from folio.orchestration.advisory import partition_advisory_findings


def finding(kind="coverage", *, basis="content", **kwargs):
    return Finding(
        kind=kind,
        basis=basis,
        description="可补充交易撤销的验收示例",
        section_ids=["withdrawal"],
        key="acceptance-example",
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["coverage", "pending"])
def test_guide_only_suggestions_are_separated_without_mutating_review(kind):
    suggestion = finding(kind, basis="guidance")
    required = finding(kind)
    result = Review(findings=[suggestion, required])
    before = result.model_dump()

    actionable, notes = partition_advisory_findings(result, enabled=True)

    assert actionable == [required]
    assert notes == [suggestion]
    assert result.model_dump() == before


@pytest.mark.parametrize("kind", ["conflict", "unsupported", "extraction"])
def test_content_error_categories_cannot_be_dismissed_by_guide_label(kind):
    error = finding(kind, basis="guidance")
    actionable, notes = partition_advisory_findings(Review(findings=[error]), enabled=True)
    assert actionable == [error]
    assert notes == []


def test_no_guide_keeps_existing_review_behavior():
    issues = [finding(), finding("pending", basis="guidance")]
    actionable, notes = partition_advisory_findings(Review(findings=issues), enabled=False)
    assert actionable == issues
    assert notes == []


def test_legacy_result_without_basis_remains_actionable():
    old_result = Review.model_validate(
        {
            "findings": [
                {
                    "kind": "coverage",
                    "description": "用户要求明确撤销权限，正文没有说明",
                    "section_ids": ["withdrawal"],
                    "key": "withdrawal-permission",
                }
            ]
        }
    )
    actionable, notes = partition_advisory_findings(old_result, enabled=True)
    assert actionable == old_result.findings
    assert notes == []


def test_optional_wording_does_not_hide_explicit_business_requirement():
    issue = finding().model_copy(
        update={"description": "建议补充用户已明确要求的异常分支，否则缺失撤销失败后的处理规则"}
    )
    actionable, notes = partition_advisory_findings(Review(findings=[issue]), enabled=True)
    assert actionable == [issue]
    assert notes == []


def test_local_claim_verdicts_are_not_changed_by_advice_routing():
    verdict = Verdict(claim_id="permission", supported=False, reason="审批权限与原始依据相反")
    result = LocalReview(verdicts=[verdict], findings=[finding(basis="guidance")])
    actionable, notes = partition_advisory_findings(result, enabled=True)
    assert actionable == []
    assert len(notes) == 1
    assert result.verdicts == [verdict]
    assert result.verdicts[0].supported is False
