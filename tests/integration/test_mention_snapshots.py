"""Native mention positions freeze local display names and original target ownership."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from sqlalchemy import event, select
from tests.support.memory_identity import transfer_account
from tests.support.user_profiles_helpers import observe_members
from tests.unit.test_canonical_ingress import _Bot, _stack
from tests.unit.test_normalizer import group_event

from yuki.adapters.onebot.normalizer import normalize_event
from yuki.conversation.media_index import prepare_event_media
from yuki.domain.messages import MessageMention
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.identity import ingress
from yuki.identity.canonical_repository import ensure_presence, find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
from yuki.persistence.models import ChatEventModel, MembershipModel
from yuki.services.processor import _input_content


async def _qq(database):
    registry, resolver, uow = await _stack(database)
    await observe_members(database, ("1001", "1002", "1003"))
    async with database.immediate_session() as session:
        presence = await ensure_presence(session, "9999")
    bot = _Bot("9999")
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="9999", presence_id=presence)
    return bot, resolver, uow


@pytest.mark.asyncio
async def test_names_stay_in_native_positions_and_replay_keeps_original_snapshot(database):
    bot, resolver, uow = await _qq(database)
    native = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment.text("  [提及1002] "),
                    MessageSegment.at(1002),
                    MessageSegment.text("请问"),
                    MessageSegment.at(1003),
                    MessageSegment.at(1002),
                    MessageSegment.at("all"),
                ]
            )
        )
    )
    admitted = await resolver.pre_admit(bot, native)
    assert not admitted.dropped
    assert admitted.message.text == "[提及1002] @card-1002请问@card-1003@card-1002@全体成员"
    assert not admitted.message.mentions_bot
    first = await uow.append_inbound(admitted.message, admitted)
    snapshots = [item["data"] for item in first.event.segments if item["type"] == "mention"]
    assert [item["display_name"] for item in snapshots] == [
        "card-1002",
        "card-1003",
        "card-1002",
        "全体成员",
    ]
    assert snapshots[0]["person_id"] == snapshots[2]["person_id"]
    assert all(item["presence_id"] is None for item in snapshots)
    async with database.immediate_session() as session:
        target = await find_identity_binding(session, "1002")
        target.display_name = "current-platform-nickname"
        member = await session.get(MembershipModel, (target.person_id, admitted.space_id))
        member.group_card = ""
        person = await session.get(CanonicalPersonModel, target.person_id)
        person.display_name = "independent-person-name"
    retried = await resolver.pre_admit(bot, native)
    assert "@current-platform-nickname" in retried.message.text
    duplicate = await uow.append_inbound(retried.message, retried)
    assert not duplicate.created and duplicate.event.id == first.event.id
    assert duplicate.event.content == first.event.content
    assert duplicate.event.segments == first.event.segments


@pytest.mark.asyncio
async def test_retired_presence_is_member_and_quote_keeps_accepted_names(database):
    bot, resolver, uow = await _qq(database)
    async with database.immediate_session() as session:
        retired_id = await ensure_presence(session, "8001")
        retired = await session.get(PresenceModel, retired_id)
        retired.enabled = False
    original = normalize_event(
        group_event(
            Message([MessageSegment.at(1002), MessageSegment.text("旧问题")]), message_id=701
        )
    )
    admitted = await resolver.pre_admit(bot, original)
    recorded = await uow.append_inbound(admitted.message, admitted)
    old_person = admitted.message.mentions[0].person_id
    await transfer_account(database, "1002", "1003")
    async with database.immediate_session() as session:
        target = await find_identity_binding(session, "1002")
        member = await session.get(MembershipModel, (target.person_id, admitted.space_id))
        member.group_card = "新群名片"
    current = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment.at(1002),
                    MessageSegment.text("现在的问题 "),
                    MessageSegment.at(8001),
                    MessageSegment.text(" 以及纯文字@Yuki"),
                ]
            ),
            message_id=702,
        )
    )
    current = replace(
        current, reply_to_message_id=original.message_id, reply_text="伪造引用@今日名字"
    )
    reply = await resolver.pre_admit(bot, current)
    assert not reply.message.mentions_bot
    assert reply.message.reply_to_event_id == recorded.event.id
    assert reply.message.reply_text == "@card-1002旧问题"
    assert reply.message.text == "@新群名片现在的问题 @未识别成员 以及纯文字@Yuki"
    assert reply.message.mentions[1].person_id is None
    assert reply.message.mentions[1].presence_id is None
    assert reply.message.mentions[0].person_id != old_person
    assert reply.message.reply_segments[0]["data"]["person_id"] == old_person
    content = _input_content(reply.message, reply.message.text)
    assert "@card-1002旧问题" in content and "@新群名片现在的问题" in content
    assert "伪造引用" not in content


@pytest.mark.asyncio
async def test_captured_quote_without_keeper_keeps_its_native_name(database):
    bot, resolver, _uow = await _qq(database)
    native = normalize_event(group_event(Message("当前句子"), message_id=750))
    native = replace(
        native,
        reply_to_message_id="not-observed",
        reply_text="[提及1002] 原引用",
        reply_segments=(
            {"type": "at", "data": {"qq": "1002", "name": "捕获旧群名片"}},
            {"type": "text", "data": {"text": " 原引用"}},
        ),
        reply_mentions=(MessageMention("member", 0, 0, len("[提及1002]"), "1002", "捕获旧群名片"),),
    )
    admitted = await resolver.pre_admit(bot, native)
    assert admitted.message.reply_to_event_id is None
    assert admitted.message.reply_text == "@捕获旧群名片 原引用"
    assert admitted.message.reply_mentions[0].person_id is None
    assert "card-1002" not in admitted.message.reply_text


@pytest.mark.asyncio
async def test_native_names_win_for_member_and_multiple_yuki_presences(database):
    bot, resolver, _uow = await _qq(database)
    async with database.immediate_session() as session:
        await ensure_presence(session, "8001")
    native = normalize_event(
        group_event(
            Message(
                [
                    MessageSegment("at", {"qq": "9999", "name": "本账号群名片"}),
                    MessageSegment("at", {"qq": "8001", "name": "另一个账号名片"}),
                    MessageSegment("at", {"qq": "1002", "name": "本次原生名片"}),
                    MessageSegment.image("https://invalid.test/image.png"),
                ]
            ),
            message_id=760,
        )
    )
    admitted = await resolver.pre_admit(bot, native)
    assert admitted.message.text.startswith("@本账号群名片@另一个账号名片@本次原生名片")
    assert admitted.message.mentions_bot
    assert admitted.message.mentions[0].presence_id != admitted.message.mentions[1].presence_id
    items = prepare_event_media(
        event_id=1,
        conversation_id=admitted.conversation_id,
        generation=1,
        segments=admitted.message.segments,
    )
    assert len(items) == 1 and items[0].attachment_index == 0
    assert items[0].segment_index == native.attachments[0].segment_index


@pytest.mark.asyncio
@pytest.mark.parametrize("mention_self", [False, True])
async def test_mention_read_preparation_precedes_new_person_first_write(
    database, monkeypatch, mention_self
):
    bot, resolver, _uow = await _qq(database)
    warmup = normalize_event(group_event(Message("warmup"), message_id=770))
    await resolver.pre_admit(bot, warmup)
    writes = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            writes.append(statement)

    prepare = ingress.resolve_message_mentions
    prepared = False

    async def check(session, message, **kwargs):
        nonlocal prepared
        assert not writes
        result = await prepare(session, message, **kwargs)
        assert not writes
        prepared = True
        return result

    monkeypatch.setattr(ingress, "resolve_message_mentions", check)
    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        target = 76543210 if mention_self else 1002
        native = normalize_event(group_event(Message([MessageSegment.at(target)]), message_id=771))
        native = replace(native, sender=replace(native.sender, user_id="76543210"))
        admitted = await resolver.pre_admit(bot, native)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert prepared and writes
    assert not admitted.dropped
    assert admitted.message.text == ("@card" if mention_self else "@card-1002")
    if mention_self:
        assert admitted.message.mentions[0].person_id == admitted.author_person_id


def test_telegram_native_entities_keep_utf16_positions_caption_and_reply_names():
    client = SimpleNamespace(self_id="7", username="yuki_bot")
    raw = {
        "message_id": 1,
        "date": 1792280000,
        "chat": {"id": -40, "type": "group", "title": "group"},
        "from": {"id": 1, "first_name": "sender"},
        "caption": "😀Bob 和 @friend，字面@Yuki",
        "caption_entities": [
            {
                "type": "text_mention",
                "offset": 2,
                "length": 3,
                "user": {"id": 2, "first_name": "原名", "last_name": "同学"},
            },
            {"type": "mention", "offset": 8, "length": 7},
        ],
        "photo": [{"file_id": "photo", "file_size": 3}],
        "reply_to_message": {
            "message_id": 5,
            "from": {"id": 3},
            "text": "昵称",
            "entities": [
                {
                    "type": "text_mention",
                    "offset": 0,
                    "length": 2,
                    "user": {"id": 9, "first_name": "过去名字"},
                }
            ],
        },
    }
    message = normalize_message(raw, client)
    assert not message.mentions_bot
    assert message.mentioned_user_ids == ("2",)
    assert [message.text[item.text_start : item.text_end] for item in message.mentions] == [
        "Bob",
        "@friend",
    ]
    assert message.mentions[0].display_name == "原名 同学"
    assert message.mentions[1].target_user_id is None
    assert message.attachments[0].segment_index == len(message.segments) - 1
    assert message.reply_mentions[0].display_name == "过去名字"


@pytest.mark.asyncio
async def test_missing_name_uses_local_source_display_only_without_claiming_old_owner(database):
    bot, resolver, uow = await _qq(database)
    async with database.immediate_session() as session:
        retired = await session.get(PresenceModel, await ensure_presence(session, "8001"))
        retired.enabled = False
    original = normalize_event(group_event(Message("hello"), message_id=801))
    first = await resolver.pre_admit(bot, original)
    recorded = await uow.append_inbound(first.message, first)
    async with database.immediate_session() as session:
        row = await session.get(ChatEventModel, recorded.event.id)
        row.sender_user_id = "8001"
        row.sender_group_card = "退役账号群名片"
    native = normalize_event(group_event(Message([MessageSegment.at(8001)]), message_id=802))
    native = replace(native, received_at=recorded.event.occurred_at + timedelta(seconds=1))
    admitted = await resolver.pre_admit(bot, native)
    assert admitted.message.text == "@退役账号群名片"
    assert not admitted.message.mentions_bot
    assert admitted.message.mentions[0].person_id is None
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(CanonicalPersonModel.id).where(
                    CanonicalPersonModel.id == recorded.event.author_person_id
                )
            )
            is not None
        )
