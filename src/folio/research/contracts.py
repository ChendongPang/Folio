"""Durable research configuration and model-facing action contracts."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic.json_schema import SkipJsonSchema

from folio.research.memory import ResearchNoteUpdate
from folio.runtime.tool_calls import MAX_TOOL_CALLS_PER_BATCH


class ResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_tool_calls: int = Field(default=50, ge=1, le=500)
    max_steps: int = Field(default=60, ge=1, le=600)
    max_steps_per_round: int = Field(default=12, ge=1, le=100)
    max_model_calls: int = Field(default=40, ge=1, le=500)
    max_execution_seconds: float = Field(default=600, gt=0, le=7200)
    max_seconds_per_execution: float = Field(default=120, ge=1, le=300)
    max_result_chars: int = Field(default=600000, ge=2000)
    max_observation_chars: int = Field(default=600000, ge=2000)
    max_artifact_review_chars: int = Field(default=600000, ge=2000)
    reserve_model_calls: int = Field(default=40, ge=1, le=10000)


class ResearchStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal[
        "fs.list", "fs.inspect", "fs.search", "fs.read", "fs.view", "sandbox.exec", "artifact.save", "research.save", "finish"
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Actual provider envelope, preserved for restart and the matching tool result.
    # Empty only for decisions durably admitted before native tool calling.
    tool_call: dict[str, Any] = Field(default_factory=dict)
    _legacy_arguments_json: str | None = PrivateAttr(default=None)
    summary: str = Field(default="", max_length=1500)
    notes: str = Field(default="", max_length=4000)
    research_notes: list[ResearchNoteUpdate] = Field(default_factory=list)
    review_reason: str = Field(default="", max_length=1000)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def accept_saved_arguments(cls, value, handler):
        """Old durable decisions replay unchanged; new replies expose an object."""
        legacy = None
        if isinstance(value, dict) and "arguments_json" in value:
            value = dict(value)
            legacy = value.pop("arguments_json")
            if not isinstance(legacy, str) or len(legacy) > 1_024_000:
                raise ValueError("arguments_json must be a bounded JSON object string")
            try:
                args = json.loads(legacy)
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("arguments_json must encode a JSON object") from exc
            if not isinstance(args, dict):
                raise ValueError("arguments_json must encode a JSON object")
            if "arguments" in value and value["arguments"] != args:
                raise ValueError("arguments and arguments_json disagree")
            value["arguments"] = args
        if isinstance(value, dict) and isinstance(value.get("arguments"), dict):
            value = dict(value)
            value["arguments"] = {key: item for key, item in value["arguments"].items()
                                  if item is not None}
        result = handler(value)
        result._legacy_arguments_json = legacy
        return result

    @property
    def arguments_json(self):
        # This compatibility property also preserves the identity of an already
        # admitted, pre-migration tool operation after a service restart.
        return self._legacy_arguments_json or json.dumps(self.arguments, ensure_ascii=False)

    def provided_metadata(self, name):
        """Recover omitted-vs-explicit metadata after cache/checkpoint defaults.

        Persisted Pydantic results contain default fields. The original native
        argument object is the durable record of which updates were supplied.
        """
        if self.tool_call:
            raw = self.tool_call.get("function", {}).get("arguments", "")
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                return False
            return isinstance(value, dict) and name in value
        return name in self.model_fields_set

    def note_updates(self):
        """Preserve nested omitted fields across full-default result caches."""
        if not self.tool_call:
            return self.research_notes
        try:
            raw = json.loads(self.tool_call["function"]["arguments"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Cannot restore research-note updates from native tool calls') from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("research_notes", []), list):
            raise ValueError('Research notes in native tool calls must be a list')
        return [ResearchNoteUpdate.model_validate(item) for item in raw.get("research_notes", [])]

    @model_validator(mode="after")
    def valid_arguments(self):
        try:
            size = len(json.dumps(self.arguments, ensure_ascii=False))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("arguments must be a JSON object") from exc
        if size > 1_024_000:
            raise ValueError("Research arguments exceed the supported size")
        if any(len(item) > 3500 for item in self.missing_information):
            raise ValueError("Research gap exceeds the supported size")
        return self


class ResearchBatch(BaseModel):
    """One provider turn, including all correlated calls and visible content."""

    model_config = ConfigDict(extra="forbid")
    steps: list[ResearchStep] = Field(min_length=1, max_length=MAX_TOOL_CALLS_PER_BATCH)
    assistant_content: str = ""
    reasoning_content: SkipJsonSchema[str | None] = None

    @model_validator(mode="before")
    @classmethod
    def accept_saved_step(cls, value):
        # Existing cache entries and durable pending_step records are single calls.
        if isinstance(value, ResearchStep):
            return {"steps": [value]}
        if isinstance(value, dict) and "tool" in value and "steps" not in value:
            return {"steps": [value]}
        return value


class CoverageExclusionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    reason: str = Field(min_length=1, max_length=3500)


class DerivedEvidenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool
    reason: str = Field(min_length=1, max_length=3500)
    source_matches: bool
    computation_matches: bool
    business_scope_matches: bool

    @model_validator(mode="after")
    def positive_requires_all_checks(self):
        if self.supported and not all(
            (self.source_matches, self.computation_matches, self.business_scope_matches)
        ):
            raise ValueError("A supported derivation requires all three checks")
        return self
