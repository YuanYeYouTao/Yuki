"""QQ official Bot delivery: passive msg_id/msg_seq, no-window paths and native layout.

Failure cases written before the implementation (taskbook 4, items 12/14/15/17 and
the adapter-side facts they rely on):

12. A group reply whose passive window expired (40034128) is reported as delivered,
    retried, or sent somewhere else (another chat, private chat, another platform).
14. A delivery without a passive window to a group with no inbound message sends a
    group message without ``msg_id``, or isn't a definite "no delivery path" failure;
    with inbound messages it isn't attached to the newest one.
15. After a restart the ``msg_seq`` for one ``msg_id`` starts from 1 again (it must
    follow the durable delivery records), or 40054005 is retried more than once.
17. A private delivery without a passive window doesn't try ``is_wakeup`` first, doesn't
    retry once as an ordinary proactive message after 40034122/40034123, or a refused
    retry is still reported as delivered.
- Quote replies lose the REFIDX_ the platform requires, outbound REFIDX_ isn't kept.
- Group @ isn't written as ``<qqbot-at-user id=… />`` inside a Markdown message.
- Media isn't uploaded in parts and sent as ``msg_type`` 7 with the returned file_info.
- Text with media is not split into separately receipted deliveries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.support.qq_official import FakeQQPlatform

from yuki.gateway.providers.qq_official import (
    QQOfficialAPIError,
    QQOfficialClient,
    QQOfficialConnection,
    QQOfficialNoDeliveryPath,
    QQOfficialReplyLimit,
)
from yuki.gateway.providers.qq_official_social import QQOfficialSocialOperations, send_message
from yuki.gateway.providers.qq_official_store import record_message
from yuki.gateway.social import TransportMedia, TransportMention, TransportMessage, TransportTarget
from yuki.identity.canonical_repository import create_presence

GROUP = TransportTarget("space", "G1")
PRIVATE = TransportTarget("person", "U1")


@pytest.fixture
async def qq(database):
    platform = FakeQQPlatform()
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id="BOT", platform="qq_official", now=datetime.now(UTC)
        )
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        client.self_id = "BOT"
        yield platform, QQOfficialConnection(client, database, presence.id)


async def test_group_reply_limit_is_one_definite_failure(qq):
    platform, connection = qq
    platform.reply("POST", "/v2/groups/G1/messages", 400, code=40034128, message="limit")
    with pytest.raises(QQOfficialReplyLimit) as refused:
        await send_message(connection, GROUP, TransportMessage("hi"), reply_to="IN-1")
    assert refused.value.uncertain is False
    assert [item.path for item in platform.messages()] == ["/v2/groups/G1/messages"]
    assert platform.messages()[0].body["msg_id"] == "IN-1"
    # A later reply to the same message reuses seq 1: nothing was accepted.
    await send_message(connection, GROUP, TransportMessage("again"), reply_to="IN-1")
    assert platform.messages()[-1].body["msg_seq"] == 1


async def test_group_without_window_has_no_path_or_uses_newest_inbound(qq, database):
    platform, connection = qq
    operations = QQOfficialSocialOperations()
    with pytest.raises(QQOfficialNoDeliveryPath) as missing:
        await operations.send(connection, GROUP, TransportMessage("scheduled"))
    assert missing.value.uncertain is False
    assert platform.messages() == []

    now = datetime.now(UTC)
    for message_id, minutes in (("IN-old", 3), ("IN-new", 1), ("IN-other-group", 0)):
        await record_message(
            database,
            presence_id=connection.presence_id,
            message_id=message_id,
            chat_kind="group",
            chat_id="G2" if message_id == "IN-other-group" else "G1",
            direction="inbound",
            created_at=now - timedelta(minutes=minutes),
        )
    await operations.send(connection, GROUP, TransportMessage("scheduled"))
    body = platform.messages()[-1].body
    assert body["msg_id"] == "IN-new" and body["msg_seq"] == 1
    assert all("msg_id" in item.body for item in platform.messages("/v2/groups/"))
    assert platform.messages("/v2/users/") == []


async def test_msg_seq_follows_durable_records_and_dedup_retries_once(qq, database):
    platform, connection = qq
    for seq in (1, 2):
        await record_message(
            database,
            presence_id=connection.presence_id,
            message_id=f"OLD-{seq}",
            chat_kind="group",
            chat_id="G1",
            direction="outbound",
            reply_to="IN-1",
            msg_seq=seq,
        )
    # A new client is a restarted process: the in-memory side starts empty.
    restarted = QQOfficialConnection(connection.client, database, connection.presence_id)
    await send_message(restarted, GROUP, TransportMessage("third"), reply_to="IN-1")
    assert platform.messages()[-1].body["msg_seq"] == 3

    platform.reply("POST", "/v2/groups/G1/messages", 400, code=40054005, message="dedup")
    receipt = await send_message(restarted, GROUP, TransportMessage("fourth"), reply_to="IN-1")
    assert [item.body["msg_seq"] for item in platform.messages()[-2:]] == [4, 5]
    await send_message(restarted, GROUP, TransportMessage("fifth"), reply_to="IN-1")
    assert platform.messages()[-1].body["msg_seq"] == 6
    assert receipt.reference == f"OUT-{platform.sent - 1}"

    before = len(platform.messages())
    for _ in range(2):
        platform.reply("POST", "/v2/groups/G1/messages", 400, code=40054005, message="dedup")
    with pytest.raises(QQOfficialAPIError) as duplicate:
        await send_message(restarted, GROUP, TransportMessage("sixth"), reply_to="IN-1")
    assert duplicate.value.uncertain is False
    assert len(platform.messages()) == before + 2


@pytest.mark.parametrize("refusal", [40034122, 40034123])
async def test_private_without_window_wakes_up_then_one_proactive_retry(qq, refusal):
    platform, connection = qq
    operations = QQOfficialSocialOperations()
    receipt = await operations.send(connection, PRIVATE, TransportMessage("reminder"))
    assert platform.messages()[-1].body == {"msg_type": 0, "content": "reminder", "is_wakeup": True}
    assert receipt.reference == "OUT-1"

    platform.reply("POST", "/v2/users/U1/messages", 400, code=refusal, message="no wakeup")
    receipt = await operations.send(connection, PRIVATE, TransportMessage("reminder"))
    first, second = (item.body for item in platform.messages()[-2:])
    assert first["is_wakeup"] is True and "is_wakeup" not in second
    assert "msg_id" not in second and receipt.reference == "OUT-2"

    before = len(platform.messages())
    platform.reply("POST", "/v2/users/U1/messages", 400, code=refusal, message="no wakeup")
    platform.reply("POST", "/v2/users/U1/messages", 400, code=40034102, message="no proactive")
    with pytest.raises(QQOfficialAPIError) as refused:
        await operations.send(connection, PRIVATE, TransportMessage("reminder"))
    assert refused.value.uncertain is False
    assert len(platform.messages()) == before + 2
    assert platform.messages("/v2/groups/") == []


async def test_quotes_keep_refidx_both_ways(qq, database):
    platform, connection = qq
    await record_message(
        database,
        presence_id=connection.presence_id,
        message_id="IN-1",
        chat_kind="group",
        chat_id="G1",
        direction="inbound",
        ref_idx="REFIDX_IN-1",
    )
    await send_message(
        connection, GROUP, TransportMessage("quoted", reply_reference="IN-1"), reply_to="IN-1"
    )
    assert platform.messages()[-1].body["message_reference"] == {"message_id": "REFIDX_IN-1"}
    # The outbound REFIDX_ returned by the platform makes Yuki's own message quotable.
    await send_message(
        connection, GROUP, TransportMessage("self", reply_reference="OUT-1"), reply_to="IN-1"
    )
    assert platform.messages()[-1].body["message_reference"] == {"message_id": "REFIDX_OUT-1"}


async def test_group_mentions_use_native_markdown_tag(qq):
    platform, connection = qq
    text = "你好 @小明 看这里"
    await send_message(
        connection,
        GROUP,
        TransportMessage(text, mentions=(TransportMention("MEMBER-1", "小明", 3, 6),)),
        reply_to="IN-1",
    )
    body = platform.messages()[-1].body
    assert body["msg_type"] == 2
    assert body["markdown"]["content"] == '你好 <qqbot-at-user id="MEMBER-1" /> 看这里'


async def test_media_is_uploaded_in_parts_and_sent_as_rich_media(qq):
    platform, connection = qq
    operations = QQOfficialSocialOperations()
    media = TransportMedia("image", b"12345678", "a.png", "image/png")
    parts = operations.plan_send(TransportMessage("caption", media=media))
    assert [(part.text, part.media is not None) for part in parts] == [
        ("caption", False),
        ("", True),
    ]
    await send_message(connection, GROUP, parts[1], reply_to="IN-1")
    paths = [item.path for item in platform.requests if item.host != "bots.qq.com"]
    assert paths == [
        "/v2/groups/G1/upload_prepare",
        "/part-1",
        "/v2/groups/G1/upload_part_finish",
        "/part-2",
        "/v2/groups/G1/upload_part_finish",
        "/v2/groups/G1/files",
        "/v2/groups/G1/messages",
    ]
    prepare = platform.requests[1].body
    assert prepare["file_type"] == 1 and prepare["file_size"] == "8"
    puts = [item.body for item in platform.requests if item.host == "cos.example"]
    assert puts == [b"1234", b"5678"]
    assert platform.messages()[-1].body == {
        "msg_type": 7,
        "media": {"file_info": "FILE-INFO-1"},
        "msg_id": "IN-1",
        "msg_seq": 1,
    }


async def test_recall_deletes_the_original_platform_message(qq):
    platform, connection = qq
    await QQOfficialSocialOperations().recall(connection, GROUP, "OUT-9")
    assert (platform.requests[-1].method, platform.requests[-1].path) == (
        "DELETE",
        "/v2/groups/G1/messages/OUT-9",
    )
