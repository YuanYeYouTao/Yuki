"""Exercise account ownership, read reachability, and delegated group effects."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.conversation.canonical_db_models import SpaceActiveRouteModel
from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from yuki.identity.db_models import (
    AccountModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.routing import PresenceRouter, RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.social.models import SocialError
from yuki.social.service import SocialContext, SocialService
from yuki.workspace.store import WorkspaceStore


class Bot:
    def __init__(self, account):
        self.self_id = account
        self.calls = []
        self.missing = set()
        self.fail_list = False
        self.history = {"messages": []}

    async def call_api(self, action, **params):
        self.calls.append((action, params))
        if action in {"get_friend_msg_history", "get_group_msg_history"}:
            return self.history
        if action == "get_group_member_info":
            if str(params["user_id"]) in self.missing:
                raise RuntimeError("not a member")
            return {"user_id": params["user_id"]}
        if action == "get_group_member_list":
            if self.fail_list:
                raise RuntimeError("unavailable")
            return [{"user_id": 10001, "nickname": "member"}]
        return {"message_id": len(self.calls) + 1000}


async def social_env(database, tmp_path):
    bot = Bot("80001")
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(
            qq_transfer_root=tmp_path / "transfer", qq_gateway_root="/transfer"
        )
    )
    router = PresenceRouter(database, registry)
    writer = ScopedEventLedgerUnitOfWork(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "10001")
        # This shared fixture exercises the original, pre-0121 Social contracts.
        from sqlalchemy import update

        await session.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.person_id == person)
            .values(history_starts_at=None)
        )
        presence = await ensure_presence(session, bot.self_id)
        space = await ensure_space(session, "20001")
    from datetime import UTC, datetime

    from yuki.identity.canonical_repository import find_identity_binding
    from yuki.identity.person_state import observe_canonical_person

    async with database.sessions() as session, session.begin():
        binding = await find_identity_binding(session, "10001")
        await observe_canonical_person(
            session,
            binding=binding,
            nickname="test-10001",
            group_id="20001",
            group_card="",
            group_name="",
            nickname_known=True,
            group_card_known=True,
            now=datetime.now(UTC),
        )
    registry.connect(bot, provider_id="snowluma", presence_id=presence)
    await writer.append(
        scope=ConversationScope.group(bot.self_id, "20001"),
        platform_message_id="inbound",
        sender_user_id="10001",
        direction="inbound",
        content="hello",
    )
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
        conversation = event.canonical_conversation_id
        account = await session.scalar(
            select(AccountModel).where(
                AccountModel.platform == "qq", AccountModel.external_account_id == "10001"
            )
        )
    assert await router.cas_takeover_space(space) == "taken"
    service = SocialService(database, router, writer)
    store = WorkspaceStore(tmp_path / "workspace")
    service.workspace = store
    return SimpleNamespace(
        db=database,
        bot=bot,
        registry=registry,
        router=router,
        service=service,
        person=person,
        account=account.id,
        presence=presence,
        space=space,
        store=store,
        context=SocialContext("turn", "call", conversation, space_id=space),
    )


async def test_recall_uses_original_presence(social_env, route_state):
    env = social_env
    sent = await env.service.execute("send_message", {"text": "hello"}, env.context)
    other = Bot("80002")
    async with env.db.sessions() as session, session.begin():
        second = await ensure_presence(session, other.self_id)
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.id == sent["event_id"])
        )
        event_id = event.id
        route = await session.get(SpaceActiveRouteModel, env.space)
        if route_state == "deleted":
            await session.delete(route)
        elif route_state == "paused":
            route.paused = True
        else:
            route.presence_id = second
    env.registry.connect(other, provider_id="snowluma", presence_id=second)
    result = await env.service.execute(
        "recall_own_message", {"event_id": event_id}, replace(env.context, call_id="recall")
    )
    assert result["status"] == "succeeded"
    assert env.bot.calls[-1] == ("delete_msg", {"message_id": event.platform_message_id})
    assert other.calls == []
    env.registry.disconnect(env.bot)
    with pytest.raises(RouteSendError, match="original_presence_unavailable"):
        await env.service.execute(
            "recall_own_message", {"event_id": event_id}, replace(env.context, call_id="offline")
        )
    assert other.calls == []


async def test_members_ignore_send_pause_and_try_accessible_connections(social_env):
    env = social_env
    async with env.db.sessions() as session, session.begin():
        route = await session.get(SpaceActiveRouteModel, env.space)
        route.paused = True
        generation = route.route_generation
    env.bot.calls.clear()
    result = await env.service.execute("get_group_members", {}, env.context)
    assert result["coverage"] == "observed" and result["items"][0]["account_id"] == env.account
    assert result["items"][0]["reference"] == f"@{{account:{env.account}}}"
    assert env.bot.calls == []
    async with env.db.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, env.space)
        assert route.paused and route.route_generation == generation


async def test_multiple_space_bindings_require_explicit_selection(social_env):
    env = social_env
    async with env.db.sessions() as session, session.begin():
        other = await ensure_space(session, "20002")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == other)
        )
        binding.space_id = env.space
    observed = await env.service.execute("get_group_members", {}, env.context)
    assert observed["coverage"] == "observed"
    assert [item["person_id"] for item in observed["items"]] == [env.person]


async def test_recall_rechecks_connection_before_claim(social_env, monkeypatch):
    env = social_env
    sent = await env.service.execute("send_message", {"text": "hello"}, env.context)
    async with env.db.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.id == sent["event_id"])
        )
        event_id = event.id
    prepare = env.service.receipts.prepare

    async def disconnect_before_claim(**kwargs):
        receipt = await prepare(**kwargs)
        env.registry.disconnect(env.bot)
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", disconnect_before_claim)
    with pytest.raises(RouteSendError, match="disconnected"):
        await env.service.execute(
            "recall_own_message", {"event_id": event_id}, replace(env.context, call_id="recall")
        )
    assert not any(action == "delete_msg" for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find("turn", "recall")
    assert receipt.status == "prepared"


async def test_disabled_presence_cannot_read_or_recall(social_env):
    env = social_env
    async with env.db.sessions() as session, session.begin():
        presence = await session.get(PresenceModel, env.presence)
        presence.enabled = False
    with pytest.raises(RouteSendError, match="original_presence_unavailable"):
        await env.router.resolve_presence(env.presence)
    observed = await env.service.execute("get_group_members", {}, env.context)
    assert observed["coverage"] == "observed"


async def add_second_account(env):
    async with env.db.sessions() as session, session.begin():
        other = await ensure_person(session, "10002")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == other)
        )
        from yuki.identity.person_identity import merge_persons

        await merge_persons(session, other, env.person)
        return binding.id


async def test_private_send_uses_explicit_binding_and_rejects_foreign_binding(social_env):
    env = social_env
    assert await env.router.cas_takeover_person(env.person) == "taken"
    second = await add_second_account(env)
    contacts = await env.service.execute(
        "find_contacts",
        {
            "kind": "person",
            "target_id": env.person,
        },
        env.context,
    )
    assert len(contacts["bindings"]) == 2
    assert all(
        binding["platform"] == "qq" and "user_id" not in binding for binding in contacts["bindings"]
    )
    with pytest.raises(RouteSendError, match="binding_unavailable"):
        await env.service.execute(
            "send_message",
            {
                "text": "private report",
                "target": {"kind": "person", "target_id": env.person, "binding_id": str(uuid4())},
            },
            env.context,
        )
    result = await env.service.execute(
        "send_message",
        {
            "text": "private report",
            "target": {"kind": "person", "target_id": env.person, "binding_id": second},
        },
        env.context,
    )
    assert result["status"] == "succeeded"
    assert env.bot.calls[-1] == (
        "send_private_msg",
        {"user_id": 10002, "message": [{"type": "text", "data": {"text": "private report"}}]},
    )


async def test_group_mention_resolves_binding_without_first_item(social_env, selector):
    env = social_env
    other = await add_second_account(env)
    async with env.db.sessions() as session:
        second = await session.get(IdentityBindingModel, other)
        account_id = second.account_id
    if selector == "subject":
        with pytest.raises(SocialError, match="invalid_message_arguments"):
            await env.service.execute(
                "send_message",
                {"text": "hello", "mentions": [{"subject_ref": "current_speaker"}]},
                env.context,
            )
        return
    requested = str(uuid4()) if selector == "foreign" else account_id
    result = await env.service.execute(
        "send_message", {"text": f"@{{account:{requested}}} hello"}, env.context
    )
    assert result["status"] == "succeeded"
    native = [part for part in env.bot.calls[-1][1]["message"] if part["type"] == "at"]
    if selector == "foreign":
        assert not native and result["unresolved_mentions"]
    else:
        assert native == [{"type": "at", "data": {"qq": "10002"}}]


async def test_real_mentions_preserve_segments_and_replay(social_env, attachment):
    env = social_env
    args = {"text": f"前@{{account:{env.account}}}后"}
    if attachment:
        artifact = snapshot_bytes(env.store, "hello.txt", b"hello")
        args.update(artifact_id=artifact["artifact_id"], attachment_kind=attachment)
    result = await env.service.execute("send_message", args, env.context)
    assert result["status"] == "succeeded"
    messages = [params["message"] for action, params in env.bot.calls if action == "send_group_msg"]
    assert len(messages) == 1
    assert messages[0][:3] == [
        {"type": "text", "data": {"text": "前"}},
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": "后"}},
    ]
    async with env.db.sessions() as session:
        events = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        )
        assert any(
            env.account in event.segments_json and '"mention"' in event.segments_json
            for event in events
        )
    effects = list(env.bot.calls)
    assert await env.service.execute("send_message", args, env.context) == result
    assert env.bot.calls == effects


async def current_speaker_mention(env):
    from tests.support.commands_and_chat_helpers import inbound

    from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
    from yuki.services.agent_tools import ToolRuntime
    from yuki.social.agent_adapter import invoke_social

    async with env.db.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    message = replace(
        inbound("test", message_id="inbound", user_id="10001", group_id="20001"),
        bot_user_id="80001",
        source_event_id=event_id,
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    runtime = ToolRuntime(inbound=message)
    token = current_invocation.set(ToolInvocationContext(runtime=runtime, call_id="mention-me"))
    try:
        result = await invoke_social(
            env.service,
            "send_message",
            {
                "text": "@test-10001 test",
            },
            runtime,
        )
    finally:
        current_invocation.reset(token)
    assert result["status"] == "succeeded"
    assert env.bot.calls[-1][1]["message"][0] == {"type": "at", "data": {"qq": "10001"}}


async def test_mentions_reject_ambiguous_or_nonmember_accounts(social_env):
    env = social_env
    binding_id = await add_second_account(env)
    async with env.db.sessions() as session, session.begin():
        binding = await session.get(IdentityBindingModel, binding_id)
        account = binding.account_id
        from yuki.identity.db_models import CanonicalPersonModel

        person = await session.get(CanonicalPersonModel, binding.person_id)
        person.enabled = False
    env.bot.missing.add("10002")
    env.bot.calls.clear()
    result = await env.service.execute(
        "send_message", {"text": f"@{{account:{account}}}"}, env.context
    )
    assert result["status"] == "succeeded"
    assert env.bot.calls == [
        (
            "send_group_msg",
            {"group_id": 20001, "message": [{"type": "at", "data": {"qq": "10002"}}]},
        )
    ]
