"""Model-call deadlines follow the current job without shared runtime mutation."""

from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_MODEL_CALL_TIMEOUT_SECONDS = 900

_call_timeout: ContextVar[float] = ContextVar(
    "folio_model_call_timeout_seconds", default=DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
)


def current_model_call_timeout() -> float:
    return _call_timeout.get()


@contextmanager
def model_call_timeout(seconds: float):
    """Apply a job's configured deadline to this call and its async children."""
    token = _call_timeout.set(seconds)
    try:
        yield
    finally:
        _call_timeout.reset(token)
