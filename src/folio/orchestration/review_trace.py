"""Attribute real model attempts and cache hits to asynchronous review scopes.

Child asyncio tasks inherit the current scope. Nested scopes also contribute to
their parent, while sibling scopes retain separate counters. Callers must await
their child tasks before taking the final snapshot; this collector does not own
or cancel work. No prompts, responses or other model inputs are collected.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

__all__ = [
    "ReviewCallStats",
    "record_cache_hit",
    "record_call_failure",
    "record_model_call",
    "review_calls",
]

_COUNTERS = ("model_calls", "cache_hits", "failed_calls")


class ReviewCallStats:
    def __init__(self):
        self._lock = Lock()
        self._totals = dict.fromkeys(_COUNTERS, 0)
        self._tasks: dict[str, dict[str, int]] = {}

    def _record(self, task: str, field: str) -> None:
        # Recording has no await points. The lock also protects inherited
        # contexts used with asyncio.to_thread and snapshots from other threads.
        with self._lock:
            self._totals[field] += 1
            counts = self._tasks.setdefault(task, dict.fromkeys(_COUNTERS, 0))
            counts[field] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return self._totals | {
                "tasks": {task: counts.copy() for task, counts in sorted(self._tasks.items())}
            }


_scopes: ContextVar[tuple[ReviewCallStats, ...]] = ContextVar("folio_review_calls", default=())


@contextmanager
def review_calls() -> Iterator[ReviewCallStats]:
    stats = ReviewCallStats()
    token = _scopes.set((*_scopes.get(), stats))
    try:
        yield stats
    finally:
        _scopes.reset(token)


def record_model_call(task: str) -> None:
    """Record one actual runtime invocation, after acquiring its model slot."""
    for stats in _scopes.get():
        stats._record(task, "model_calls")


def record_cache_hit(task: str) -> None:
    """Record one successfully validated cached result, with no model request."""
    for stats in _scopes.get():
        stats._record(task, "cache_hits")


def record_call_failure(task: str) -> None:
    """Mark a failed attempt; its invocation was already counted separately."""
    for stats in _scopes.get():
        stats._record(task, "failed_calls")
