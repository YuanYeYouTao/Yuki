"""Thin NoneBot matcher delegating all business logic to the processor."""

from __future__ import annotations

import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.matcher import Matcher

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.adapters.onebot.profiles import OneBotUserProfileResolver
from qq_ai_bot.adapters.onebot.sender import OneBotSender
from qq_ai_bot.container import get_container
from qq_ai_bot.identity.errors import CanonicalIdentityError

logger = logging.getLogger(__name__)

ai_message = on_message(priority=10, block=False)


@ai_message.handle()
async def handle_ai_message(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    """Normalize and process a OneBot message without persisting unrelated group chat."""

    container = get_container()
    inbound = normalize_event(
        event,
        ignored_bot_users=container.settings.ignored_bot_users,
    )
    profile_resolver = OneBotUserProfileResolver(bot)
    try:
        result = await container.processor.handle(
            inbound,
            OneBotSender(bot, event),
            profile_resolver,
        )
    except Exception as exc:
        logger.error(
            "unhandled_message_failure exception_category=%s identity_category=%s",
            type(exc).__name__,
            exc.category if isinstance(exc, CanonicalIdentityError) else "none",
            exc_info=exc,
        )
        return
    if result.handled:
        matcher.stop_propagation()  # type: ignore[no-untyped-call]
