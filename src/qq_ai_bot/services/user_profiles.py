"""Privacy-preserving profile capture and current-scope resolution."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.persistence.repositories import PeopleRepository

logger = logging.getLogger(__name__)

_PROFILE_NAME_LIMIT = 128
PROFILE_LOOKUP_TIMEOUT_SECONDS = 0.25
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def sanitize_profile_name(value: str) -> str:
    """Flatten untrusted display metadata into a bounded single line."""

    cleaned = _CONTROL_CHARACTERS.sub(" ", value)
    return _WHITESPACE.sub(" ", cleaned).strip()[:_PROFILE_NAME_LIMIT]


class UserProfileResolver(Protocol):
    """Optionally fill missing event profile fields from the platform."""

    async def resolve(self, message: InboundMessage) -> ProfileResolution:
        """Return profile fields for only the current message sender."""


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    """Resolved profile values plus whether empty values are authoritative."""

    nickname: str
    group_card: str
    nickname_known: bool
    group_card_known: bool

    @property
    def display_name(self) -> str:
        """Return the resolved event/API display name."""

        return self.group_card or self.nickname

    @classmethod
    def from_sender(cls, sender: SenderIdentity) -> ProfileResolution:
        """Preserve authoritative empty cards supplied by the adapter."""

        return cls(
            nickname=sender.nickname,
            group_card=sender.group_card,
            nickname_known=sender.nickname_known or bool(sender.nickname),
            group_card_known=sender.group_card_known or bool(sender.group_card),
        )


class UserProfileService:
    """Capture profiles and enforce private/group lookup boundaries."""

    def __init__(
        self,
        repository: PeopleRepository,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._repository = repository
        self._runtime_config = runtime_config
        # Only successful empty values: no names, ownership or route authority.
        self._empty_profiles: OrderedDict[tuple[str, str, str | None], tuple[float, bool, bool]] = (
            OrderedDict()
        )

    async def capture(
        self,
        message: InboundMessage,
        resolver: UserProfileResolver | None = None,
        *,
        runtime: RuntimeConfigSnapshot | None = None,
    ) -> UserProfileSnapshot:
        """Capture one triggered caller and return an identity safe for this scope."""

        cache_key = (message.bot_user_id, message.sender.user_id, message.group_id)
        cached = self._empty_profiles.get(cache_key)
        lookup_message = message
        if cached is not None:
            if cached[0] <= time.monotonic():
                del self._empty_profiles[cache_key]
            else:
                lookup_message = replace(
                    message,
                    sender=replace(
                        message.sender,
                        nickname_known=message.sender.nickname_known or cached[1],
                        group_card_known=message.sender.group_card_known or cached[2],
                    ),
                )
        resolved = ProfileResolution.from_sender(lookup_message.sender)
        if resolver is not None:
            try:
                async with asyncio.timeout(PROFILE_LOOKUP_TIMEOUT_SECONDS):
                    resolved = await resolver.resolve(lookup_message)
            except Exception as exc:
                logger.warning(
                    "profile_resolve_failed exception_category=%s",
                    type(exc).__name__,
                )

        empty_nickname = resolved.nickname_known and not resolved.nickname
        empty_card = resolved.group_card_known and not resolved.group_card
        if empty_nickname or empty_card:
            # Cache hits do not renew the deadline indefinitely.
            deadline = (
                cached[0]
                if cached is not None and cached[0] > time.monotonic()
                else time.monotonic() + 60
            )
            self._empty_profiles[cache_key] = (deadline, empty_nickname, empty_card)
            self._empty_profiles.move_to_end(cache_key)
            while len(self._empty_profiles) > 256:
                self._empty_profiles.popitem(last=False)
        else:
            self._empty_profiles.pop(cache_key, None)

        nickname = sanitize_profile_name(resolved.nickname)
        group_card = sanitize_profile_name(resolved.group_card)
        existing: UserProfileSnapshot | None = None
        try:
            existing = await self._repository.get(
                user_id=message.sender.user_id,
                group_id=message.group_id,
            )
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(
                "profile_read_failed exception_category=%s",
                type(exc).__name__,
            )

        if message.scope_type is ScopeType.PRIVATE:
            if not resolved.nickname_known:
                nickname = nickname or (existing.nickname if existing is not None else "")
        elif existing is not None and not resolved.group_card_known:
            # Only the exact (user_id, group_id) card may be reused in a group.
            group_card = group_card or existing.group_card

        profile = UserProfileSnapshot(
            user_id=message.sender.user_id,
            scope_type=message.scope_type,
            nickname=nickname,
            group_id=message.group_id,
            group_card=group_card,
        )
        try:
            initial_affection: int | None = None
            initial_trust: int | None = None
            if runtime is None and self._runtime_config is not None:
                runtime = await self._runtime_config.snapshot(
                    user_id=profile.user_id,
                    group_id=profile.group_id,
                )
            if runtime is not None:
                initial_affection = runtime.relationship.initial_affection
                initial_trust = runtime.relationship.initial_trust
            await self._repository.observe(
                user_id=profile.user_id,
                nickname=nickname,
                group_id=profile.group_id,
                group_card=group_card,
                nickname_known=resolved.nickname_known,
                group_card_known=resolved.group_card_known,
                initial_affection=initial_affection,
                initial_trust=initial_trust,
                is_bot=message.sender.is_bot,
                expected_person_id=message.person_id,
            )
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(
                "profile_write_failed exception_category=%s",
                type(exc).__name__,
            )
        return profile
