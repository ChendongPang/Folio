"""Per-business-task model usage collected across nested async workflows."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic

from folio.orchestration.work import TaskUsage


def _token_count(usage: dict | None, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


@dataclass
class TaskUsageTracker:
    task_id: str
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    started: float = field(default_factory=monotonic)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    cache_hits: int = 0
    failed_calls: int = 0

    def model_call(self) -> None:
        self.model_calls += 1

    def cache_hit(self) -> None:
        self.cache_hits += 1

    def model_failure(self) -> None:
        self.failed_calls += 1

    def add_usage(self, usage: dict | None) -> None:
        prompt = _token_count(usage, "prompt_tokens", "input_tokens")
        completion = _token_count(usage, "completion_tokens", "output_tokens")
        total = _token_count(usage, "total_tokens")
        self.input_tokens += prompt
        self.output_tokens += completion
        self.total_tokens += total or prompt + completion

    def snapshot(self) -> TaskUsage:
        return TaskUsage(
            task_id=self.task_id,
            started_at=self.started_at,
            finished_at=datetime.now(UTC).isoformat(),
            elapsed_ms=max(0, round((monotonic() - self.started) * 1000)),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            model_calls=self.model_calls,
            cache_hits=self.cache_hits,
            failed_calls=self.failed_calls,
        )


_CURRENT_TASK_USAGE: ContextVar[TaskUsageTracker | None] = ContextVar(
    "folio_business_task_usage", default=None
)


def current_task_usage() -> TaskUsageTracker | None:
    return _CURRENT_TASK_USAGE.get()


def bind_task_usage(tracker: TaskUsageTracker):
    return _CURRENT_TASK_USAGE.set(tracker)


def reset_task_usage(token) -> None:
    _CURRENT_TASK_USAGE.reset(token)


def record_model_call() -> None:
    if tracker := current_task_usage():
        tracker.model_call()


def record_model_usage(usage: dict | None) -> None:
    if tracker := current_task_usage():
        tracker.add_usage(usage)


def record_cache_hit() -> None:
    if tracker := current_task_usage():
        tracker.cache_hit()


def record_model_failure() -> None:
    if tracker := current_task_usage():
        tracker.model_failure()
