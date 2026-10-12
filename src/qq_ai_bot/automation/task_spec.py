"""A scheduled Main Agent task: timing, target and the complete request."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from qq_ai_bot.automation.models import Schedule, StrictModel
from qq_ai_bot.time.service import validate_timezone


class TaskDelivery(StrictModel):
    target: Literal["auto", "self_private", "current_group", "none"] = "auto"


class TaskSpec(StrictModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    trigger: Schedule
    timezone: str | None = Field(default=None, min_length=1)
    constraints: tuple[str, ...] = ()
    delivery: TaskDelivery = Field(default_factory=TaskDelivery)

    def instruction(self) -> str:
        return json.dumps(
            {"goal": self.goal, "constraints": self.constraints, "delivery": self.delivery.target},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def prepare_task(task: TaskSpec, *, timezone: str, current_group_id: str | None) -> TaskSpec:
    target = task.delivery.target
    if target == "auto":
        target = "current_group" if current_group_id else "self_private"
    if target == "current_group" and current_group_id is None:
        raise ValueError("current_group requires a real group conversation")
    trigger = task.trigger
    trigger_timezone = getattr(trigger, "timezone", None)
    if trigger_timezone:
        trigger = trigger.model_copy(update={"timezone": validate_timezone(trigger_timezone)})
    return task.model_copy(
        update={
            "timezone": validate_timezone(task.timezone or timezone),
            "trigger": trigger,
            "delivery": TaskDelivery(target=target),
        }
    )


def canonical_task_hash(task: TaskSpec) -> str:
    raw = json.dumps(
        task.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()
