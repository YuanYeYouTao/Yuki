"""Task-owned validity checks after model queues and before provider dispatch."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "model_dispatch_guard", default=None
)


@contextmanager
def model_dispatch_guard(check: Callable[[], Awaitable[None]]) -> Iterator[None]:
    """Scope a read-only guard to this operation and its child request tasks."""
    token = _current.set(check)
    try:
        yield
    finally:
        _current.reset(token)


async def check_model_dispatch() -> None:
    """Reject stale input without retrying it or changing request accounting."""
    check = _current.get()
    if check is not None:
        await check()
