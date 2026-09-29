"""Optional task-owned accounting at the actual HTTP attempt boundary."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class ProviderAttemptCounter:
    """Count dispatched HTTP requests, including retries and provider continuations.

    A missing usage report is unknown rather than zero. Fake providers never call
    ``dispatched`` and are not included in physical-request totals.
    """

    requests: int = 0
    reported_usage_requests: int = 0

    def dispatched(self) -> None:
        self.requests += 1

    def reported_usage(self, total_tokens: int | None) -> None:
        if type(total_tokens) is int and total_tokens >= 0:
            self.reported_usage_requests += 1

    @property
    def unknown_usage_requests(self) -> int:
        return self.requests - self.reported_usage_requests


current_provider_attempts: ContextVar[ProviderAttemptCounter | None] = ContextVar(
    "current_provider_attempts", default=None
)

# Transport adapters never choose business tasks or route by request content.
before_provider_request: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "before_provider_request", default=None
)

after_provider_request: ContextVar[Callable[[str, int | None], Awaitable[None]] | None] = (
    ContextVar("after_provider_request", default=None)
)
