"""Optional task-owned accounting at the actual HTTP attempt boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.domain.messages import ChatResponse

USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "reasoning_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)


@dataclass(slots=True)
class ProviderAttemptCounter:
    """Count dispatched HTTP requests, including retries and provider continuations.

    A missing usage report is unknown rather than zero. Fake providers never call
    ``dispatched`` and are not included in physical-request totals.
    """

    requests: int = 0
    reported_usage_requests: int = 0
    _usage: dict[int, dict[str, int]] = field(default_factory=dict)

    def dispatched(self) -> None:
        self.requests += 1

    def reported_usage(
        self, total_tokens: int | None, *, usage: Mapping[str, object] | None = None
    ) -> None:
        if self.requests == 0:
            # Injected providers without a real transport attempt are not part
            # of physical accounting, even if an adapter parser reports usage.
            return
        previous = self._usage.setdefault(self.requests, {})
        reported_before = "total_tokens" in previous
        for name, value in {**(usage or {}), "total_tokens": total_tokens}.items():
            if name in USAGE_FIELDS and type(value) is int and value >= 0:
                previous[name] = value
        if "total_tokens" in previous and not reported_before:
            self.reported_usage_requests += 1

    def reported_response(self, response: ChatResponse) -> None:
        self.reported_usage(
            response.total_tokens, usage={name: getattr(response, name) for name in USAGE_FIELDS}
        )

    def usage_totals(self) -> dict[str, int | None]:
        """Known output/total subtotals; complete input/cache coverage or unknown."""
        rows = [self._usage.get(index, {}) for index in range(1, self.requests + 1)]
        # A logical field describes the complete physical-request set. Mixing a
        # partial input denominator with a complete cache numerator recreates the
        # coverage error; keep that field unknown if any attempt omitted it.
        return {
            name: sum(row[name] for row in rows)
            if rows and all(name in row for row in rows)
            else sum(row[name] for row in rows if name in row)
            if name in {"total_tokens", "completion_tokens", "reasoning_tokens"}
            and any(name in row for row in rows)
            else None
            for name in USAGE_FIELDS
        }

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
