"""Typed decisions are separate from writing; the current provider is the configured LLM."""

from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field

from folio.models import Model
from folio.runtime.base import RuntimeResult

DECISION_TASKS = {
    "plan",
    "work_plan",
    "next_action",
    "understand_work",
    "review_work",
    "validate",
    "review",
    "drill_down",
    "intent",
    "select_context",
}


class ActionDecision(Model):
    action: Literal["search", "write", "pending"]
    summary: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list, max_length=30)


class IntentDecision(Model):
    # Strict providers require every output property in required. Local parsing
    # still accepts older cached/mock decisions through the default below.
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"required": ["intent", "summary", "style_source_ids"]}
    )
    intent: Literal[
        "chat", "analyze", "analyze_style", "generate", "resume_generation", "requirements", "clarify"
    ]
    summary: str = Field(min_length=1, max_length=1000)
    style_source_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=200
    )


class DecisionProvider(Protocol):
    async def decide(self, task, context, schema, request_id) -> RuntimeResult: ...


class LLMDecisionProvider:
    """Reuse the connection, but never inherit writer conversation or self-evaluation."""

    def __init__(self, runtime):
        self.runtime = runtime

    async def decide(self, task, context, schema, request_id):
        return await self.runtime.complete(task, context, schema, request_id)
