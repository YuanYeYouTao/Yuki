"""Read-only usage query and facts projected from original invocation rows."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from yuki.model_runtime.models import ModelStats

UsageRange = Literal["today", "24h", "7d", "all"]
UsageDimension = Literal["model", "task"]


class ModelUsageWindow(BaseModel):
    range: UsageRange
    timezone: str
    start: datetime | None
    end: datetime


class ModelUsageFilters(BaseModel):
    task: str | None = None
    provider: str | None = None
    model: str | None = None
    profile_id: str | None = None


class ModelUsageQuery(ModelUsageFilters):
    range: UsageRange
    timezone: str
    group_by: UsageDimension = "model"
    end: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must name an IANA timezone") from exc
        return value

    def window(self) -> ModelUsageWindow:
        end = self.end.astimezone(UTC)
        if self.range == "today":
            start = (
                end.astimezone(ZoneInfo(self.timezone))
                .replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
                .astimezone(UTC)
            )
        elif self.range == "all":
            start = None
        else:
            start = end - timedelta(hours=24 if self.range == "24h" else 24 * 7)
        return ModelUsageWindow(range=self.range, timezone=self.timezone, start=start, end=end)


class ModelUsageCoverage(BaseModel):
    """Calls reporting each field, including reported partial total/output usage."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int
    cache_creation_input_tokens: int
    cache_creation_5m_input_tokens: int
    cache_creation_1h_input_tokens: int


class ModelCacheRead(BaseModel):
    reported_calls: int
    prompt_tokens: int
    cached_prompt_tokens: int
    ratio: float | None


class ModelUsageStats(ModelStats):
    """Known sums, field coverage and cache ratio from the same reporting calls."""

    coverage: ModelUsageCoverage
    cache_read: ModelCacheRead


class ModelUsageSource(BaseModel):
    task: str
    provider: str
    model: str
    profile_id: str
    stats: ModelUsageStats


class ModelUsageGroup(BaseModel):
    task: str | None
    provider: str | None
    model: str | None
    stats: ModelUsageStats
    sources: list[ModelUsageSource]


class ModelUsageModel(BaseModel):
    provider: str
    model: str


class ModelUsageOptions(BaseModel):
    tasks: list[str]
    models: list[ModelUsageModel]


class ModelStatsView(BaseModel):
    range: ModelUsageWindow
    group_by: UsageDimension
    filters: ModelUsageFilters
    options: ModelUsageOptions
    summary: ModelUsageStats
    groups: list[ModelUsageGroup]
