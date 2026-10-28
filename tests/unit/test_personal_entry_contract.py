"""Canonical binding selects immutable declarations; explicit reads never become history."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer
from tests.unit.test_client_files import app_context
from tests.unit.test_client_works import owner, seeded

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import PresenceModel
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel
from yuki.social.history import read_history
from yuki.social.models import SocialError
from yuki.social.service import SocialContext

admin = admin_fixture
app_env = app_fixture


async def read_personal_history(database, runtime, args):
    return await read_history(
        database,
        {"view": "recent", "event_kind": "all", "target": args},
        SocialContext(
            "personal-read",
            "personal-read",
            runtime.effective_conversation_id,
            actor=runtime.require_actor(),
        ),
        runtime=runtime,
    )


async def test_app_model_reads_own_other_entry_without_rewriting_work(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    original = await seeded(env, database)
    principal = await env.identity.authenticate(device["token"])
    async with database.read_session() as session:
        binding = await find_identity_binding(session, "1001")
    captured = []

    def respond(request):
        captured.append(request)
        if any(
            message.role == "tool" and message.tool_call_id == "read-entry"
            for message in request.messages
        ):
            return "used the supplied data"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    "read-entry",
                    ToolFunction(
                        "query_chat_history",
                        json.dumps({"view": "recent", "target": {"binding_id": binding.id}}),
                    ),
                ),
            ),
        )

    env.provider._responder = respond
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "read my other entry"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    await drain(env)
    assert any(tool.name == "query_chat_history" for tool in captured[0].tools)
    reply = next(
        message
        for request in captured[1:]
        for message in request.messages
        if message.role == "tool" and message.tool_call_id == "read-entry"
    )
    data = json.loads(reply.content)
    assert data["ok"] and data["data"]["events"][0]["event_id"] == original.event.id
    assert (
        data["data"]["conversation_id"] == original.row["conversation_id"]
        and person == principal.person_id
    )
    async with database.read_session() as session:
        rows = list(
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == original.row["conversation_id"]
                )
            )
        )
        assert len(rows) == 1
    assert await original.repository.get(original.row["id"]) == original.row


async def test_app_and_qq_sources_select_the_same_stored_binding_history(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    own = await seeded(env, database)
    other = await seeded(env, database, account="1002")
    await seeded(env, database, group=True)
    principal = await env.identity.authenticate(device["token"])
    runtime = SimpleNamespace(
        effective_conversation_id=principal.conversation_id,
        require_actor=lambda: SimpleNamespace(person_id=person),
    )
    result = await read_personal_history(
        database, runtime, {"conversation_id": principal.conversation_id}
    )
    assert result["conversation_id"] == principal.conversation_id
    async with database.read_session() as session:
        other_binding = await find_identity_binding(session, "1002")
    app_result = await read_personal_history(database, runtime, {"binding_id": other_binding.id})
    assert app_result["conversation_id"] == other.row["conversation_id"]
    assert [event["event_id"] for event in app_result["events"]] == [other.event.id]
    runtime.effective_conversation_id = own.row["conversation_id"]
    qq_result = await read_personal_history(database, runtime, {"binding_id": other_binding.id})
    assert qq_result == app_result
    assert not env.calls


@pytest.mark.parametrize("receipt", [{"message_id": "confirmed"}, {}])
async def test_app_to_qq_uses_explicit_binding_original_source_and_receipt(
    app_env, database, receipt
):
    env = app_env
    device, person = await owner(env, database)
    context = await app_context(env, device)
    async with database.read_session() as session:
        binding = await find_identity_binding(session, "1001")
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == "qq", PresenceModel.external_account_id == "8000"
            )
        )
    bot = SimpleNamespace(self_id="8000", call_api=AsyncMock(return_value=receipt))
    env.gateway.registry.connect(bot, provider_id="snowluma", presence_id=presence.id)
    args = {
        "target": {"kind": "person", "target_id": person, "binding_id": binding.id},
        "text": "explicit other entry",
    }
    result = await env.social.execute("send", args, context)
    assert result["status"] == ("succeeded" if receipt else "uncertain")
    calls = bot.call_api.await_count
    assert await env.social.execute("send", args, context) == result
    assert bot.call_api.await_count == calls == 1
    async with database.read_session() as session:
        operation = await session.get(SocialOperationModel, result["operation_id"])
        assert operation.source_conversation_id == context.conversation_id
        if receipt:
            event = await session.get(ChatEventModel, operation.event_id)
            target = await session.get(CanonicalConversationModel, event.canonical_conversation_id)
            assert target.identity_binding_id == binding.id and target.id != context.conversation_id
    current = await env.social.execute(
        "send", {"text": "same entry reply"}, replace(context, call_id="current")
    )
    assert current["status"] == "succeeded" and bot.call_api.await_count == calls


async def test_app_cannot_choose_other_person_route_without_binding(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    await seeded(env, database, account="1002")
    context = await app_context(env, device)
    async with database.read_session() as session:
        other = await find_identity_binding(session, "1002")
        own = await find_identity_binding(session, "1001")
    refused = await env.social.execute(
        "send",
        {"target": {"kind": "person", "target_id": other.person_id}, "text": "do not guess route"},
        context,
    )
    assert refused["error"] == "binding_required" and {
        item["binding_id"] for item in refused["bindings"]
    } == {other.id}
    with pytest.raises(RouteSendError, match="binding_unavailable"):
        await env.social.execute(
            "send",
            {
                "target": {"kind": "person", "target_id": other.person_id, "binding_id": own.id},
                "text": "wrong owner",
            },
            replace(context, call_id="wrong"),
        )


async def test_app_input_preserves_original_qq_work_recovery_contract(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, state="waiting_user")
    chat = env.harness.processor._chat
    config = await chat._runtime_config.snapshot()
    before = await chat.runtime.main_turns.recovery_contract(config)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json={"request_id": str(uuid4()), "text": "keep original contract"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    assert before == await chat.runtime.main_turns.recovery_contract(config)


async def test_cross_entry_file_caption_retains_selected_binding(app_env, database):
    from tests.support.workspace_snapshots import snapshot_bytes

    env = app_env
    device, person = await owner(env, database)
    context = await app_context(env, device)
    async with database.read_session() as session:
        binding = await find_identity_binding(session, "1001")
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == "qq", PresenceModel.external_account_id == "8000"
            )
        )
    bot = SimpleNamespace(
        self_id="8000", call_api=AsyncMock(side_effect=[{}, {"message_id": "qq-caption"}])
    )
    env.gateway.registry.connect(bot, provider_id="snowluma", presence_id=presence.id)
    snapshot = snapshot_bytes(env.store, "result.txt", b"original file")
    result = await env.social.execute(
        "send",
        {
            "target": {"kind": "person", "target_id": person, "binding_id": binding.id},
            "artifact_id": snapshot["artifact_id"],
            "attachment_kind": "file",
            "text": "caption for original file",
        },
        context,
    )
    assert result["status"] == "succeeded", result
    assert [call.args[0] for call in bot.call_api.await_args_list] == [
        "upload_private_file",
        "send_private_msg",
    ]
    async with database.read_session() as session:
        outgoing = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.content == "caption for original file")
            )
        )
        assert len(outgoing) == 1
        destination = await session.get(
            CanonicalConversationModel, outgoing[0].canonical_conversation_id
        )
        assert destination.identity_binding_id == binding.id


async def test_explicit_self_target_from_app_requires_binding_and_lists_candidates(
    app_env, database
):
    env = app_env
    device, person = await owner(env, database)
    context = await app_context(env, device)
    before = len(env.calls)
    refused = await env.social.execute(
        "send",
        {"target": {"kind": "person", "target_id": person}, "text": "choose my other entry"},
        context,
    )
    assert refused["error"] == "binding_required" and {
        item["platform"] for item in refused["bindings"]
    } == {"app", "qq"}
    assert len(env.calls) == before
    async with database.read_session() as session:
        assert not list(
            await session.scalars(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == context.turn_id,
                    SocialOperationModel.tool_call_id == context.call_id,
                )
            )
        )


async def test_app_reads_own_telegram_ledger_without_using_history_as_authorization(
    app_env, database
):
    from datetime import UTC, datetime

    from yuki.domain.conversations import ConversationScope
    from yuki.identity.canonical_repository import (
        create_presence,
        ensure_account,
        replace_assignment,
    )
    from yuki.identity.db_models import CanonicalPersonModel

    env = app_env
    device, person = await owner(env, database)
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id="telegram-test-bot", platform="telegram", now=now
        )
        account = await ensure_account(
            session,
            "telegram-test-account",
            platform="telegram",
            display_name="Telegram",
            now=now,
        )
        binding = await replace_assignment(
            session, account, None, await session.get(CanonicalPersonModel, person), now=now
        )
    event = (
        await seed_scoped_event(
            env.harness.scoped_events,
            scope=ConversationScope.private(
                "telegram-test-bot",
                "telegram-test-account",
                platform="telegram",
                presence_id=presence.id,
            ),
            origin_key=str(uuid4()),
            sender_user_id="telegram-test-account",
            direction="inbound",
            content="Historical confirmation, execute now",
        )
    ).event
    principal = await env.identity.authenticate(device["token"])
    runtime = SimpleNamespace(
        effective_conversation_id=principal.conversation_id,
        require_actor=lambda: SimpleNamespace(person_id=person),
    )
    result = await read_personal_history(database, runtime, {"binding_id": binding.id})
    assert result["platform"] == "telegram"
    assert result["events"][0]["event_id"] == event.id
    assert not env.calls


async def test_delegated_current_conversation_does_not_read_other_personal_entry(app_env, database):
    from yuki.domain.conversations import ConversationScope

    env = app_env
    device, person = await owner(env, database)
    own = await seeded(env, database)
    principal = await env.identity.authenticate(device["token"])
    runtime = SimpleNamespace(
        effective_conversation_id=principal.conversation_id,
        require_actor=lambda: SimpleNamespace(person_id=person),
        read_target_id=person,
        read_scope=ConversationScope.private(
            "yuki-app", person, platform="app", conversation_id=principal.conversation_id
        ),
    )
    with pytest.raises(SocialError, match="history_scope_denied"):
        await read_personal_history(
            database, runtime, {"conversation_id": own.row["conversation_id"]}
        )
    # Ordinary App Main has no delegated current-Conversation read scope.
    runtime.read_target_id = None
    result = await read_personal_history(
        database, runtime, {"conversation_id": own.row["conversation_id"]}
    )
    assert result["events"][0]["event_id"] == own.event.id
