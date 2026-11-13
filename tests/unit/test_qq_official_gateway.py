"""QQ official Bot WebSocket session and inbound projection.

Failure cases written before the implementation:

- Gateway close 4004 doesn't drop the cached token, so reconnects keep using it.
- 4013/4014 (intent invalid/not permitted) reconnects with the same intents forever,
  or drops the group/C2C message intent instead of the optional ones.
- A dropped session with a session id starts a new IDENTIFY instead of RESUME with the
  last sequence; OP 9 keeps resuming the invalid session.
- A half-open connection (no frame within two heartbeat intervals) is never detected.
- Health lacks ``last_error_category``/``last_success_at`` or reports healthy after a
  rejection; the Registry keeps a connection after ``close()``.
- Full-mode group messages are all treated as addressed to Yuki, or ``mentions[].is_you``
  is ignored; other bots look like people; member/user openids are not the sender;
  quoted content (message_type 103, msg_elements) or attachments are lost.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.support.qq_official import FakeQQPlatform
from websockets.asyncio.server import serve

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import AttachmentKind
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.qq_official import QQOfficialClient
from yuki.gateway.providers.qq_official_ingress import (
    INTENT_GROUP_AND_C2C,
    INTENT_GROUP_MEMBERS,
    INTENT_INTERACTION,
    QQOfficialGateway,
    normalize_message,
)
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.routing import PresenceRouter

READY = {"session_id": "S1", "user": {"id": "BOT", "username": "Yuki"}}


@pytest.fixture
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


async def run_gateway(database, script, *, interval=30_000, until):
    """Serve one scripted handler per connection and stop when ``until`` is set."""
    frames: list[list[dict]] = []
    platform = FakeQQPlatform()

    async def handler(connection):
        index = len(frames)
        frames.append([])
        await connection.send(json.dumps({"op": 10, "d": {"heartbeat_interval": interval}}))
        frames[index].append(json.loads(await connection.recv()))
        await script[min(index, len(script) - 1)](connection, frames[index])

    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    async with serve(handler, "127.0.0.1", 0) as listener:
        platform.gateway_url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        async with platform.client() as http:
            gateway = QQOfficialGateway(
                database,
                registry,
                PresenceRouter(database, registry),
                SimpleNamespace(),
                SimpleNamespace(),
                QQOfficialClient("1020", "secret-value", client=http),
                retry_delay=0.01,
            )
            await gateway.start()
            try:
                await asyncio.wait_for(until.wait(), 5)
                health = await gateway.health()
                active = registry.has_any_active(platform="qq_official")
            finally:
                await gateway.close()
    return SimpleNamespace(
        frames=frames,
        health=health,
        active=active,
        closed_active=registry.has_any_active(platform="qq_official"),
        gateway=gateway,
    )


def dispatch(kind, data, seq):
    return json.dumps({"op": 0, "s": seq, "t": kind, "d": data})


async def test_intent_rejection_drops_optional_intents_then_connects(database, no_proxy):
    ready = asyncio.Event()

    async def reject(connection, _frames):
        await connection.close(4014, "intent not allowed")

    async def accept(connection, _frames):
        await connection.send(dispatch("READY", READY, 1))
        ready.set()
        await connection.wait_closed()

    result = await run_gateway(database, [reject, reject, accept], until=ready)
    intents = [frames[0]["d"]["intents"] for frames in result.frames]
    full = INTENT_GROUP_AND_C2C | INTENT_GROUP_MEMBERS | INTENT_INTERACTION
    assert intents == [full, INTENT_GROUP_AND_C2C | INTENT_INTERACTION, INTENT_GROUP_AND_C2C]
    assert result.active and not result.closed_active
    assert result.health["healthy"] is True
    assert result.health["last_error_category"] is None
    assert result.health["last_success_at"] is not None


async def test_resume_with_last_sequence_and_identify_after_invalid_session(database, no_proxy):
    ready = asyncio.Event()

    async def first(connection, _frames):
        await connection.send(dispatch("READY", READY, 1))
        await connection.send(dispatch("UNHANDLED_EVENT", {}, 5))
        await connection.close(4009, "session timed out")

    async def invalid(connection, _frames):
        await connection.send(json.dumps({"op": 9, "d": False}))
        await connection.wait_closed()

    async def identify(connection, _frames):
        await connection.send(dispatch("READY", {**READY, "session_id": "S2"}, 1))
        ready.set()
        await connection.wait_closed()

    result = await run_gateway(database, [first, invalid, identify], until=ready)
    opening = [frames[0] for frames in result.frames]
    assert [frame["op"] for frame in opening] == [2, 6, 2]
    assert opening[1]["d"]["session_id"] == "S1" and opening[1]["d"]["seq"] == 5
    assert opening[1]["d"]["token"] == "QQBot token-1"


async def test_invalid_token_close_fetches_a_new_token(database, no_proxy):
    ready = asyncio.Event()

    async def invalid_token(connection, _frames):
        await connection.close(4004, "invalid token")

    async def accept(connection, _frames):
        await connection.send(dispatch("READY", READY, 1))
        ready.set()
        await connection.wait_closed()

    result = await run_gateway(database, [invalid_token, accept], until=ready)
    tokens = [frames[0]["d"]["token"] for frames in result.frames]
    assert tokens == ["QQBot token-1", "QQBot token-2"]


async def test_half_open_connection_is_replaced(database, no_proxy):
    reconnected = asyncio.Event()

    async def silent(connection, frames):
        await connection.send(dispatch("READY", READY, 1))
        async for frame in connection:  # Heartbeats are read but never acknowledged.
            frames.append(json.loads(frame))

    async def second(connection, _frames):
        reconnected.set()
        await connection.wait_closed()

    result = await run_gateway(database, [silent, second], interval=50, until=reconnected)
    assert len(result.frames) >= 2
    heartbeats = [frame for frame in result.frames[0] if frame["op"] == 1]
    assert heartbeats and heartbeats[0]["d"] == 1
    assert result.frames[1][0]["op"] == 6  # The half-open session is resumed, not dropped.


async def test_rejection_is_visible_in_health(database, no_proxy):
    retried = asyncio.Event()

    async def reject(connection, _frames):
        await connection.close(4914, "bot offline")

    async def unanswered(connection, _frames):
        retried.set()
        await connection.wait_closed()

    result = await run_gateway(database, [reject, unanswered], until=retried)
    assert set(result.health) >= {
        "enabled",
        "running",
        "healthy",
        "last_error_category",
        "last_success_at",
    }
    assert result.health["healthy"] is False
    assert not result.closed_active


def group_message(**overrides):
    data = {
        "id": "ROBOT1.0_msg",
        "content": "今天天气不错",
        "group_openid": "GROUP-1",
        "timestamp": "2026-11-13T10:00:00+08:00",
        "author": {"id": "AUTHOR-1", "member_openid": "MEMBER-1"},
    }
    return {**data, **overrides}


def test_full_mode_group_message_is_only_addressed_by_is_you():
    plain = normalize_message("GROUP_MESSAGE_CREATE", group_message(), self_id="BOT")
    assert plain is not None and plain.mentions_bot is False
    assert plain.scope_type is ScopeType.GROUP and plain.group_id == "GROUP-1"
    assert plain.sender.user_id == "MEMBER-1" and plain.platform == "qq_official"
    assert plain.native_message_id == plain.ingress_key == "ROBOT1.0_msg"
    assert plain.occurred_at == datetime(2026, 11, 13, 2, 0, tzinfo=UTC)

    called = normalize_message(
        "GROUP_MESSAGE_CREATE",
        group_message(
            content="<@BOTX> 在干嘛 <@!MX>",
            mentions=[
                {"id": "BOTX", "is_you": True},
                {"id": "MX", "member_openid": "MEMBER-2", "username": "小明"},
            ],
        ),
        self_id="BOT",
    )
    assert called is not None and called.mentions_bot is True
    assert [(item.kind, item.target_user_id) for item in called.mentions] == [
        ("yuki", "BOT"),
        ("member", "MEMBER-2"),
    ]
    assert "<@" not in called.text and called.text.endswith("@小明")
    assert called.mentioned_user_ids == ("MEMBER-2",)

    at = normalize_message("GROUP_AT_MESSAGE_CREATE", group_message(content=" 你好"), self_id="BOT")
    assert at is not None and at.mentions_bot is True and at.text == "你好"


def test_private_sender_other_bots_quotes_and_media():
    private = normalize_message(
        "C2C_MESSAGE_CREATE",
        {
            "id": "C2C-1",
            "content": "hi <faceType=1,faceId=\"14\",ext=\"eyJ0ZXh0Ijoi5b6u56yRIn0=\">",
            "author": {"id": "AUTHOR-1", "user_openid": "MEMBER-1"},
            "attachments": [
                {
                    "content_type": "image/png",
                    "url": "multimedia.nt.qq.com.cn/a.png",
                    "filename": "a.png",
                    "size": 10,
                }
            ],
        },
        self_id="BOT",
    )
    assert private is not None and private.scope_type is ScopeType.PRIVATE
    # member_openid and user_openid are one account inside one Bot.
    assert private.sender.user_id == "MEMBER-1"
    assert private.text == "hi [表情:微笑]"
    assert [(item.kind, item.file) for item in private.attachments] == [
        (AttachmentKind.IMAGE, "https://multimedia.nt.qq.com.cn/a.png")
    ]

    bot = normalize_message(
        "GROUP_AT_MESSAGE_CREATE",
        group_message(author={"id": "OTHER", "member_openid": "OTHER-M", "bot": True}),
        self_id="BOT",
    )
    assert bot is not None and bot.sender.is_bot is True

    quoted = normalize_message(
        "GROUP_MESSAGE_CREATE",
        group_message(
            content="这是什么",
            message_type=103,
            msg_elements=[
                {
                    "content": "原来的话",
                    "attachments": [{"content_type": "file", "url": "https://x/f", "filename": "f.zip"}],
                }
            ],
            message_scene={"ext": ["msg_idx=REFIDX_self", "ref_msg_idx=REFIDX_quoted"]},
        ),
        self_id="BOT",
    )
    assert quoted is not None and quoted.reply_text == "原来的话"
    assert [(item.kind, item.source) for item in quoted.reply_attachments] == [
        (AttachmentKind.FILE, "reply")
    ]
