"""OneBot profile lookup used only after a message has triggered the bot."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.services.user_profiles import ProfileResolution

logger = logging.getLogger(__name__)


class OneBotUserProfileResolver:
    """Fill missing nickname/card fields for the current OneBot sender."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def resolve_group_name(self, group_id: str) -> str:
        """Refresh group metadata; the processor bounds lookup frequency."""

        try:
            payload = await self._bot.call_api(
                "get_group_info",
                group_id=int(group_id),
                no_cache=True,
            )
        except Exception as exc:
            logger.warning(
                "onebot_group_lookup_failed exception_category=%s",
                type(exc).__name__,
            )
            return ""
        if not isinstance(payload, Mapping):
            return ""
        group_name = payload.get("group_name")
        return group_name if isinstance(group_name, str) else ""

    async def resolve(self, message: InboundMessage) -> ProfileResolution:
        """Query only the current user, never a group member list."""

        nickname = message.sender.nickname
        group_card = message.sender.group_card
        nickname_known = bool(nickname)
        group_card_known = bool(group_card)
        try:
            if message.scope_type is ScopeType.GROUP and message.group_id is not None:
                if not nickname or not group_card:
                    payload = await self._bot.call_api(
                        "get_group_member_info",
                        group_id=int(message.group_id),
                        user_id=int(message.sender.user_id),
                        no_cache=False,
                    )
                    nickname, group_card, nickname_known, group_card_known = self._merge_payload(
                        payload,
                        nickname=nickname,
                        group_card=group_card,
                        nickname_known=nickname_known,
                        group_card_known=group_card_known,
                    )
            elif not nickname:
                payload = await self._bot.call_api(
                    "get_stranger_info",
                    user_id=int(message.sender.user_id),
                    no_cache=False,
                )
                nickname, _, nickname_known, _ = self._merge_payload(
                    payload,
                    nickname=nickname,
                    group_card="",
                    nickname_known=nickname_known,
                    group_card_known=False,
                )
        except Exception as exc:
            logger.warning(
                "onebot_profile_lookup_failed exception_category=%s",
                type(exc).__name__,
            )
        return ProfileResolution(
            nickname=nickname,
            group_card=group_card,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
        )

    @staticmethod
    def _merge_payload(
        payload: Any,
        *,
        nickname: str,
        group_card: str,
        nickname_known: bool,
        group_card_known: bool,
    ) -> tuple[str, str, bool, bool]:
        if not isinstance(payload, Mapping):
            return nickname, group_card, nickname_known, group_card_known
        returned_nickname = payload.get("nickname")
        returned_card = payload.get("card")
        if not nickname_known and isinstance(returned_nickname, str):
            nickname = returned_nickname
            nickname_known = True
        if not group_card_known and isinstance(returned_card, str):
            group_card = returned_card
            group_card_known = True
        return nickname, group_card, nickname_known, group_card_known
