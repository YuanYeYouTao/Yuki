"""History identity follows the original event rather than a reused platform account."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.repository_records import EventRecord

PERSON_A = "00000000-0000-4000-8000-000000000001"
PERSON_B = "00000000-0000-4000-8000-000000000002"
PERSON_C = "00000000-0000-4000-8000-000000000003"
PRESENCE_A = "00000000-0000-4000-8000-000000000004"
PRESENCE_B = "00000000-0000-4000-8000-000000000005"


def _event(event_id: int, **changes: object) -> EventRecord:
    event = EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"native-{event_id}",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="9912345",
        sender_group_card="同名成员",
        direction="inbound",
        content=f"原文{event_id}",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 10, 16, 12, tzinfo=UTC) + timedelta(seconds=event_id),
        group_id="2001",
        canonical_conversation_id="00000000-0000-4000-8000-000000000006",
        author_kind="person",
        author_person_id=PERSON_A,
        ingress_presence_id=PRESENCE_A,
    )
    return replace(event, **changes)


def test_rebound_native_account_does_not_relabel_or_group_old_person() -> None:
    old = _event(1)
    new = _event(2, author_person_id=PERSON_B)
    renderer = ChatEventPromptRenderer((old, new))

    assert renderer.render_reference_event(old).startswith(f"[同名成员|person:{PERSON_A}]\n")
    assert f"|person:{PERSON_A}|事件:#1" in renderer.render_event(old)
    assert renderer.render_reference_event(new).startswith(f"[同名成员|person:{PERSON_B}]\n")
    assert [ids for _, ids, _ in renderer.main_agent_history((old, new))] == [(1,), (2,)]


def test_one_person_across_real_bindings_keeps_one_immutable_sender_group() -> None:
    first = _event(1)
    other_binding = _event(2, sender_user_id="7744556", platform="telegram")
    renderer = ChatEventPromptRenderer((first, other_binding))

    history = renderer.main_agent_history((first, other_binding))

    assert [ids for _, ids, _ in history] == [(1, 2)]
    assert (history[0][2].content or "").count(f"person:{PERSON_A}") == 1


def test_human_and_yuki_with_same_native_number_keep_their_original_authors() -> None:
    human = _event(
        1,
        platform="telegram",
        bot_user_id="9000",
        sender_user_id="8000",
        sender_group_card="",
    )
    yuki = _event(
        2,
        sender_user_id="8000",
        sender_group_card="",
        author_kind="yuki",
        author_person_id=None,
        author_presence_id=PRESENCE_A,
        direction="outbound",
    )
    renderer = ChatEventPromptRenderer((human, yuki))

    assert renderer.render_reference_event(human).startswith(f"[成员|person:{PERSON_A}]\n")
    assert renderer.render_reference_event(yuki).startswith(f"[Yuki|presence:{PRESENCE_A}]\n")


def test_different_yuki_presences_are_not_collapsed_by_the_same_native_number() -> None:
    first = _event(
        1,
        author_kind="yuki",
        author_person_id=None,
        author_presence_id=PRESENCE_A,
        direction="outbound",
    )
    second = replace(first, id=2, author_presence_id=PRESENCE_B)
    renderer = ChatEventPromptRenderer((first, second))

    assert renderer.render_reference_event(first).startswith(f"[同名成员|presence:{PRESENCE_A}]\n")
    assert [ids for _, ids, _ in renderer.main_agent_history((first, second))] == [(1,), (2,)]


def test_internal_reply_uses_the_referenced_event_not_the_latest_native_owner() -> None:
    old = _event(1, sender_group_card="原发言人")
    new = _event(2, author_person_id=PERSON_B, sender_group_card="新绑定人")
    reply = _event(3, sender_user_id="7733221", author_person_id=PERSON_C, reply_to_event_id=1)
    renderer = ChatEventPromptRenderer((old, new, reply))

    for rendered in (renderer.render_reference_event(reply), renderer.render_event(reply)):
        assert f"回复:#1/原发言人/person:{PERSON_A}" in rendered
        assert f"person:{PERSON_B}" not in rendered


def test_recorded_mentioned_people_win_without_guessing_a_native_owner() -> None:
    old = _event(1, sender_group_card="原发言人")
    new = _event(2, author_person_id=PERSON_B, sender_group_card="新绑定人")
    mention = _event(
        3,
        sender_user_id="7733221",
        author_person_id=PERSON_C,
        content="[提及9912345] 看这里",
        mentioned_user_ids=("9912345",),
        mentioned_person_ids=(PERSON_A,),
    )
    renderer = ChatEventPromptRenderer((old, new, mention))

    for rendered in (renderer.render_reference_event(mention), renderer.render_event(mention)):
        assert f"提及:person:{PERSON_A}" in rendered
        assert f"person:{PERSON_B}" not in rendered
        assert "新绑定人" not in rendered
        assert "9912345" not in rendered


@pytest.mark.parametrize("recorded", [(), (PERSON_A,)])
def test_canonical_mention_segments_remain_visible_without_rebinding(
    recorded: tuple[str, ...],
) -> None:
    event = _event(
        1,
        mentioned_person_ids=recorded,
        segments=({"type": "mention", "data": {"person_id": PERSON_B}},),
    )
    rendered = ChatEventPromptRenderer((event,)).render_reference_event(event)

    assert f"提及:person:{PERSON_A if recorded else PERSON_B}" in rendered
    if recorded:
        assert f"person:{PERSON_B}" not in rendered


@pytest.mark.parametrize("legacy_projection", [False, True])
def test_raw_mentions_are_display_only_in_an_explicit_old_projection(
    legacy_projection: bool,
) -> None:
    old = _event(1, sender_group_card="原发言人")
    mention = _event(
        2,
        sender_user_id="7733221",
        author_person_id=PERSON_C,
        content="[提及9912345] 看这里",
        projected_content="[提及9912345] 看这里" if legacy_projection else None,
        mentioned_user_ids=("9912345",),
    )
    future = _event(3, author_person_id=PERSON_B, sender_group_card="新绑定人")
    rendered = ChatEventPromptRenderer((old, mention, future)).render_reference_event(mention)

    assert "新绑定人" not in rendered
    assert f"person:{PERSON_A}" not in rendered
    assert f"person:{PERSON_B}" not in rendered
    if legacy_projection:
        assert "提及:原发言人" in rendered
        assert rendered.endswith(">[提及原发言人] 看这里")
    else:
        assert "|提及:" not in rendered
        assert rendered.endswith(">[提及成员] 看这里")


def test_old_frozen_paid_fragment_is_not_rewritten_after_rebinding() -> None:
    old = _event(1)
    new = _event(2, author_person_id=PERSON_B)
    paid_text = "[同名成员|QQ:9912345]\n#1>原已付字节"
    frozen = FrozenFragments.load(
        [
            {
                "kind": "model_input",
                "event_ids": [1],
                "message": {"role": "user", "content": paid_text},
            }
        ]
    )
    renderer = ChatEventPromptRenderer((old, new))
    grouped, individual = renderer.main_agent_history_views((old, new))

    extended = frozen.extend_history(
        ((ids, message) for _, ids, message in grouped),
        ((ids, message) for _, ids, message in individual),
    )

    messages = extended.messages()
    assert messages[0].content == paid_text
    assert (messages[1].content or "").startswith(f"[20:00:02｜同名成员|person:{PERSON_B}]")
