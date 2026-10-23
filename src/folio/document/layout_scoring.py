"""Result shared by the Jev image scorer and post-render correction workflow."""

from pydantic import Field

from folio.models import Model


class PageLayoutScore(Model):
    page: int = Field(ge=1)
    score: float = Field(ge=0, le=10, allow_inf_nan=False)
    # Jev supplies a numeric judgment, not a generated explanation. The main
    # LLM inspects low-scoring pages separately to explain and repair them.
    reason: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    provider_score: float | None = Field(default=None, ge=0, le=9, allow_inf_nan=False)
