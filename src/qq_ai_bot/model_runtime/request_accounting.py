"""Optional task-owned accounting at the actual HTTP attempt boundary."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

# Transport adapters never choose business tasks or route by request content.
before_provider_request: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "before_provider_request", default=None
)

after_provider_request: ContextVar[Callable[[str, int | None], Awaitable[None]] | None] = (
    ContextVar("after_provider_request", default=None)
)
