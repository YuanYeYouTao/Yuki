"""History envelopes preserve event-time names across live role changes."""

from datetime import UTC, datetime

import pytest

from yuki.config import Settings
from yuki.domain.conversations import ScopeType
from yuki.persistence.event_view import event_view
from yuki.persistence.repository_records import EventRecord


@pytest.mark.parametrize(
    ("nickname", "card", "expected"),
    [("", "", "Agent"), ("旧角色名", "", "旧角色名"), ("旧昵称", "旧群名片", "旧群名片")],
)
def test_event_view_does_not_rename_historical_bot_authors(tmp_path, nickname, card, expected):
    soul = tmp_path / "SOUL.md"
    soul.write_text("我是{{name}}。", encoding="utf-8")
    settings = Settings(_env_file=None, soul_file=soul)
    settings.apply_character_name("Yuki", revision=1)
    row = EventRecord(
        id=42,
        bot_user_id="bot-account",
        platform_message_id="original-message",
        scope_type=ScopeType.GROUP,
        sender_user_id="bot-account",
        direction="outbound",
        content="原消息",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        author_kind="yuki",
        sender_nickname=nickname,
        sender_group_card=card,
    )
    original = event_view(row)
    assert original["sender_display_name"] == expected

    settings.apply_character_name("Aurora", revision=2)
    assert settings.character_identity.display_name == "Aurora"
    assert "Aurora" in settings.character_prompt
    assert event_view(row) == original
    assert row.sender_nickname == nickname
    assert row.sender_group_card == card
