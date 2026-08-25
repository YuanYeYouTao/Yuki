"""Centralized lifecycle and capacity rules for emoji assets."""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import EmojiRuntimeConfig
from qq_ai_bot.emoji.models import EmojiAnalysis, EmojiAsset, EmojiLifecycleStatus
from qq_ai_bot.emoji.replacement import EmojiReplacementService
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from yuki_plugin_sdk.events import EventName

_ALLOWED_TRANSITIONS: dict[EmojiLifecycleStatus, frozenset[EmojiLifecycleStatus]] = {
    EmojiLifecycleStatus.CANDIDATE: frozenset(
        {
            EmojiLifecycleStatus.RECOGNIZED,
            EmojiLifecycleStatus.REJECTED,
            EmojiLifecycleStatus.BANNED,
            EmojiLifecycleStatus.MISSING,
        }
    ),
    EmojiLifecycleStatus.RECOGNIZED: frozenset(
        {
            EmojiLifecycleStatus.ADOPTED,
            EmojiLifecycleStatus.REJECTED,
            EmojiLifecycleStatus.BANNED,
            EmojiLifecycleStatus.MISSING,
        }
    ),
    EmojiLifecycleStatus.ADOPTED: frozenset(
        {
            EmojiLifecycleStatus.RECOGNIZED,
            EmojiLifecycleStatus.BANNED,
            EmojiLifecycleStatus.MISSING,
        }
    ),
    EmojiLifecycleStatus.REJECTED: frozenset(
        {
            EmojiLifecycleStatus.CANDIDATE,
            EmojiLifecycleStatus.RECOGNIZED,
            EmojiLifecycleStatus.BANNED,
            EmojiLifecycleStatus.MISSING,
        }
    ),
    EmojiLifecycleStatus.BANNED: frozenset({EmojiLifecycleStatus.RECOGNIZED}),
    EmojiLifecycleStatus.MISSING: frozenset(
        {
            EmojiLifecycleStatus.CANDIDATE,
            EmojiLifecycleStatus.RECOGNIZED,
            EmojiLifecycleStatus.ADOPTED,
            EmojiLifecycleStatus.BANNED,
        }
    ),
}


class EmojiLifecycleService:
    """Own all status changes; analysis can auto-adopt directly without review."""

    def __init__(
        self,
        repository: EmojiRepository,
        event_publisher: LifecycleEventPublisher | None = None,
        replacement: EmojiReplacementService | None = None,
    ) -> None:
        self._repository = repository
        self._event_publisher = event_publisher
        self._replacement = replacement

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    async def apply_analysis(
        self,
        asset: EmojiAsset,
        analysis: EmojiAnalysis,
        *,
        runtime: EmojiRuntimeConfig,
    ) -> EmojiAsset:
        status = (
            EmojiLifecycleStatus.RECOGNIZED if analysis.is_emoji else EmojiLifecycleStatus.REJECTED
        )
        updated = await self._repository.save_analysis(asset.id, analysis, status=status)
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_ANALYZED,
            {
                "emoji_id": updated.id,
                "is_emoji": analysis.is_emoji,
                "confidence": analysis.confidence,
                "analysis_version": analysis.analysis_version,
            },
        )
        if status is EmojiLifecycleStatus.REJECTED:
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_REJECTED,
                {"emoji_id": updated.id, "source": "classifier"},
            )
        if (
            analysis.is_emoji
            and runtime.auto_adopt_enabled
            and analysis.confidence >= runtime.auto_adopt_min_confidence
        ):
            if updated.status is EmojiLifecycleStatus.ADOPTED:
                return updated
            await self.adopt(
                updated.id,
                scope_type="global",
                scope_id="",
                runtime=runtime,
            )
            refreshed = await self._repository.get(updated.id)
            if refreshed is None:
                raise RuntimeError("adopted emoji disappeared")
            return refreshed
        return updated

    async def transition(
        self,
        emoji_id: str,
        target: EmojiLifecycleStatus,
        *,
        session: AsyncSession | None = None,
    ) -> EmojiAsset:
        asset = await self._require(emoji_id, session=session)
        if target == asset.status:
            return asset
        if target not in _ALLOWED_TRANSITIONS[asset.status]:
            raise ValueError(f"illegal emoji transition: {asset.status.value} -> {target.value}")
        updated = await self._repository.set_status(emoji_id, target, session=session)
        event = {
            EmojiLifecycleStatus.REJECTED: EventName.EMOJI_REJECTED,
            EmojiLifecycleStatus.BANNED: EventName.EMOJI_BANNED,
        }.get(target)
        if event is not None:
            await publish_notification(
                self._event_publisher,
                event,
                {"emoji_id": updated.id, "source": "administrator"},
            )
        return updated

    async def adopt(
        self,
        emoji_id: str,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str,
        runtime: EmojiRuntimeConfig,
    ) -> None:
        asset = await self._require(emoji_id)
        if asset.status not in {
            EmojiLifecycleStatus.RECOGNIZED,
            EmojiLifecycleStatus.ADOPTED,
        }:
            raise ValueError("only recognized emoji can be adopted")
        if await self._repository.has_enabled_scope(
            emoji_id,
            scope_type=scope_type,
            scope_id=scope_id,
        ):
            return
        capacity = runtime.pool_capacity
        if capacity is not None:
            count = await self._repository.adopted_count(
                group_id=scope_id if scope_type == "group" else None
            )
            if count >= capacity:
                if runtime.replacement_mode == "off":
                    raise ValueError("emoji pool is full and replacement is disabled")
                candidates = await self._repository.replaceable(
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
                replaceable = (
                    await self._replacement.choose(candidates, mode=runtime.replacement_mode)
                    if self._replacement is not None
                    else (candidates[0] if candidates else None)
                )
                if replaceable is None:
                    raise ValueError("emoji pool is full and contains only pinned assets")
                await self._repository.remove_scope(
                    replaceable.id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
        await self._repository.adopt_scope(
            emoji_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_ADOPTED,
            {
                "emoji_id": emoji_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
            },
        )

    async def unadopt(
        self,
        emoji_id: str,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str,
    ) -> bool:
        removed = await self._repository.remove_scope(
            emoji_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if removed:
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_UNADOPTED,
                {
                    "emoji_id": emoji_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                },
            )
        return removed

    async def _require(self, emoji_id: str, *, session: AsyncSession | None = None) -> EmojiAsset:
        asset = await self._repository.get(emoji_id, session=session)
        if asset is None:
            raise LookupError("emoji asset not found")
        return asset
