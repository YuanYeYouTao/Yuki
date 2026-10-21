"""Ordinary own-state feedback precedes actual delivery without revoking its turn."""

import json
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.semantic_participation_host_helpers import _host
from yuki_participation.controller import State
from yuki_participation.participation import ParticipationCheckpoint

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import create_space_binding
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel


async def test_quiet_is_saved_before_split_delivery_and_keeps_original_request_and_receipts(
    telegram_env, database, tmp_path, monkeypatch
):
    env = telegram_env
    host, _ = await _host(database, tmp_path)
    processor = env.harness.processor
    chat = processor._chat
    config = RuntimeConfigService(
        database=database,
        settings=make_settings(database.url, conversation_semantic_participation_enabled=True),
    )
    processor._runtime_config = chat._runtime_config = host.app.runtime_config = config
    host.app.chat = chat
    processor.set_participation(host)
    chat.observe_main_response = host.observe_main_response
    original_call = TelegramClient.call
    seen_hints = []

    async def transport(client, method, params, files=None):
        if method == "sendMessage":
            # Inspect the real persisted snapshot before the platform returns ACK.
            # A quiet decision affects later participation, not this selected turn.
            assert len(env.model.requests) == 1
            item = next(iter(host._sessions.values()))
            _, saved = await host._store.load(item.scene.scope)
            checkpoint = ParticipationCheckpoint.model_validate(
                State.model_validate_json(saved).host_checkpoint["participation_v1"]
            )
            unit = next(iter(checkpoint.units.values()))
            assert unit.hint.delta.engage == "quiet"
            assert unit.hint.response_id == "original-main-response"
            seen_hints.append(unit.hint)
        return await original_call(client, method, params, files)

    monkeypatch.setattr(TelegramClient, "call", transport)

    def respond(request):
        if request.messages[-1].role == "tool":
            return "done"
        return ChatResponse(
            '<yuki-state>{"engage":"quiet"}</yuki-state>',
            0,
            provider_request_id="original-main-response",
            tool_calls=(
                ToolCall(
                    "original-send",
                    ToolFunction("send_message", json.dumps({"text": "first\nsecond"})),
                ),
            ),
        )

    env.model._responder = respond
    try:
        async with database.immediate_session() as session:
            await create_space_binding(
                session,
                external_space_id="-10042",
                name="ordinary explicit input",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                now=datetime.now(UTC),
                platform="telegram",
            )
        incoming = update(chat=-10042, topic=10, text="!ai original explicit input")
        incoming["message"]["date"] = int(datetime.now(UTC).timestamp())
        await env.gateway.receive(incoming)
        await drain(env)

        sends = [params for method, params in env.wire if method == "sendMessage"]
        assert [params["text"] for params in sends] == ["first", "second"]
        assert all(params["message_thread_id"] == 10 for params in sends)
        assert len(seen_hints) == 2 and seen_hints[0] == seen_hints[1]
        assert len(env.model.requests) == 2
        first, continuation = env.model.requests
        original_messages = deepcopy(first.messages)
        assert continuation.tools == first.tools
        assert continuation.messages[: len(first.messages)] == first.messages

        async with database.sessions() as reader:
            rows = list(await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
            operations = list(
                await reader.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.action == "send_message"
                    )
                )
            )
        source, *outbound = rows
        assert source.direction == "inbound" and len(outbound) == 2
        assert [row.content for row in outbound] == ["first", "second"]
        assert {row.canonical_conversation_id for row in rows} == {source.canonical_conversation_id}
        admission = await host.ordinary.get(source.id)
        assert admission.actor_person_id == source.author_person_id
        assert admission.binding.event_id == source.id
        assert seen_hints[0].run_ref == admission.activation_id
        assert seen_hints[0].source.event_id == f"event:{source.id}"
        assert {operation.event_id for operation in operations} == {row.id for row in outbound}
        assert all(operation.status == "succeeded" for operation in operations)
        assert not host._observer.calls

        # Duplicate transport input is the same admission, not a second main/send.
        await env.gateway.receive(incoming)
        await drain(env)
        assert len(env.model.requests) == 2
        assert first.messages == original_messages
        assert [params for method, params in env.wire if method == "sendMessage"] == sends
    finally:
        await host.close()
