"""Yuki's own mentions and messages read under her current character name.

Failure cases written before the change:
1. A native @ of Yuki snapshotted as the group card ("Agent") reaches the model only as
   "@Agent", so after a compaction leaves few of her own lines she no longer knows the
   mention is her. Every prompt view must show the current character name instead.
2. The original display name must stay readable, so "Agent 怎么不回" still connects.
3. A mention whose snapshot already equals the character name needs no extra label.
4. A member who happens to be called "Agent" (or another bot called "BOT yuki") is a
   member mention and keeps its event-time name.
5. Literal "@Agent" text that is not a native mention span stays literal.
6. Yuki's own outbound lines and replies to them must not carry the fallback "Agent".
7. Current text that differs from the stored body (offsets do not belong to it) is not
   rewritten at guessed positions.
8. A rename shows the new name; the cached history source is rebuilt for it.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.repository_records import EventRecord

PERSON = "00000000-0000-4000-8000-000000000001"
PRESENCE = "00000000-0000-4000-8000-000000000004"
YUKI_ACCOUNT = "00000000-0000-4000-8000-000000000007"
MEMBER_ACCOUNT = "00000000-0000-4000-8000-000000000008"


def _mention(kind: str, name: str, account: str, start: int) -> dict[str, object]:
    return {
        "type": "mention",
        "data": {
            "kind": kind,
            "display_name": name,
            "account_id": account,
            "presence_id": PRESENCE if kind == "yuki" else None,
            "text_start": start,
            "text_end": start + len(name) + 1,
        },
    }


def _event(event_id: int, **changes: object) -> EventRecord:
    event = EventRecord(
        id=event_id,
        bot_user_id="8000",
        origin_key=f"native-{event_id}",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="9912345",
        sender_group_card="查无此人",
        direction="inbound",
        content="@Agent 笨蛋",
        visual_summary="",
        segments=(_mention("yuki", "Agent", YUKI_ACCOUNT, 0), {"type": "text", "data": {}}),
        occurred_at=datetime(2026, 11, 18, 15, 40, tzinfo=UTC) + timedelta(seconds=event_id),
        group_id="2001",
        canonical_conversation_id="00000000-0000-4000-8000-000000000006",
        author_kind="person",
        author_person_id=PERSON,
    )
    return replace(event, **changes)


def _views(renderer: ChatEventPromptRenderer, event: EventRecord) -> tuple[str, ...]:
    return (
        renderer.render_event(event),
        renderer.render_reference_event(event),
        renderer.reference_message(
            event, current_event_id=event.id, current_content=event.content
        ).content
        or "",
        renderer.main_agent_history((event,))[0][2].content or "",
    )


def test_native_self_mention_reads_as_current_character_name_in_every_view() -> None:
    event = _event(1)
    renderer = ChatEventPromptRenderer((event,), self_name="Yuki")

    for text in _views(renderer, event):
        assert text.endswith(">@Yuki 笨蛋") or text.endswith("] @Yuki 笨蛋")
        assert f"提及:Yuki(显示为Agent)/@{{account:{YUKI_ACCOUNT}}}" in text
        assert "@Agent" not in text


def test_self_mention_already_named_after_the_character_has_no_alias_label() -> None:
    event = _event(
        1,
        content="@Yuki 在吗",
        segments=(_mention("yuki", "Yuki", YUKI_ACCOUNT, 0),),
    )
    renderer = ChatEventPromptRenderer((event,), self_name="Yuki")

    for text in _views(renderer, event):
        assert f"提及:Yuki/@{{account:{YUKI_ACCOUNT}}}" in text
        assert "显示为" not in text


def test_member_mentions_and_literal_text_keep_their_event_time_names() -> None:
    content = "@Agent 和 @BOT yuki 都在，@Agent 也在"
    event = _event(
        1,
        content=content,
        segments=(
            _mention("member", "Agent", MEMBER_ACCOUNT, 0),
            _mention("member", "BOT yuki", MEMBER_ACCOUNT, content.index("@BOT")),
            _mention("yuki", "Agent", YUKI_ACCOUNT, content.rindex("@Agent")),
        ),
    )
    renderer = ChatEventPromptRenderer((event,), self_name="Yuki")

    for text in _views(renderer, event):
        assert text.endswith("@Agent 和 @BOT yuki 都在，@Yuki 也在")
        assert (
            f"提及:Agent/@{{account:{MEMBER_ACCOUNT}}},BOT yuki/@{{account:{MEMBER_ACCOUNT}}},"
            f"Yuki(显示为Agent)/@{{account:{YUKI_ACCOUNT}}}"
        ) in text


def test_literal_at_text_without_a_native_span_is_untouched() -> None:
    event = _event(1, content="@Agent 是谁", segments=({"type": "text", "data": {}},))
    renderer = ChatEventPromptRenderer((event,), self_name="Yuki")

    for text in _views(renderer, event):
        assert text.endswith("@Agent 是谁")


def test_own_outbound_lines_and_replies_to_them_use_the_character_name() -> None:
    own = _event(
        1,
        direction="outbound",
        sender_group_card="",
        content="我刚才看 Diana 已经回答他了",
        segments=(),
        author_kind="yuki",
        author_person_id=None,
        author_presence_id=PRESENCE,
    )
    reply = _event(2, content="真的吗", segments=(), reply_to_event_id=1)
    renderer = ChatEventPromptRenderer((own, reply), self_name="Yuki")

    assert renderer.render_reference_event(own).startswith(f"[Yuki|presence:{PRESENCE}]\n")
    assert f"发送者:Yuki|presence:{PRESENCE}" in renderer.render_event(own)
    assert f"回复:#1/Yuki/presence:{PRESENCE}" in renderer.render_reference_event(reply)
    assert "Agent" not in renderer.main_agent_history((own, reply))[0][2].content


def test_current_text_that_is_not_the_stored_body_is_not_rewritten_at_guessed_offsets() -> None:
    event = _event(1)
    renderer = ChatEventPromptRenderer((event,), self_name="Yuki")

    current = renderer.reference_message(
        event, current_event_id=event.id, current_content="笨蛋"
    ).content

    assert current is not None and current.endswith(">笨蛋")
    assert "提及:Yuki(显示为Agent)" in current


def test_rename_shows_the_new_character_name() -> None:
    event = _event(1)
    renderer = ChatEventPromptRenderer((event,), self_name="ゆき")

    assert renderer.render_reference_event(event).endswith(">@ゆき 笨蛋")
