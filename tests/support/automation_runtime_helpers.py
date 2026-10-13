from __future__ import annotations

from datetime import datetime, timedelta

from qq_ai_bot.automation.task_spec import TaskSpec


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _script() -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "name": "一次提醒",
            "goal": "提醒本人：测试",
            "timezone": "Asia/Shanghai",
            "trigger": {"type": "after", "seconds": 1},
            "delivery": {"target": "self_private"},
        }
    )
