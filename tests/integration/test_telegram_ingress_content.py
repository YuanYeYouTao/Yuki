"""Native service updates are acknowledged without becoming human chat inputs."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import AttachmentKind
from qq_ai_bot.gateway.providers.telegram_ingress import normalize_message
from qq_ai_bot.identity.canonical_repository import create_space_binding, find_space_binding
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_schema_v1 import inputs, work


def _service_update(field, value, *, identifier=1, chat=-10042):
    result = update(identifier, chat=chat)
    del result["message"]["text"]
    result["message"][field] = value
    return result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("new_chat_members", [{"id": 43, "first_name": "New member", "is_bot": False}]),
        ("left_chat_member", {"id": 43, "first_name": "Former member", "is_bot": False}),
        ("new_chat_title", "Renamed group"),
        ("new_chat_photo", [{"file_id": "group-photo", "width": 10, "height": 10}]),
        ("delete_chat_photo", True),
        ("supergroup_chat_created", True),
        ("pinned_message", update(2)["message"]),
        ("forum_topic_created", {"name": "Topic", "icon_color": 7322096}),
        ("message_auto_delete_timer_changed", {"message_auto_delete_time": 86400}),
    ],
)
async def test_service_update_never_enters_processor_or_creates_chat_state(
    telegram_env, database, monkeypatch, field, value
):
    env = telegram_env
    handle = AsyncMock(wraps=env.harness.processor.handle)
    monkeypatch.setattr(env.harness.processor, "handle", handle)

    await env.gateway.receive(_service_update(field, value))
    await drain(env)

    handle.assert_not_awaited()
    assert not env.model.requests
    assert [method for method, _params in env.wire] == ["getMe"]
    async with database.sessions() as session:
        for table in (ChatEventModel, CanonicalConversationModel, work, inputs):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize(
    ("field", "media", "kind"),
    [
        ("photo", [{"file_id": "photo"}], AttachmentKind.IMAGE),
        ("sticker", {"file_id": "sticker", "is_animated": False}, AttachmentKind.IMAGE),
        ("animation", {"file_id": "animation"}, AttachmentKind.VIDEO),
        ("video", {"file_id": "video"}, AttachmentKind.VIDEO),
        ("voice", {"file_id": "voice"}, AttachmentKind.AUDIO),
        ("audio", {"file_id": "audio"}, AttachmentKind.AUDIO),
        ("document", {"file_id": "document", "file_name": "document.txt"}, AttachmentKind.FILE),
    ],
)
async def test_media_without_text_or_caption_still_normalizes(telegram_env, field, media, kind):
    raw = _service_update(field, media)["message"]

    normalized = normalize_message(raw, telegram_env.gateway.client)

    assert normalized is not None and normalized.text == ""
    assert len(normalized.attachments) == 1
    assert normalized.attachments[0].kind is kind
    assert normalized.segments[0]["type"] == kind.value


async def test_reply_relation_without_current_content_is_not_a_chat_input(telegram_env):
    incoming = update(text="")
    incoming["message"]["reply_to_message"] = update(2, text="Original message")["message"]

    normalized = normalize_message(incoming["message"], telegram_env.gateway.client)

    assert normalized is None


async def test_service_update_with_reply_relation_does_not_replay_old_chat(
    telegram_env, database, monkeypatch
):
    env = telegram_env
    handle = AsyncMock(wraps=env.harness.processor.handle)
    monkeypatch.setattr(env.harness.processor, "handle", handle)
    incoming = _service_update("pinned_message", update(2)["message"])
    incoming["message"]["reply_to_message"] = update(2)["message"]

    await env.gateway.receive(incoming)

    handle.assert_not_awaited()
    assert not env.model.requests
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0


async def test_current_text_with_reply_keeps_original_reference_and_body(telegram_env):
    incoming = update(text="Continue this discussion")
    incoming["message"]["reply_to_message"] = update(2, text="Original message")["message"]

    normalized = normalize_message(incoming["message"], telegram_env.gateway.client)

    assert normalized is not None and normalized.text == "Continue this discussion"
    assert normalized.reply_to_message_id == "42:2"
    assert normalized.reply_text == "Original message"


async def test_bare_native_mention_still_reaches_main_and_original_social_receipt(
    telegram_env, database
):
    env = telegram_env
    incoming = update(chat=-10042, text="@yuki_test")
    incoming["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 10}]

    await env.gateway.receive(incoming)
    await drain(env)

    assert env.model.requests
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "-10042"
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    assert len(rows) == 2
    assert rows[0].content == "@yuki_test" and rows[0].author_person_id
    assert rows[1].direction == "outbound" and rows[1].author_presence_id


async def test_poll_confirms_ignored_service_update(telegram_env, monkeypatch):
    env = telegram_env
    confirmed = asyncio.Event()
    calls = []

    async def call(method, params):
        assert method == "getUpdates"
        calls.append(dict(params))
        if len(calls) == 1:
            return [_service_update("new_chat_title", "Renamed", identifier=7)]
        confirmed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(env.gateway.client, "call", call)
    poll = asyncio.create_task(env.gateway._run())
    try:
        await asyncio.wait_for(confirmed.wait(), 5)
        assert "offset" not in calls[0]
        assert calls[1]["offset"] == 8
        assert not env.model.requests and not env.gateway._turns
    finally:
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)


@pytest.mark.parametrize("field", ["migrate_to_chat_id", "migrate_from_chat_id"])
async def test_group_migration_keeps_original_binding_without_chat_activation(
    telegram_env, database, field
):
    env = telegram_env
    async with database.immediate_session() as session:
        original = await create_space_binding(
            session,
            external_space_id="-42",
            name="Original group",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=datetime.now(UTC),
            platform="telegram",
        )
        original_id, original_space = original.id, original.space_id
    incoming = _service_update(
        field,
        -10042 if field == "migrate_to_chat_id" else -42,
        chat=-42 if field == "migrate_to_chat_id" else -10042,
    )

    await env.gateway.receive(incoming)

    async with database.sessions() as session:
        assert await find_space_binding(session, "-42", platform="telegram") is None
        current = await find_space_binding(session, "-10042", platform="telegram")
        assert current is not None
        assert (current.id, current.space_id) == (original_id, original_space)
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0
    assert not env.model.requests and not env.gateway._turns
