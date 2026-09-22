"""Deterministic semantic coarse ranking over adopted emoji metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from qq_ai_bot.admin.models import EmojiRuntimeConfig
from qq_ai_bot.emoji.models import EmojiAsset, EmojiReplyMode, EmojiSelectionRequest
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage

_TOKEN = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class RankedEmoji:
    asset: EmojiAsset
    score: float


class EmojiRetriever:
    def __init__(self, repository: EmojiRepository, storage: EmojiStorage) -> None:
        self._repository = repository
        self._storage = storage

    async def retrieve(
        self,
        request: EmojiSelectionRequest,
        *,
        runtime: EmojiRuntimeConfig,
    ) -> tuple[RankedEmoji, ...]:
        cooldown_after = datetime.now(UTC) - timedelta(seconds=runtime.same_emoji_cooldown_seconds)
        scope_cooldown_after = (
            None
            if request.mode in {EmojiReplyMode.PREFERRED, EmojiReplyMode.EMOJI_ONLY}
            else datetime.now(UTC) - timedelta(seconds=runtime.scope_repeat_cooldown_seconds)
        )
        rows = await self._repository.selectable(
            private_peer_user_id=request.private_peer_user_id,
            group_id=request.group_id,
            cooldown_after=cooldown_after,
            scope_cooldown_after=scope_cooldown_after,
            limit=max(runtime.selector_candidate_count * 4, runtime.selector_candidate_count),
        )
        query_terms = _terms(" ".join((request.goal, request.emotion, request.reply_text)))
        ranked: list[RankedEmoji] = []
        for asset, scope_weight in rows:
            if not self._storage.exists(asset.relative_path):
                continue
            document_terms = _terms(
                " ".join(
                    (
                        asset.description,
                        asset.ocr_text,
                        *asset.emotion_tags,
                        *asset.usage_scenarios,
                    )
                )
            )
            overlap = len(query_terms & document_terms)
            coverage = overlap / max(len(query_terms), 1)
            confidence = asset.confidence
            diversity = 1 / (1 + asset.use_count)
            score = scope_weight * 2 + coverage * 6 + confidence * 2 + diversity
            ranked.append(RankedEmoji(asset=asset, score=score))
        ranked.sort(
            key=lambda item: (-item.score, item.asset.last_used_at or item.asset.created_at)
        )
        return tuple(ranked[: runtime.selector_candidate_count])


def _terms(text: str) -> frozenset[str]:
    normalized = text.casefold()
    terms: set[str] = set()
    for token in _TOKEN.findall(normalized):
        if len(token) <= 2:
            terms.add(token)
            continue
        terms.add(token)
        terms.update(token[index : index + 2] for index in range(len(token) - 1))
    return frozenset(terms)
