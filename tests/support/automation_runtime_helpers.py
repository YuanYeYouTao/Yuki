from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from tests.conftest import make_settings

from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.task_spec import TaskSpec
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.time.service import TimeContextService


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _RouteStub:
    def __init__(self, error_category: str | None = None) -> None:
        self._error_category = error_category

    async def resolve_send_for_person(self, _person_id: str) -> object:
        if self._error_category is not None:
            raise RouteSendError(self._error_category)
        return object()

    async def resolve_send_for_space(self, _space_id: str) -> object:
        if self._error_category is not None:
            raise RouteSendError(self._error_category)
        return object()


def _router(error_category: str | None = None) -> PresenceRouter:
    return cast(PresenceRouter, _RouteStub(error_category))


def _inbound(user_id: str = "10001") -> InboundMessage:
    return InboundMessage(
        source_event_id=1,
        message_id="automation-create",
        event_type="private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="用户"),
        text="1秒后提醒我测试",
        raw_text="1秒后提醒我测试",
        bot_user_id="7777",
    )


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


def _superuser_inbound(*targets: str) -> InboundMessage:
    text = "1秒后提醒 " + " ".join(targets)
    return InboundMessage(
        source_event_id=1,
        message_id="automation-super",
        event_type="private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="9000", nickname="超管"),
        text=text,
        raw_text=text,
        bot_user_id="7777",
    )


def _canonical_service(database):
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    return AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        time_service=TimeContextService(clock=clock),
    )
