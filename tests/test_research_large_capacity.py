"""Large-model budgets apply to research requests and returned tool arguments."""

from types import SimpleNamespace

from folio.document.style_contracts import StyleAnalysisConfig
from folio.research.contracts import DerivedEvidenceReview, ResearchConfig, ResearchStep
from folio.research.planning import PlanningResearchLimits, PlanningResearchStep
from folio.research.workflow import ResearchTools


def test_research_tool_paging_defaults_allow_large_results():
    research = ResearchConfig()
    planning = PlanningResearchLimits()
    assert research.max_result_chars == research.max_observation_chars == research.max_artifact_review_chars == 600000
    assert planning.max_result_chars == 600000
    assert planning.max_evidence_chars is None
    assert StyleAnalysisConfig().max_prompt_chars == 600000
    assert StyleAnalysisConfig().max_total_chars is None


def test_large_native_arguments_are_not_rejected_at_old_character_limits():
    arguments = {"text": "source evidence " * 15000}
    assert ResearchStep(tool="fs.read", arguments=arguments).arguments == arguments
    assert PlanningResearchStep(tool="finish", arguments=arguments).arguments == arguments


async def test_artifact_review_uses_whole_request_token_capacity_before_character_fallback():
    seen = []

    async def request_capacity(action, context, schema):
        seen.append((action, context, schema))
        return {"fits": True, "estimated_input_tokens": 160000}

    tools = object.__new__(ResearchTools)
    tools.config = ResearchConfig(max_artifact_review_chars=24000)
    tools.engine = SimpleNamespace(request_capacity=request_capacity)
    context = {"input_text": "verified input " * 50000}
    assert await tools.review_fits("verify_derivation", context, DerivedEvidenceReview)
    assert seen == [("verify_derivation", context, DerivedEvidenceReview)]

    tools.engine = SimpleNamespace()
    assert not await tools.review_fits("verify_derivation", context, DerivedEvidenceReview)
