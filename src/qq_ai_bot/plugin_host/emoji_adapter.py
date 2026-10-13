"""Failure-isolated plugin score signals for core emoji candidates."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from qq_ai_bot.emoji.models import EmojiSelectionRequest
from qq_ai_bot.emoji.retriever import RankedEmoji
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki_plugin_sdk.models import (
    EmojiSelectionCandidate,
    EmojiSelectionSignal,
    EmojiSelectionSignalContext,
)
from yuki_plugin_sdk.registrar import EmojiSelectionSignalRegistration

logger = logging.getLogger(__name__)


class PluginEmojiSelectionSignalAdapter:
    def __init__(self, registry: ExtensionRegistry, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("emoji signal timeout must be positive")
        self._registry = registry
        self._timeout = timeout_seconds

    async def adjust(
        self,
        candidates: tuple[RankedEmoji, ...],
        request: EmojiSelectionRequest,
    ) -> tuple[RankedEmoji, ...]:
        if not candidates:
            return candidates
        context = EmojiSelectionSignalContext(
            goal=request.goal[:300],
            emotion=request.emotion[:100],
            group_id=request.group_id,
            candidates=tuple(
                EmojiSelectionCandidate(
                    emoji_id=item.asset.id,
                    description=item.asset.description[:500],
                    emotion_tags=item.asset.emotion_tags[:30],
                    usage_scenarios=item.asset.usage_scenarios[:30],
                    base_score=item.score,
                )
                for item in candidates
            ),
        )
        registrations = tuple(
            cast(EmojiSelectionSignalRegistration, item.registration)
            for item in self._registry.list(kind=ExtensionKind.EMOJI_SELECTION_SIGNAL)
        )
        if not registrations:
            return candidates
        values = await asyncio.gather(
            *(self._invoke(registration, context) for registration in registrations)
        )
        valid_ids = {item.asset.id for item in candidates}
        deltas: dict[str, float] = {}
        for signal in values:
            if signal is None or signal.candidate_id not in valid_ids:
                continue
            deltas[signal.candidate_id] = deltas.get(signal.candidate_id, 0) + (
                signal.score_delta * signal.confidence
            )
        adjusted = tuple(
            RankedEmoji(asset=item.asset, score=item.score + deltas.get(item.asset.id, 0))
            for item in candidates
        )
        return tuple(sorted(adjusted, key=lambda item: -item.score))

    async def _invoke(
        self,
        registration: EmojiSelectionSignalRegistration,
        context: EmojiSelectionSignalContext,
    ) -> EmojiSelectionSignal | None:
        try:
            async with asyncio.timeout(self._timeout):
                return await registration.provider(context)
        except Exception:
            logger.debug("plugin emoji selection signal failed", exc_info=True)
            return None
