"""Request-local cooperative I/O limits without changing provider protocols.

Built-in transports use the smaller of their configured timeout and the run's
remaining wall time. A socket timeout cannot cancel work already dispatched to
a remote service, and arbitrary injected providers may not observe this context.
The runner therefore also rejects late results after every synchronous call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Optional

_deadline: ContextVar[Optional[float]] = ContextVar("echo_adk_io_deadline", default=None)


@contextmanager
def limit_io(seconds: float) -> Iterator[None]:
    """Narrow I/O time for this call; nested limits can never extend a deadline."""
    deadline = monotonic() + max(0.0, seconds)
    parent = _deadline.get()
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def io_timeout(configured: float) -> float:
    """Return a positive timeout or refuse to start I/O after the deadline."""
    deadline = _deadline.get()
    remaining = configured if deadline is None else min(configured, deadline - monotonic())
    if remaining <= 0:
        raise TimeoutError("run wall-clock deadline expired")
    return remaining
