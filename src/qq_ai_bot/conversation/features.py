"""Build local autonomous-admission features from ledger projections.

This is the production replacement for leftover ``PlannerContextBuilder.admission_features``.
It never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.participation import AdmissionFeatures, AdmissionSignalHint
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.policies import replies_to_bot

_HISTORY_LIMIT = 10


@dataclass(frozen=True, slots=True)
class _ConversationMetrics:
    pending: int
    bot_count: int
    average_interval: float
    idle: float
    since_bot: float | None
    last_was_bot: bool


class AdmissionFeatureBuilder:
    """Keep repository reads out of the pure participation scorer."""

    def __init__(
        self,
        *,
        ledger: EventLedgerRepository,
    ) -> None:
        self._ledger = ledger

    async def admission_features(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        runtime: RuntimeConfigSnapshot,
        plugin_signals: tuple[AdmissionSignalHint, ...] = (),
        now: datetime | None = None,
    ) -> AdmissionFeatures:
        """Build local participation features without a generative model call."""

        del runtime
        current_time = now or datetime.now(UTC)
        recent = await self._ledger.list_recent_activity(
            inbound.scope(),
            conversation_id=inbound.conversation_id,
            limit=_HISTORY_LIMIT + 1,
        )
        metrics = self._metrics(recent, current_time)
        return AdmissionFeatures(
            scope_type=inbound.scope_type,
            text=content,
            reply_target_is_bot=replies_to_bot(inbound),
            mentions_bot=inbound.mentions_bot,
            continuation=metrics.last_was_bot,
            pending_message_count=metrics.pending,
            recent_bot_messages=metrics.bot_count,
            recent_total_messages=len(recent),
            average_human_interval_seconds=metrics.average_interval,
            idle_seconds=metrics.idle,
            seconds_since_last_bot_message=metrics.since_bot,
            plugin_signals=plugin_signals,
            new_message_count=max(1, metrics.pending),
            media_only=not content.strip() and bool(inbound.attachments),
            now=current_time,
        )

    @staticmethod
    def _metrics(
        rows: tuple[tuple[str, str | None, datetime], ...],
        now: datetime,
    ) -> _ConversationMetrics:
        human = [row for row in rows if row[0] == "message" and row[1] == AuthorKind.PERSON]
        bot = [row for row in rows if row[0] == "message" and row[1] == AuthorKind.YUKI]
        messages = [row for row in rows if row[0] == "message"]
        normalized_now = AdmissionFeatureBuilder._aware_utc(now)
        intervals = [
            max(
                0.0,
                (
                    AdmissionFeatureBuilder._aware_utc(right[2])
                    - AdmissionFeatureBuilder._aware_utc(left[2])
                ).total_seconds(),
            )
            for left, right in pairwise(human)
        ]
        last_bot_index = max(
            (
                index
                for index, row in enumerate(rows)
                if row[0] == "message" and row[1] == AuthorKind.YUKI
            ),
            default=-1,
        )
        pending = sum(
            1
            for row in rows[last_bot_index + 1 :]
            if row[0] == "message" and row[1] == AuthorKind.PERSON
        )
        last_time = (
            AdmissionFeatureBuilder._aware_utc(messages[-1][2]) if messages else normalized_now
        )
        last_bot_time = AdmissionFeatureBuilder._aware_utc(bot[-1][2]) if bot else None
        return _ConversationMetrics(
            pending=pending,
            bot_count=len(bot),
            average_interval=sum(intervals) / len(intervals) if intervals else 60.0,
            idle=max(0.0, (normalized_now - last_time).total_seconds()),
            since_bot=(
                max(0.0, (normalized_now - last_bot_time).total_seconds())
                if last_bot_time is not None
                else None
            ),
            last_was_bot=bool(messages and messages[-1][1] == AuthorKind.YUKI),
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
