"""Per-purpose call budgets inherited by asynchronous model work."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CallBudgetPolicy:
    bucket: str
    max_calls: int
    reserve_calls: int = 0
    purpose: Literal["visual_prefetch", "research"] = "visual_prefetch"


current_call_budget: ContextVar[CallBudgetPolicy | None] = ContextVar(
    "folio_call_budget", default=None
)


@contextmanager
def call_budget_scope(policy):
    token = current_call_budget.set(policy)
    try:
        yield
    finally:
        current_call_budget.reset(token)


def visual_prefetch_policy(max_calls):
    """Bound optional work and leave a quarter of total calls for core work.

    Small jobs keep at least twelve calls, or their whole allowance if smaller.
    A user-authorized increase of the total allowance starts a new soft bucket;
    the job's durable overall counter still includes every previous attempt.
    """
    return CallBudgetPolicy(
        bucket=f"visual_prefetch:{max_calls}",
        max_calls=min(120, max_calls // 5),
        reserve_calls=min(max_calls, max(12, (max_calls + 3) // 4)),
    )


class VisualPrefetchDeferred(RuntimeError):
    """Optional work yielded its allowance; final export may still finish it."""

    code = "visual_prefetch_deferred"

    def __init__(self, *, bucket, used, limit, calls, reserve_calls):
        self.bucket = bucket
        self.used = used
        self.limit = limit
        self.calls = calls
        self.reserve_calls = reserve_calls
        super().__init__(
            f"Visual prefetch deferred ({bucket}: {used}/{limit}; "
            f"job calls={calls}, reserved={reserve_calls})"
        )


class ResearchModelDeferred(RuntimeError):
    """Research has exhausted its allowance or reached the core-call reserve."""

    code = "research_model_deferred"

    def __init__(self, *, bucket, used, limit, calls, reserve_calls):
        self.bucket = bucket
        self.used = used
        self.limit = limit
        self.calls = calls
        self.reserve_calls = reserve_calls
        super().__init__(
            f"Research model deferred ({bucket}: {used}/{limit}; "
            f"job calls={calls}, reserved={reserve_calls})"
        )
