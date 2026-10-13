"""Explicit outer-event reads use the original account and preserve raw results."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.attachment_inputs import AttachmentInputService, NativeInput
from qq_ai_bot.social.agent_adapter import invoke_social
from qq_ai_bot.social.history import forward_messages
from qq_ai_bot.social.models import SocialError


async def setup_forward(database, tmp_path, segments):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        event = await reader.scalar(select(ChatEventModel))
        event_id = event.id
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event_id)
            .values(segments_json=json.dumps(segments))
        )
    env.bot.calls.clear()
    message = InboundMessage(
        message_id="inbound",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="请读取这份转发",
        bot_user_id="80001",
        group_id="20001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        presence_id=env.presence,
        space_id=env.space,
        source_event_id=event_id,
    )
    snapshot = ConversationTurnSnapshot(env.context.conversation_id, "forward-test", 1, event_id, 1)
    runtime = ToolRuntime(message, None, False, turn_snapshot=snapshot)
    return env, event_id, runtime


async def invoke(env, runtime, args):
    token = current_invocation.set(ToolInvocationContext(runtime, "forward-test"))
    try:
        return await invoke_social(env.service, "read_conversation_history", args, runtime)
    finally:
        current_invocation.reset(token)


@pytest.mark.parametrize(
    "envelope",
    [
        lambda x: x,
        lambda x: {"messages": x},
        lambda x: {"message_list": x},
        lambda x: {"data": x},
        lambda x: {"data": {"list": x}},
        lambda x: {"data": {"messages": x}},
        lambda x: {"data": {"message_list": x}},
    ],
)
async def test_real_gateway_preserves_all_raw_nodes_and_never_expands_inner(
    database, tmp_path, envelope
):
    env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    raw = [
        None,
        "raw value",
        17,
        {
            "unknown": {"media": "untouched"},
            "message": [{"type": "forward", "data": {"id": "inner"}}],
        },
    ] + [{"text": "long" * 6000} for _ in range(55)]

    async def api(action, **params):
        env.bot.calls.append((action, params))
        assert action == "get_forward_msg" and params == {"id": "outer"}
        return envelope(raw)

    env.bot.call_api = api
    before = None
    async with database.sessions() as reader:
        before = await reader.scalar(select(func.count(ChatEventModel.id)))
    result = await invoke(
        env,
        runtime,
        {"event_id": event_id, "limit": 1, "presence_id": env.presence, "target_id": env.person},
    )
    assert result["complete"] and result["forwards"][0]["messages"] == raw
    assert result["presence_id"] == env.presence and result["content_trust"] == "untrusted_data"
    assert env.bot.calls == [("get_forward_msg", {"id": "outer"})]
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count(ChatEventModel.id))) == before


async def test_same_locator_reuse_partial_empty_success_and_fresh_invocation(database, tmp_path):
    segments = [
        {"type": "forward", "data": {"id": "same"}},
        {"type": "text", "data": {"text": "separator"}},
        {"type": "forward", "data": {"id": "bad"}},
        {"type": "forward", "data": {"id": "same"}},
        {"type": "forward", "data": {}},
    ]
    env, event_id, runtime = await setup_forward(database, tmp_path, segments)

    async def api(action, **params):
        env.bot.calls.append((action, params))
        return (
            {"status": "failed", "messages": []}
            if params["id"] == "bad"
            else {"messages": "invalid ancillary", "data": {"list": []}}
        )

    env.bot.call_api = api
    for _ in range(2):
        result = await invoke(env, runtime, {"event_id": event_id})
        assert not result["complete"] and "error" not in result
        assert [x["outer_segment_index"] for x in result["forwards"]] == [0, 2, 3, 4]
        assert result["forwards"][0]["messages"] == [] and result["forwards"][2]["messages"] == []
    assert len(env.bot.calls) == 4 and all(p["id"] != "inner" for _, p in env.bot.calls)


@pytest.mark.parametrize("value", [True, False, 0, -1, None, "1"])
async def test_invalid_internal_anchor_has_no_gateway_effect(database, tmp_path, value):
    env, _event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    with pytest.raises(SocialError, match="invalid_history_event_id"):
        await invoke(env, runtime, {"event_id": value})
    assert not env.bot.calls


@pytest.mark.parametrize("change", ["derived", "author_disabled", "generation", "privacy"])
async def test_publication_fences_only_real_source_withdrawal(database, tmp_path, change):
    env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )

    async def api(action, **params):
        async with database.immediate_session() as writer:
            if change == "derived":
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event_id)
                    .values(
                        audio_transcript="later derived",
                        visual_summary="later visual",
                        content="updated ancillary text",
                    )
                )
            elif change == "author_disabled":
                await writer.execute(
                    update(CanonicalPersonModel)
                    .where(CanonicalPersonModel.id == env.person)
                    .values(enabled=False)
                )
            elif change == "generation":
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == env.context.conversation_id)
                    .values(generation=2)
                )
            else:
                row = await writer.get(ExecutionTraceStateModel, 1)
                if row:
                    row.privacy_generation += 1
                else:
                    writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return ["legally fetched"]

    env.bot.call_api = api
    if change in {"generation", "privacy"}:
        with pytest.raises(SocialError, match="history_source_changed"):
            await invoke(env, runtime, {"event_id": event_id})
    else:
        assert (await invoke(env, runtime, {"event_id": event_id}))["forwards"][0]["messages"] == [
            "legally fetched"
        ]


async def test_delegated_same_owner_reads_event_before_target_rewriting(database, tmp_path):
    env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    env.bot.call_api = AsyncMock(return_value=["delegated"])
    allowed = replace(runtime, read_target_id=env.space, read_scope=runtime.inbound.scope())
    assert (
        await invoke(
            env, allowed, {"event_id": event_id, "kind": "person", "target_id": env.person}
        )
    )["complete"]
    denied = replace(allowed, read_target_id=env.person)
    with pytest.raises(SocialError, match="history_scope_denied"):
        await invoke(env, denied, {"event_id": event_id})
    assert env.bot.call_api.await_count == 1


async def test_outbound_outer_event_uses_author_presence(database, tmp_path):
    env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event_id)
            .values(
                direction="outbound",
                author_presence_id=env.presence,
                ingress_presence_id=None,
                author_kind="yuki",
                author_person_id=None,
            )
        )
    env.bot.call_api = AsyncMock(return_value=[])
    assert (await invoke(env, runtime, {"event_id": event_id}))["complete"]
    env.bot.call_api.assert_awaited_once_with("get_forward_msg", id="outer")


async def test_pure_forward_skips_queue_rate_and_media(database, tmp_path):
    _env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    service = AttachmentInputService.__new__(AttachmentInputService)
    service._pending = 100
    service._pending_limit = 0
    service._limiter = SimpleNamespace(
        allow=AsyncMock(side_effect=AssertionError("no media admission"))
    )
    message = replace(
        runtime.inbound,
        attachments=(MessageAttachment(AttachmentKind.FORWARD, "forward", file="outer"),),
    )
    assert await service.prepare(message, None, None) == NativeInput()
    service._limiter.allow.assert_not_called()
    event = await EventLedgerRepository(database).get_event(event_id)
    for current in [None, event_id]:
        text = ChatEventPromptRenderer.event_content(event, current, "current text")
        assert text.count("[含合併轉發]") == 1


def test_envelope_uses_valid_list_after_invalid_metadata_and_rejects_failure():
    assert forward_messages({"messages": None, "data": {"list": [None]}}) == [None]
    with pytest.raises(SocialError, match="history_provider_failed"):
        forward_messages({"retcode": 1, "messages": []})


async def test_same_batch_noncacheable_history_uses_two_real_calls(database, tmp_path):
    from tests.support.social_history_cases import history_agent_loop

    env = await social_env(database, tmp_path)
    await history_agent_loop(env, same_batch=True)
