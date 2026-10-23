"""Task-local, public model activity; never forward response or reasoning content."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

_callback: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "folio_model_activity", default=None
)
_task: ContextVar[str | None] = ContextVar("folio_model_activity_task", default=None)
_PHASES = {"waiting": 0, "connected": 1, "thinking": 2, "responding": 3}


@contextmanager
def model_activity(callback: Callable[[dict], None]):
    """Subscribe the current operation and its child tasks to safe activity events."""
    token = _callback.set(callback)
    try:
        yield
    finally:
        _callback.reset(token)


@contextmanager
def model_activity_task(task: str):
    """Attach the logical model task without changing an operation's subscriber."""
    token = _task.set(task)
    try:
        yield
    finally:
        _task.reset(token)


class ModelActivity:
    """At most one event per real phase of an individual HTTP attempt."""

    def __init__(self):
        self.callback = _callback.get()
        self.task = _task.get()
        self.phase = -1

    def emit(self, status: str, *, response_chars: int | None = None):
        phase = _PHASES[status]
        if self.callback is None or phase <= self.phase:
            return
        self.phase = phase
        event = {"status": status}
        if self.task is not None:
            event["task"] = self.task
        if status == "responding" and type(response_chars) is int and response_chars >= 0:
            event["response_chars"] = response_chars
        try:
            self.callback(event)
        except Exception:
            # Progress is observational: a disconnected consumer cannot invalidate
            # a model response or trigger another provider request.
            pass
