"""Autonomous participation scoring and local admission features (R4/R5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from qq_ai_bot.conversation.participation import (
    AdmissionFeatures,
    LocalAutonomousParticipationPolicy,
)
from qq_ai_bot.domain.conversations import ScopeType


@dataclass(frozen=True)
class _Hint:
    source_plugin_id: str
    score_delta: float
    confidence: float = 1.0
    expires_at: datetime | None = None


def test_direct_admission_flags_do_not_boost_autonomous_score() -> None:
    scorer = LocalAutonomousParticipationPolicy()
    plain = scorer.score(AdmissionFeatures(scope_type=ScopeType.GROUP, text="请帮我查一下"))
    mentioned = scorer.score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="请帮我查一下",
            mentions_bot=True,
            reply_target_is_bot=True,
        )
    )
    private = scorer.score(AdmissionFeatures(scope_type=ScopeType.PRIVATE, text="请帮我查一下"))
    assert mentioned.score == plain.score
    assert mentioned.relevance_score == plain.relevance_score
    assert "private_scope" not in private.reasons
    assert "mentions_bot" not in mentioned.reasons
    assert "reply_to_bot" not in mentioned.reasons
    assert not private.should_participate


def test_low_value_reaction_scores_below_question_request_and_opinion() -> None:
    scorer = LocalAutonomousParticipationPolicy()

    def score(text: str) -> int:
        return scorer.score(AdmissionFeatures(scope_type=ScopeType.GROUP, text=text)).score

    low = score("哈哈")
    assert score("这是什么？") > low
    assert score("请帮我查一下资料") > low
    assert score("你觉得这个方案怎么样？") > low


def test_pressure_presence_and_fast_activity_are_distinct_components() -> None:
    scorer = LocalAutonomousParticipationPolicy()
    accumulated = scorer.score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            pending_message_count=8,
            average_human_interval_seconds=60,
        )
    )
    overactive = scorer.score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            recent_bot_messages=8,
            recent_total_messages=10,
            seconds_since_last_bot_message=5,
        )
    )
    fast = scorer.score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            average_human_interval_seconds=1,
        )
    )
    assert accumulated.pressure_score > 0
    assert overactive.presence_penalty > 0
    assert fast.activity_penalty > 0


def test_idle_compensation_requires_a_real_new_message() -> None:
    result = LocalAutonomousParticipationPolicy().score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="",
            idle_seconds=3600,
            new_message_count=0,
        )
    )
    assert result.score == 0
    assert not result.should_participate
    assert result.pressure_score == 0


def test_relationship_and_plugin_adjustments_are_bounded() -> None:
    now = datetime.now(UTC)
    signals = tuple(
        _Hint(
            source_plugin_id=f"plugin-{index}",
            score_delta=10.0,
            confidence=1.0,
            expires_at=now + timedelta(minutes=1),
        )
        for index in range(3)
    )
    result = LocalAutonomousParticipationPolicy().score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            relationship_adjustment=999,
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.relationship_adjustment == 5
    assert result.plugin_adjustment == 15


def test_same_plugin_and_expired_signals_cannot_bypass_caps() -> None:
    now = datetime.now(UTC)
    signals = (
        _Hint(source_plugin_id="same", score_delta=10.0, confidence=1.0),
        _Hint(source_plugin_id="same", score_delta=10.0, confidence=1.0),
        _Hint(
            source_plugin_id="expired",
            score_delta=10.0,
            confidence=1.0,
            expires_at=now - timedelta(seconds=1),
        ),
    )
    result = LocalAutonomousParticipationPolicy().score(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.plugin_adjustment == 10


def test_admission_metrics_use_author_kind_across_presences() -> None:
    from qq_ai_bot.conversation.features import AdmissionFeatureBuilder
    from qq_ai_bot.persistence.repository_records import EventRecord

    now = datetime(2026, 8, 25, tzinfo=UTC)

    def _row(
        event_id: int,
        *,
        bot_user_id: str,
        sender_user_id: str,
        author_kind: str | None,
        seconds: int,
    ) -> EventRecord:
        return EventRecord(
            id=event_id,
            bot_user_id=bot_user_id,
            platform_message_id=f"m{event_id}",
            scope_type=ScopeType.PRIVATE,
            sender_user_id=sender_user_id,
            direction="outbound" if author_kind == "yuki" else "inbound",
            content="x",
            visual_summary="",
            segments=(),
            occurred_at=now + timedelta(seconds=seconds),
            private_peer_user_id="1001",
            author_kind=author_kind,
        )

    rows = (
        _row(1, bot_user_id="8000", sender_user_id="1001", author_kind="person", seconds=0),
        _row(2, bot_user_id="8000", sender_user_id="8000", author_kind="yuki", seconds=1),
        _row(3, bot_user_id="8001", sender_user_id="7777", author_kind="external_bot", seconds=2),
        _row(4, bot_user_id="8001", sender_user_id="1001", author_kind="person", seconds=3),
        _row(5, bot_user_id="8001", sender_user_id="8001", author_kind="yuki", seconds=4),
    )
    after_yuki = AdmissionFeatureBuilder._metrics(rows, "8001", now + timedelta(seconds=5))
    assert after_yuki.bot_count == 2
    assert after_yuki.last_was_bot is True
    assert after_yuki.pending == 0
    pending_human = AdmissionFeatureBuilder._metrics(
        (
            *rows,
            _row(6, bot_user_id="8001", sender_user_id="1001", author_kind="person", seconds=5),
        ),
        "8001",
        now + timedelta(seconds=6),
    )
    assert pending_human.last_was_bot is False
    assert pending_human.pending == 1
    assert pending_human.bot_count == 2
