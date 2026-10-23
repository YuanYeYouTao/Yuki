"""The complete request and its one structured timing expression."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import Field, TypeAdapter

from yuki.automation.models import DailySchedule, Schedule, StrictModel, WeeklySchedule
from yuki.time.service import validate_timezone


class TaskSpec(StrictModel):
    goal: str = Field(min_length=1)
    trigger: Schedule
    name: str | None = Field(default=None, min_length=1)

    def instruction(self) -> str:
        return self.goal


def prepare_task(task: TaskSpec, *, timezone: str) -> TaskSpec:
    trigger = task.trigger
    if isinstance(trigger, DailySchedule | WeeklySchedule):
        trigger = trigger.model_copy(
            update={"timezone": validate_timezone(trigger.timezone or timezone)}
        )
    return task.model_copy(update={"trigger": trigger})


def canonical_task_hash(task: TaskSpec) -> str:
    raw = json.dumps(
        task.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def legacy_creation_hash(
    payload: object, original: dict[str, Any], scene: dict[str, Any]
) -> str | None:
    """Compare an accepted pre-0151 creation; never translate or execute it.

    Omitted defaults use the original saved scene and timezone. This reader is
    reached only for an existing call key whose immutable first task is old.
    """
    if "version" not in original or "goal" not in original or "trigger" not in original:
        return None
    if not isinstance(payload, dict) or set(payload) - {
        "version",
        "name",
        "goal",
        "trigger",
        "timezone",
        "constraints",
        "delivery",
    }:
        return None
    if payload.get("version", 1) != 1:
        return None
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in ("name", "goal")):
        return None
    constraints = payload.get("constraints", ())
    if not isinstance(constraints, list | tuple) or any(
        not isinstance(item, str) for item in constraints
    ):
        return None
    delivery = payload.get("delivery", {})
    if not isinstance(delivery, dict) or set(delivery) - {"target"}:
        return None
    target = delivery.get("target", "auto")
    if target not in {"auto", "self_private", "current_group", "none"}:
        return None
    if target == "auto":
        target = "current_group" if scene.get("current_group_id") else "self_private"
    try:
        raw_trigger = payload["trigger"]
        if not isinstance(raw_trigger, dict):
            return None
        if raw_trigger.get("type") == "once":
            if set(raw_trigger) - {"type", "local_datetime", "timezone"}:
                return None
            value = TypeAdapter(datetime).validate_python(raw_trigger["local_datetime"])
            trigger = {
                "type": "once",
                "local_datetime": TypeAdapter(datetime).dump_python(value, mode="json"),
            }
            if raw_trigger.get("timezone"):
                trigger["timezone"] = validate_timezone(raw_trigger["timezone"])
        else:
            schedule: Schedule = TypeAdapter(Schedule).validate_python(raw_trigger)
            if isinstance(schedule, DailySchedule | WeeklySchedule) and schedule.timezone:
                schedule = schedule.model_copy(
                    update={"timezone": validate_timezone(schedule.timezone)}
                )
            trigger = schedule.model_dump(mode="json", exclude_none=True)
        task = {
            "version": 1,
            "name": payload["name"],
            "goal": payload["goal"],
            "trigger": trigger,
            "timezone": validate_timezone(payload.get("timezone") or original["timezone"]),
            "constraints": list(constraints),
            "delivery": {"target": target},
        }
        raw = json.dumps(task, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (KeyError, TypeError, ValueError):
        return None
    return hashlib.sha256(raw.encode()).hexdigest()
