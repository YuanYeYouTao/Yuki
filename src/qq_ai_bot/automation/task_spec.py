"""Provider-neutral high-level automation task contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from qq_ai_bot.automation.models import AutomationContext, Schedule, StrictModel


class TaskStrategy(StrEnum):
    AUTO = "auto"
    STATIC = "static"
    GENERATED = "generated"
    AGENTIC = "agentic"


class TaskDelivery(StrictModel):
    target: Literal["auto", "self_private", "current_group", "none"] = "auto"
    text: str | None = Field(default=None, min_length=1, max_length=12000)


class TaskSpec(StrictModel):
    """Small intent contract shared by conversational and scheduled Agents."""

    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2500)
    trigger: Schedule
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    strategy: TaskStrategy = Field(
        default=TaskStrategy.AUTO,
        description=("纯提醒用 static；运行时需要模型或工具时用 agentic。auto 使用主 Agent 执行。"),
    )
    constraints: tuple[str, ...] = Field(default=(), max_length=12)
    context: AutomationContext = Field(default_factory=AutomationContext)
    delivery: TaskDelivery = Field(default_factory=TaskDelivery)
