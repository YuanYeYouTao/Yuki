"""Privacy-preserving profile capture and current-scope resolution."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.persistence.repositories import UserProfileRepository

logger = logging.getLogger(__name__)

_PROFILE_NAME_LIMIT = 128
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
        """Treat non-empty event fields as known and empty fields as missing."""

        return cls(
            nickname=sender.nickname,
            group_card=sender.group_card,
            nickname_known=bool(sender.nickname),
            group_card_known=bool(sender.group_card),
        )


class UserProfileService:
    """Capture profiles and enforce private/group lookup boundaries."""

    def __init__(
        self,
        repository: UserProfileRepository,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._repository = repository
        self._runtime_config = runtime_config

    async def capture(
        self,
        message: InboundMessage,
        resolver: UserProfileResolver | None = None,
    ) -> UserProfileSnapshot:
        """Capture one triggered caller and return an identity safe for this scope."""

        resolved = ProfileResolution.from_sender(message.sender)
        if resolver is not None:
            try:
                resolved = await resolver.resolve(message)
            except Exception as exc:
                logger.warning(
                    "profile_resolve_failed exception_category=%s",
                    type(exc).__name__,
                )

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
            if self._runtime_config is not None:
                runtime = await self._runtime_config.snapshot(
                    user_id=profile.user_id,
                    group_id=profile.group_id,
                )
                initial_affection = runtime.relationship.initial_affection
                initial_trust = runtime.relationship.initial_trust
            await self._repository.upsert(
                user_id=profile.user_id,
                nickname=nickname,
                group_id=profile.group_id,
                group_card=group_card,
                nickname_known=resolved.nickname_known,
                group_card_known=resolved.group_card_known,
                initial_affection=initial_affection,
                initial_trust=initial_trust,
                is_bot=message.sender.is_bot,
            )
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(
                "profile_write_failed exception_category=%s",
                type(exc).__name__,
            )
        return profile

    async def forget(self, user_id: str) -> bool | None:
        """Delete only the caller's profile, returning None on storage failure."""

        try:
            return await self._repository.delete_user(user_id)
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(
                "profile_delete_failed exception_category=%s",
                type(exc).__name__,
            )
            return None
