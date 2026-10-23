"""Dependency-light contracts for bounded style-reference analysis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DIMENSIONS = {
    "structure": "文档结构", "writing": "写作风格", "organization": "内容组织方式",
    "detail": "内容详细程度", "headings": "标题风格", "figures_tables": "图表风格",
    "typography": "字体与排版", "page": "页面布局", "visual": "视觉设计",
    "expression": "专业表达习惯",
}
DIMENSION_LABELS = {
    "structure": "Document structure", "writing": "Writing style",
    "organization": "Content organization", "detail": "Level of detail",
    "headings": "Heading style", "figures_tables": "Figure and table style",
    "typography": "Typography and paragraph formatting", "page": "Page layout",
    "visual": "Visual design", "expression": "Professional expression",
}


class StyleAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls: int = Field(default=128, ge=0, le=128)
    max_prompt_chars: int = Field(default=600000, ge=2000)
    max_total_chars: int | None = Field(default=None, ge=2000)
    max_samples: int = Field(default=10000, ge=1, le=100000)
    sample_chars: int = Field(default=1200, ge=100, le=4000)
    timeout_seconds: float = Field(default=90, gt=0, le=300)


class StyleRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: Literal[
        "structure", "writing", "organization", "detail", "headings", "figures_tables",
        "typography", "page", "visual", "expression",
    ]
    scope: Literal["general", "body", "headings", "tables", "cover", "appendix", "technical", "management"]
    description: str = Field(min_length=3, max_length=180)
    sample_ids: list[str] = Field(min_length=1, max_length=6)
    confidence: Literal["observed", "tentative"]


class StyleDimensionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: Literal[
        "structure", "writing", "organization", "detail", "headings", "figures_tables",
        "typography", "page", "visual", "expression",
    ]
    status: Literal["analyzed", "not_present", "requires_visual", "unresolved"]
    reason: str = Field(min_length=3, max_length=240)
    sample_ids: list[str] = Field(default_factory=list, max_length=6)


class StyleAnalysisStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[StyleRule] = Field(default_factory=list, max_length=20)
    request_sample_ids: list[str] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    dimension_checks: list[StyleDimensionCheck] = Field(default_factory=list, max_length=10)


class StyleConsolidationStep(StyleAnalysisStep):
    rules: list[StyleRule] = Field(default_factory=list, max_length=40)
    reviewed_rule_ids: list[str] = Field(min_length=1, max_length=2000)
