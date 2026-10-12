"""Schedule definitions and immutable occurrence projections."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# TurnOrigin moved to the neutral runtime layer in 3.6.0-R1; this re-export
# keeps the pre-existing import sites working until they migrate.
from qq_ai_bot.runtime.origin import TurnOrigin as TurnOrigin


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AfterSchedule(StrictModel):
    type: Literal["after"]
    seconds: int = Field(ge=1)


class OnceSchedule(StrictModel):
    type: Literal["once"]
    local_datetime: datetime
    timezone: str | None = None


class DailySchedule(StrictModel):
    type: Literal["daily"]
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    timezone: str | None = None


class WeeklySchedule(StrictModel):
    type: Literal["weekly"]
    weekdays: tuple[int, ...] = Field(min_length=1)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    timezone: str | None = None

    @model_validator(mode="after")
    def _valid_weekdays(self) -> WeeklySchedule:
        if any(day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("weekdays 必须使用星期一=1 到星期日=7")
        return self


class IntervalSchedule(StrictModel):
    type: Literal["interval"]
    seconds: int = Field(ge=1)


Schedule = Annotated[
    AfterSchedule | OnceSchedule | DailySchedule | WeeklySchedule | IntervalSchedule,
    Field(discriminator="type"),
]


class AutomationStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MISSED = "missed"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"


class AutomationRecord(StrictModel):
    id: int
    version_id: int | None = None
    claimed_by: str | None = None
    creator_kind: Literal["person", "self"] = "person"
    creator_user_id: str
    bot_user_id: str
    name: str
    status: AutomationStatus
    timezone: str
    script: dict[str, Any]
    schedule: Schedule
    script_hash: str
    authority_snapshot: dict[str, Any]
    created_from_message_id: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    max_runs: int | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime
    canonical_creator_person_id: str | None = None
    canonical_target_person_id: str | None = None
    canonical_target_space_id: str | None = None
    canonical_presence_id: str | None = None


class AutomationCreatorIdentity(StrictModel):
    """Safe creator projection for owner-independent automation reads."""

    person_id: str | None
    external_account_id: str
    display_name: str | None = None
    kind: Literal["person", "self"] = "person"


class AutomationDirectoryRecord(StrictModel):
    """The fields consumed by ordinary automation list/get."""

    id: int
    creator_kind: Literal["person", "self"] = "person"
    creator_user_id: str
    canonical_creator_person_id: str | None
    name: str
    status: AutomationStatus
    timezone: str
    next_run_at: datetime | None


class AutomationDirectoryEntry(StrictModel):
    """One task and its creator as returned by the global directory."""

    record: AutomationRecord | AutomationDirectoryRecord
    creator: AutomationCreatorIdentity


class AutomationRunRecord(StrictModel):
    id: int
    version_id: int | None = None
    automation_id: int
    scheduled_for: datetime
    actual_started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    steps_completed: int
    llm_calls: int
    tool_calls: int
    messages_sent: int
    error_category: str | None
    result_summary: dict[str, Any]
