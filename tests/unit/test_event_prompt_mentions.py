"""The prompt reads the common immutable mention snapshot without target rebinding."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.repository_records import EventRecord


def test_canonical_mentions_keep_original_names_and_positions_in_every_prompt_view() -> None:
    event = EventRecord(
        id=9,
        bot_user_id="9999",
        platform_message_id="mention-message",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        sender_group_card="发言人",
        direction="inbound",
        content="@油桃 这句和你说，@远野 那句和你说；@油桃 再确认。",
        visual_summary="",
        segments=(
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "油桃", "account_id": "account-a"},
            },
            {"type": "text", "data": {"text": " 这句和你说，"}},
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "远野", "account_id": "account-b"},
            },
            {"type": "text", "data": {"text": " 那句和你说；"}},
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "油桃", "account_id": "account-a"},
            },
            {"type": "text", "data": {"text": " 再确认。"}},
        ),
        mentioned_user_ids=("1002", "1003"),
        mentioned_person_ids=("person-a", "person-b"),
        occurred_at=datetime.now(UTC),
        canonical_conversation_id="conversation-a",
        author_kind="person",
        author_person_id="speaker",
    )
    renamed = replace(event, id=10, sender_user_id="1002", sender_group_card="后来改名")
    renderer = ChatEventPromptRenderer((event, renamed))

    for text in (
        renderer.render_event(event),
        renderer.render_reference_event(event),
        renderer.reference_message(
            event, current_event_id=event.id, current_content=event.content
        ).content,
        renderer.main_agent_history((event,))[0][2].content,
    ):
        assert text is not None and text.endswith(event.content)
        assert (
            "提及:油桃/@{account:account-a},远野/@{account:account-b},油桃/@{account:account-a}"
            in text
        )
        assert "[提及成员]" not in text and "提及Yuki" not in text
        assert "后来改名" not in text


def test_canonical_current_text_does_not_rewrite_quoted_or_literal_markers() -> None:
    event = EventRecord(
        id=1,
        bot_user_id="9999",
        platform_message_id="current",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="@油桃 当前正文",
        visual_summary="",
        segments=(),
        mentioned_user_ids=("1002",),
        occurred_at=datetime.now(UTC),
    )
    current = "[回复的消息]\n@远野 引用正文 [提及1002]\n\n@油桃 当前正文"
    rendered = ChatEventPromptRenderer((event,)).render_reference_event(
        event, current_event_id=event.id, current_content=current
    )

    assert rendered.endswith(current)


def test_account_author_is_displayed_without_a_fabricated_person():
    account_id = str(uuid4())
    event = EventRecord(
        id=11,
        bot_user_id="yuki",
        platform_message_id="unknown-source",
        platform="third",
        scope_type=ScopeType.GROUP,
        sender_user_id="tenant/user",
        sender_nickname="真实账号",
        direction="inbound",
        content="hello",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        canonical_conversation_id="conversation",
        author_kind="account",
        author_account_id=account_id,
    )
    text = ChatEventPromptRenderer((event,)).render_reference_event(event)
    assert f"account:{account_id}" in text and "person:" not in text
    assert text.endswith(">hello")
