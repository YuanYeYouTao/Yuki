"""OneBot event normalization and unsupported-content tests."""

import json

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Reply, Sender

from qq_ai_bot.adapters.onebot.normalizer import normalize_event, reproject_inbound_mentions
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import AttachmentKind


def private_event(
    message: Message, *, message_id: int = 1, user_id: int = 1001
) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=9999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=user_id, nickname="tester"),
    )


def group_event(message: Message, *, message_id: int = 2) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=9999,
        post_type="message",
        sub_type="normal",
        user_id=1001,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=1001, nickname="tester", card="card"),
        group_id=2001,
    )


def test_private_text_and_group_mention_normalize() -> None:
    private = normalize_event(private_event(Message("hello")))
    group = normalize_event(
        group_event(Message([MessageSegment.at(9999), MessageSegment.text(" question")]))
    )
    assert private.scope_type is ScopeType.PRIVATE and private.text == "hello"
    assert private.sender.nickname == "tester" and not private.sender.group_card
    assert group.scope_type is ScopeType.GROUP and group.mentions_bot
    assert group.text == "[提及Yuki] question" and group.group_id == "2001"
    assert group.sender.nickname == "tester" and group.sender.group_card == "card"


def test_group_mention_uses_original_message_after_nonebot_strips_at() -> None:
    original = Message([MessageSegment.at(9999), MessageSegment.text(" question")])
    event = group_event(Message("question"))
    event.original_message = original
    event.to_me = True

    normalized = normalize_event(event)

    assert normalized.mentions_bot
    assert normalized.text == "[提及Yuki] question"


def test_group_message_with_only_bot_mention_keeps_empty_text_trigger() -> None:
    normalized = normalize_event(group_event(Message([MessageSegment.at(9999)])))

    assert normalized.mentions_bot
    assert normalized.text == "[提及Yuki]"


def test_original_message_is_the_only_authoritative_segment_source() -> None:
    original = Message(
        [
            MessageSegment.text("原始"),
            MessageSegment.at(12345678),
            MessageSegment.text("内容"),
        ]
    )
    event = group_event(Message("被适配器改写的内容"))
    event.original_message = original

    normalized = normalize_event(event)

    assert normalized.text == "原始[提及成员1]内容"
    assert normalized.mentioned_user_ids == ("12345678",)
    assert "被适配器改写" not in normalized.text


def test_mentions_keep_order_and_reuse_member_indices_across_yuki_presences() -> None:
    normalized = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment.at(8001),
                    MessageSegment.text("和"),
                    MessageSegment.at(12345678),
                    MessageSegment.at("all"),
                    MessageSegment.at(12345678),
                    MessageSegment.at(87654321),
                    MessageSegment.at(9999),
                ]
            )
        ),
        yuki_account_ids=frozenset({"8001"}),
    )

    assert normalized.mentions_bot
    assert normalized.mentioned_user_ids == ("12345678", "87654321")
    assert normalized.text == (
        "[提及Yuki]和[提及成员1][提及全体成员][提及成员1][提及成员2][提及Yuki]"
    )


def test_canonical_reprojection_reclassifies_another_presence() -> None:
    normalized = normalize_event(
        group_event(Message([MessageSegment.at(8001), MessageSegment.text("回来啦")]))
    )
    assert normalized.text == "[提及成员1]回来啦"
    assert normalized.mentioned_user_ids == ("8001",)

    projected = reproject_inbound_mentions(normalized, frozenset({"9999", "8001"}))

    assert projected.text == "[提及Yuki]回来啦"
    assert projected.mentions_bot
    assert projected.mentioned_user_ids == ()
    assert projected.yuki_account_ids == frozenset({"9999", "8001"})


def test_other_member_mentions_use_opaque_placeholders() -> None:
    normalized = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment.at(9999),
                    MessageSegment.at(12345678),
                    MessageSegment.text("叫小明"),
                ]
            )
        )
    )

    assert normalized.mentioned_user_ids == ("12345678",)
    assert "[提及成员1]叫小明" in normalized.text
    assert "12345678" not in normalized.text


def test_at_all_is_not_exposed_as_a_user_target() -> None:
    normalized = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment.at(9999),
                    MessageSegment.at("all"),
                    MessageSegment.text("看看"),
                ]
            )
        )
    )

    assert normalized.mentioned_user_ids == ()
    assert "[提及全体成员]看看" in normalized.text


def test_reply_text_and_face_placeholder_are_supported() -> None:
    message = Message([MessageSegment.at(9999), MessageSegment.face(14), MessageSegment.text("ok")])
    event = group_event(message)
    event.reply = Reply(
        time=1,
        message_type="group",
        message_id=8,
        real_id=8,
        sender=Sender(user_id=1002),
        message=Message("quoted"),
    )
    normalized = normalize_event(event)
    assert normalized.reply_text == "quoted"
    assert "[QQ表情：微笑]" in normalized.text


def test_reply_text_uses_the_same_yuki_and_member_projection() -> None:
    event = group_event(Message("继续"))
    event.reply = Reply(
        time=1,
        message_type="group",
        message_id=8,
        real_id=8,
        sender=Sender(user_id=1002),
        message=Message(
            [
                MessageSegment.at(8001),
                MessageSegment.text("与"),
                MessageSegment.at(12345678),
            ]
        ),
    )

    normalized = normalize_event(event, yuki_account_ids=frozenset({"8001"}))

    assert normalized.reply_text == "[提及Yuki]与[提及成员1]"


def test_image_attachment_has_stable_history_marker() -> None:
    normalized = normalize_event(
        private_event(Message(MessageSegment.image("https://invalid.test/a")))
    )
    assert normalized.text == "[图片附件0]"
    assert normalized.attachments[0].kind is AttachmentKind.IMAGE
    assert normalized.attachments[0].label == "image"


def test_netease_album_share_card_becomes_bounded_chat_context() -> None:
    payload = {
        "app": "com.tencent.tuwen.lua",
        "meta": {
            "news": {
                "desc": "by MC啊显/MC赵小六",
                "jumpUrl": "https://y.music.163.com/m/album?id=242154493&userid=1001",
                "tag": "网易云音乐",
                "title": "分享专辑: 中国有弹舌",
            }
        },
        "prompt": "[分享]分享专辑: 中国有弹舌",
        "view": "news",
    }
    normalized = normalize_event(
        private_event(Message(MessageSegment.json(json.dumps(payload, ensure_ascii=False))))
    )

    assert normalized.attachments[0].kind is AttachmentKind.CARD
    assert normalized.attachments[0].summary == "分享专辑: 中国有弹舌"
    assert normalized.attachments[0].url == (
        "https://y.music.163.com/m/album?id=242154493&userid=1001"
    )
    assert "用户分享了一个网易云专辑" in normalized.text
    assert "网易云专辑 ID：242154493" in normalized.text
    assert "中国有弹舌" in normalized.text
