"""Immutable provider-neutral web search models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

WebSearchTopic = Literal["general", "news"]
WebSearchTimeRange = Literal["day", "week", "month", "year"]


class WebMode(StrEnum):
    """Configured backend strategy for model-approved web access."""

    DISABLED = "disabled"
    NATIVE = "native"
    TAVILY = "tavily"
    BOTH = "both"

    @classmethod
    def _missing_(cls, value: object) -> WebMode | None:
        # Configuration spelling only; never restores the removed router.
        if value == "native_with_tavily_fallback":
            return cls.BOTH
        return None


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    """A bounded query sent to a configured web search provider."""

    query: str
    topic: WebSearchTopic = "general"
    time_range: WebSearchTimeRange | None = None
    start_date: date | None = None
    end_date: date | None = None
    max_results: int = 5
    extract_max_results: int | None = None


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    """One real provider-returned source and its query-relevant content."""

    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    relevant_content: str
    published_at: datetime | None = None
    provider_score: float | None = None
    provider: str = "tavily"


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    """A complete search/extract operation returned to the Agent layer."""

    query: str
    sources: tuple[WebSearchSource, ...]
    provider_request_id: str | None
    latency_seconds: float
    partial_failure: bool = False
    provider: str = "tavily"
