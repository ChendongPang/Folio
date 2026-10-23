"""Versioned writing preferences, never evidence or delivery requirements."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROFILE_DIRECTORY = Path(__file__).resolve().parents[1] / "document_profiles"
GuideText = Annotated[str, Field(min_length=1, max_length=500)]
GuideLines = Annotated[list[GuideText], Field(max_length=12)]


class GuideModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuidancePolicy(GuideModel):
    # These are product behavior invariants, not switches for enabling stricter review.
    mode: Literal["advisory"] = "advisory"
    extra_review: Literal[False] = False
    retry_for_guidance: Literal[False] = False
    block_delivery: Literal[False] = False


class WritingPreferences(GuideModel):
    perspective: GuideText
    style: GuideText
    detail: GuideText
    organization: GuideText


class WritingGuide(GuideModel):
    id: Literal["urd", "srs"]
    version: int = Field(ge=1)
    name: GuideText
    purpose: GuideText
    audience: list[GuideText] = Field(min_length=1, max_length=12)
    guidance_policy: GuidancePolicy = Field(default_factory=GuidancePolicy)
    writing: WritingPreferences
    focus: dict[str, GuideLines] = Field(min_length=1, max_length=12)
    visuals: GuideLines = Field(default_factory=list)
    source_policy: GuideLines = Field(default_factory=list)
    avoid: GuideLines = Field(default_factory=list)

    @property
    def fingerprint(self):
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def load_guide(identifier: str) -> WritingGuide:
    if identifier not in {"urd", "srs"}:
        raise ValueError('Unknown document type; choose urd or srs.')
    path = PROFILE_DIRECTORY / f"{identifier}.yaml"
    guide = WritingGuide.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if guide.id != identifier:
        raise ValueError('The writing guide ID does not match its filename.')
    return guide


def list_guides() -> list[dict]:
    return [
        load_guide(identifier).model_dump(include={"id", "name", "purpose", "version"})
        for identifier in ("urd", "srs")
    ]


PLANNING = {"plan", "repair_plan", "revise_plan", "work_plan"}
WRITING = {"generate", "repair", "complete_gaps", "integrate_supplement"}
REVIEWING = {
    "validate", "review", "review_work", "review_focus", "plan_review", "drill_down",
    "validate_visual", "next_action", "understand_work",
}
VISUAL = {"design_ui", "design_flowchart"}


def guidance_context(task: str, context: dict, guide: WritingGuide | dict | None) -> dict:
    """A deterministic, idempotent view used by both token measurement and execution.

    No keyword-based business classification, model call, new evidence or checklist is
    created here. Each view is small; the model applies relevant hints within its action.
    """
    if guide is None:
        return context
    if not isinstance(guide, WritingGuide):
        guide = WritingGuide.model_validate(guide)
    if task not in PLANNING | WRITING | REVIEWING | VISUAL | {"query"}:
        return {k: v for k, v in context.items() if k != "writing_guidance"}
    data = guide.model_dump(mode="json")
    view = {key: data[key] for key in ("id", "version", "name", "purpose", "audience", "guidance_policy")}
    # The stored name also labels the UI and default document title. Translate
    # only the model-facing type label, without changing that user-facing identity.
    view["name"] = {
        "urd": "User Requirements Document",
        "srs": "Software Requirements Specification",
    }[guide.id]
    view["snapshot"] = guide.fingerprint
    if task in PLANNING | WRITING:
        view["writing"] = data["writing"]
        view["focus"] = data["focus"]
        view["avoid"] = data["avoid"]
    if task == "query":
        view["focus"] = data["focus"]
    if task in WRITING | VISUAL:
        view["visuals"] = data["visuals"]
        view["source_policy"] = data["source_policy"]
    # Review and action selection intentionally receive no checklist of preferences.
    return context | {"writing_guidance": view}


def guidance_instruction(task: str) -> str:
    instruction = (
        "\nwriting_guidance is optional advice for the selected document type. Explicit user "
        "requirements and this section's scope take precedence. Apply relevant suggestions to "
        "the actual business context, without turning them into a checklist or mechanically "
        "expanding sections, scope, tables or figures. It is neither business evidence, mandatory "
        "requirements nor an acceptance checklist; never cite it as evidence. Do not convert "
        "guide entries into mandatory Section.instruction or completion_criteria. Directory "
        "instructions and completion criteria may organize only explicit requirements from the "
        "user and actual sources; keep the guide separate. Do not add pending_items, open_questions, "
        "missing_information, searches or repairs solely because guide advice was not applied."
    )
    if task in REVIEWING:
        instruction += (
            " Review only the existing task, without adding a guide compliance audit. Do not "
            "infer unstated mandatory content from the URD/SRS name. Normally omit purely stylistic "
            "suggestions. If one must be retained, set findings basis=guidance and kind=coverage "
            "or pending; it is recorded as nonblocking advice. Use basis=content for missing "
            "explicit requirements, business conflicts or incorrect factual support. Do not "
            "downgrade a real defect merely because it overlaps a guide topic."
        )
    return instruction


def guidance_schema(schema: dict, context: dict) -> dict:
    """Use the same explicit review attribution in estimation and execution."""
    if not context.get("writing_guidance") or "Finding" not in schema.get("$defs", {}):
        return schema
    result = deepcopy(schema)
    finding = result["$defs"]["Finding"]
    finding["required"] = list(dict.fromkeys([*finding.get("required", []), "basis"]))
    return result
