"""OneBot read/control gateway for scheduled Agent context.

Scheduled message effects use SocialService and its durable receipts. This
adapter only supplies the normal Agent with its bound OneBot connection.
"""

from __future__ import annotations

import logging
from typing import Protocol

from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError

logger = logging.getLogger(__name__)


class ProactiveGatewayError(RuntimeError):
    """Sanitized gateway failure shared with plugin notification transport."""

    def __init__(self, category: str, *, uncertain: bool = False) -> None:
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class AutomationGateway(Protocol):
    async def call_api(self, action: str, params: dict[str, object]) -> object: ...


class OneBotAutomationGateway:
    def __init__(
        self,
        *,
        bot_user_id: str,
        automation_id: int,
        automation_run_id: int,
        router: PresenceRouter,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._automation_id = automation_id
        self._automation_run_id = automation_run_id
        self._router = router

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        if action.startswith(("send_", "upload_")) or action == "send_msg":
            raise ProactiveGatewayError("use_social_send_message")
        try:
            resolved = await self._router.resolve_send_for_account(self._bot_user_id)
        except RouteSendError as exc:
            raise ProactiveGatewayError(exc.category) from exc
        bot = resolved.connection.bot
        call_api = getattr(bot, "call_api", None)
        if bot is None or not callable(call_api):
            raise ProactiveGatewayError("bot_unavailable")
        try:
            return await call_api(action, **params)
        except Exception as exc:
            logger.error(
                "automation_onebot_failed automation_id=%d run_id=%d action=%s category=%s",
                self._automation_id,
                self._automation_run_id,
                action,
                type(exc).__name__,
            )
            raise ProactiveGatewayError("onebot_transport_failed") from exc
