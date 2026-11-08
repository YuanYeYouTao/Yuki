"""One public history query shape; limits are defaults, never hidden caps."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class HistoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID | None = None
    person_id: UUID | None = None
    space_id: UUID | None = None
    binding_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def one_selector(self) -> "HistoryTarget":
        if len(self.model_dump(exclude_none=True)) != 1:
            raise ValueError("target requires exactly one internal selector")
        return self


class HistorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str | None = None
    scope: Literal["private", "group"] | None = None


class HistoryQuery(BaseModel):
    """``read_chat`` arguments: one listing, or neighbours around one event."""

    model_config = ConfigDict(extra="forbid")
    target: HistoryTarget | None = None
    query: str = ""
    author: UUID | Literal["yuki"] | None = None
    has: tuple[Literal["image", "video", "file", "audio"], ...] = ()
    platform: str | None = None
    scope: Literal["private", "group"] | None = None
    order: Literal["recent", "oldest"] = "recent"
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    around: int | None = Field(default=None, gt=0, strict=True)
    context: int | None = Field(default=None, ge=0, strict=True)
    limit: int | None = Field(default=None, gt=0, strict=True)
    cursor: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")

    @field_validator("since", "until")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def around_alone(self) -> "HistoryQuery":
        if self.around is not None and (
            self.filtered
            or self.cursor is not None
            or self.limit is not None
            or "order" in self.model_fields_set
        ):
            raise ValueError("around_with_filters")
        return self

    @property
    def source(self) -> HistorySource | None:
        if self.platform is None and self.scope is None:
            return None
        return HistorySource(platform=self.platform, scope=self.scope)

    @property
    def filtered(self) -> bool:
        """Any content filter widens an omitted target to all permitted chats."""
        return bool(
            self.query
            or self.author is not None
            or self.has
            or self.platform is not None
            or self.scope is not None
            or self.since is not None
            or self.until is not None
        )
