"""Lossless, versioned user requirements, separate from bounded chat context."""

import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from folio.runtime.errors import PlanningContextTooLarge

MAX_REQUIREMENT_CHARACTERS = 600_000

# Only exact UI commands are excluded lexically. Semantic classification is
# performed once by the existing intent decision; punctuation is not intent.
UI_COMMANDS = {
    "你好",
    "您好",
    "hi",
    "hello",
    "谢谢",
    "谢谢你",
    "开始",
    "继续",
    "确认",
    "开始生成",
    "直接开始生成",
    "继续生成",
    "生成文档",
    "编写文档",
    "生成目录",
    "规划目录",
    "确认目录",
    "确认并生成",
    "确认目录并生成正文",
    "重新生成目录",
    "重新生成",
    "修改目录",
    "根据已上传资料和我们的讨论规划目录",
    "generate",
    "confirm_directory",
    "revise_directory",
    "regenerate_directory",
    "根据当前资料和需求重新生成完整目录",
    "根据最新要求重新生成目录",
    "resume_directory_research",
    "从中断处继续资料研究",
    "thank you", "thanks", "start", "continue", "confirm", "start generating",
    "continue generating", "generate document", "write document", "generate outline",
    "plan outline", "confirm outline", "confirm and generate", "regenerate outline",
    "regenerate", "revise outline", "resume source research",
    "plan the outline from the uploaded sources and our discussion",
    "regenerate the full outline from the current sources and requirements",
    "regenerate the outline from the latest requirements",
}


def substantive_text(text: str) -> bool:
    return bool(text.strip()) and text.strip().rstrip("。！!,.，?？").lower() not in UI_COMMANDS


def mark_requirement(message: dict, *, intent: str | None = None) -> None:
    """Persist why an input is active, discussion, or still awaiting a decision."""
    action = message.get("action", "chat")
    if intent is not None:
        status = {
            "generate": "active",
            "requirements": "active",
            "clarify": "clarification",
            "analyze_style": "discussion",
        }.get(intent, "discussion")
        kind = "revision" if intent == "requirements" else "request"
        source = "intent"
    else:
        status = "pending" if action == "chat" else "active"
        kind = "revision" if action == "revise_directory" else "request"
        source = "awaiting_intent" if action == "chat" else "explicit_action"
    if (
        action in {"confirm_directory", "resume_generation", "resume_directory_research"}
        or intent == "resume_generation"
        or not substantive_text(message.get("content", ""))
    ):
        status = "command"
    message["requirement"] = {"status": status, "kind": kind, "source": source}
    if intent is not None:
        message["requirement"]["intent"] = intent


def resolve_repeated_pending(state: dict) -> None:
    """Resending a failed input resolves only that exact input, preserving audit."""
    messages = state.get("messages", [])
    for index, message in enumerate(messages):
        if message.get("requirement", {}).get("status") != "pending":
            continue
        for replacement_index in range(len(messages) - 1, index, -1):
            replacement = messages[replacement_index]
            status = replacement.get("requirement", {}).get("status")
            if replacement.get("role") != "user" or status not in {
                "active",
                "discussion",
                "clarification",
                "command",
            }:
                continue
            if message.get("content", "").strip() in {
                replacement.get("content", "").strip(),
                (replacement.get("advice") or "").strip(),
            }:
                message["requirement"] = message["requirement"] | {
                    "status": "resolved",
                    "resolved_by": f"message:{replacement_index}",
                }
                break


def pending_requirements(state: dict) -> list[dict]:
    return [
        {"source_id": f"message:{index}", "text": message.get("content", "")}
        for index, message in enumerate(state.get("messages", []))
        if message.get("role") == "user"
        and message.get("requirement", {}).get("status") == "pending"
    ]


class RequirementEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    kind: Literal["request", "revision"] = "request"
    text: str = Field(min_length=1)


def _revision(entries: Sequence[RequirementEntry]) -> str:
    payload = json.dumps(
        [entry.model_dump() for entry in entries], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RequirementSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = 1
    revision: str
    entries: tuple[RequirementEntry, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def valid_revision(self):
        if self.revision != _revision(self.entries):
            raise ValueError("Requirement revision does not match its immutable entries")
        if len({entry.source_id for entry in self.entries}) != len(self.entries):
            raise ValueError("Duplicate requirement source")
        return self

    @property
    def text(self) -> str:
        return "\n\n".join(entry.text for entry in self.entries)

    @classmethod
    def from_entries(cls, entries: list[RequirementEntry]):
        return cls(revision=_revision(entries), entries=entries)

    @classmethod
    def from_text(cls, text: str):
        entries = [RequirementEntry(source_id="request", text=text)] if text.strip() else []
        return cls.from_entries(entries)

    def ensure_capacity(self) -> None:
        if len(self.text) > MAX_REQUIREMENT_CHARACTERS:
            raise RequirementsTooLarge(len(self.text))


class RequirementsTooLarge(PlanningContextTooLarge):
    def __init__(self, characters: int):
        self.public_message = (
            f"用户需求累计 {characters:,} 字符，超过当前需求容量 {MAX_REQUIREMENT_CHARACTERS:,} 字符；"
            "完整需求和修订历史已保留，未截断，尚未调用目录模型。请新建会话并提供整理后的完整需求。"
        )
        ValueError.__init__(self, self.public_message)


def sync_requirements(state: dict, *, revision_history=()) -> RequirementSnapshot:
    """Append accepted inputs once; never retroactively discard legacy constraints."""
    resolve_repeated_pending(state)
    saved = state.get("requirements")
    entries = list(RequirementSnapshot.model_validate(saved).entries) if saved else []
    known = {entry.source_id for entry in entries}
    for index, message in enumerate(state.get("messages", [])):
        if message.get("role") != "user":
            continue
        action = message.get("action", "chat")
        if action in {"confirm_directory", "resume_generation"}:
            continue
        text = message.get("content", "")
        advice = message.get("advice")
        classification = message.get("requirement")
        if classification is None:
            # Old histories have no reliable intent audit. Preserve all their
            # constraints, including entries already used by frozen directories.
            classification = {
                "status": "active" if substantive_text(text) else "command",
                "kind": "request",
                "source": "legacy",
            }
            message["requirement"] = classification
        # The browser renders the same advice as the message label. Keep the
        # original advice once, without injecting the UI prefix into requirements.
        if advice and text in {advice, f"修改建议：{advice}", f"Revision advice: {advice}"}:
            text = ""
        source_id = f"message:{index}"
        if (
            source_id not in known
            and classification["status"] == "active"
            and substantive_text(text)
        ):
            entries.append(
                RequirementEntry(source_id=source_id, kind=classification["kind"], text=text)
            )
            known.add(source_id)
        advice_id = f"advice:{index}"
        if advice and advice.strip() and advice_id not in known:
            entries.append(RequirementEntry(source_id=advice_id, kind="revision", text=advice))
            known.add(advice_id)
    # Old clients did not persist advice in their messages. Recover it from the
    # directory metadata on first use; future requests preserve exact ordering.
    texts = {entry.text for entry in entries}
    for advice in revision_history:
        if advice and advice not in texts:
            source_id = "legacy-advice:" + hashlib.sha256(advice.encode()).hexdigest()
            entries.append(RequirementEntry(source_id=source_id, kind="revision", text=advice))
            texts.add(advice)
    snapshot = RequirementSnapshot.from_entries(entries)
    state["requirements"] = snapshot.model_dump(mode="json")
    return snapshot
