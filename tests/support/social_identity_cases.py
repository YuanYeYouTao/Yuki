"""Exercise account ownership, read reachability, and delegated group effects."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import SpaceActiveRouteModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel, SpaceBindingModel
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.social.automation import SocialAutomationAdapter
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.social.service import SocialContext, SocialService
from qq_ai_bot.social.transfer import ArtifactTransfer
from qq_ai_bot.workspace.store import WorkspaceStore


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
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "10001")
        presence = await ensure_presence(session, bot.self_id)
        space = await ensure_space(session, "20001")
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
    assert await router.cas_takeover_space(space) == "taken"
    service = SocialService(database, router, writer)
    store = WorkspaceStore(tmp_path / "workspace")
    service.transfer = ArtifactTransfer(store, tmp_path / "transfer", "/transfer")
    return SimpleNamespace(
        db=database,
        bot=bot,
        registry=registry,
        router=router,
        service=service,
        person=person,
        presence=presence,
        space=space,
        store=store,
        context=SocialContext("turn", "call", conversation, space_id=space),
    )


async def test_recall_uses_original_presence(social_env, route_state):
    env = social_env
    sent = await env.service.execute("send_group_message", {"text": "hello"}, env.context)
    other = Bot("80002")
    async with env.db.sessions() as session, session.begin():
        second = await ensure_presence(session, other.self_id)
        event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.platform_message_id == sent["platform_reference"]
            )
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
    assert env.bot.calls[-1] == ("delete_msg", {"message_id": sent["platform_reference"]})
    assert other.calls == []
    env.registry.disconnect(env.bot)
    with pytest.raises(RouteSendError, match="original_presence_unavailable"):
        await env.service.execute(
            "recall_own_message", {"event_id": event_id}, replace(env.context, call_id="offline")
        )
    assert other.calls == []


async def test_members_ignore_send_pause_and_try_accessible_connections(social_env):
    env = social_env
    other = Bot("80002")
    env.bot.fail_list = True
    async with env.db.sessions() as session, session.begin():
        second = await ensure_presence(session, other.self_id)
        route = await session.get(SpaceActiveRouteModel, env.space)
        route.paused = True
        generation = route.route_generation
    env.registry.connect(other, provider_id="snowluma", presence_id=second)
    result = await env.service.execute("get_group_members", {}, env.context)
    assert result["items"] == [{"user_id": "10001", "display_name": "member"}]
    async with env.db.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, env.space)
        assert route.paused and route.route_generation == generation
    env.bot.missing.add(env.bot.self_id)
    other.missing.add(other.self_id)
    with pytest.raises(RouteSendError, match="group_unavailable"):
        await env.service.execute("get_group_members", {}, env.context)


async def test_multiple_space_bindings_require_explicit_selection(social_env):
    env = social_env
    async with env.db.sessions() as session, session.begin():
        other = await ensure_space(session, "20002")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == other)
        )
        binding.space_id = env.space
        binding_id = binding.id
    with pytest.raises(RouteSendError, match="binding_ambiguous"):
        await env.service.execute("get_group_members", {}, env.context)
    await env.service.execute("get_group_members", {"space_binding_id": binding_id}, env.context)
    assert env.bot.calls[-1][1]["group_id"] == 20002


async def test_recall_rechecks_connection_before_claim(social_env, monkeypatch):
    env = social_env
    sent = await env.service.execute("send_group_message", {"text": "hello"}, env.context)
    async with env.db.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.platform_message_id == sent["platform_reference"]
            )
        )
        event_id = event.id
    prepare = env.service.receipts.prepare

    async def disconnect_before_claim(**kwargs):
        receipt = await prepare(**kwargs)
        env.registry.disconnect(env.bot)
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", disconnect_before_claim)
    with pytest.raises(RouteSendError, match="original_presence_unavailable"):
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
    with pytest.raises(RouteSendError, match="group_unavailable"):
        await env.service.execute("get_group_members", {}, env.context)


async def add_second_account(env):
    async with env.db.sessions() as session, session.begin():
        other = await ensure_person(session, "10002")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == other)
        )
        binding.person_id = env.person
        return binding.id


async def test_private_poke_uses_pinned_binding_and_rejects_conflict(social_env):
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
    assert {binding["user_id"] for binding in contacts["bindings"]} == {"10001", "10002"}
    with pytest.raises(SocialError, match="binding_unavailable"):
        await env.service.execute(
            "poke_person",
            {
                "target_id": env.person,
                "scene": "private",
                "binding_id": second,
            },
            env.context,
        )
    result = await env.service.execute(
        "poke_person",
        {
            "target_id": env.person,
            "scene": "private",
        },
        env.context,
    )
    assert result["status"] == "succeeded"
    assert env.bot.calls[-1] == ("send_poke", {"user_id": 10001})


async def test_group_poke_resolves_binding_without_first_item(social_env, selector):
    env = social_env
    binding = await add_second_account(env)
    args = {"target_id": env.person}
    context = env.context
    if selector == "explicit":
        args["binding_id"] = binding
    elif selector == "subject":
        args = {"subject_ref": "mentioned_user"}
        context = replace(
            context,
            person_refs={"mentioned_user": env.person},
            account_refs={"mentioned_user": "10002"},
        )
    elif selector == "foreign":
        args["binding_id"] = str(uuid4())
    if selector in {"ambiguous", "foreign"}:
        with pytest.raises(SocialError, match=r"binding_ambiguous|binding_unavailable"):
            await env.service.execute("poke_person", args, context)
        assert not any(action == "send_poke" for action, _ in env.bot.calls)
    else:
        result = await env.service.execute("poke_person", args, context)
        assert result["status"] == "succeeded"
        assert env.bot.calls[-1] == ("send_poke", {"user_id": 10002, "group_id": 20001})


async def test_real_mentions_preserve_segments_and_replay(social_env, attachment):
    env = social_env
    args = {"mentions": [{"target_id": env.person}]}
    if attachment:
        artifact = env.store.write("hello.txt", b"hello")
        args.update(artifact_id=artifact["artifact_id"], attachment_kind=attachment)
    result = await env.service.execute("send_group_message", args, env.context)
    assert result["status"] == "succeeded"
    sends = [params for action, params in env.bot.calls if action == "send_group_msg"]
    assert len(sends) == 1
    assert sends[0]["message"][0] == {"type": "at", "data": {"qq": "10001"}}
    async with env.db.sessions() as session:
        events = (
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        ).all()
        assert any('"at"' in str(event.segments_json) for event in events)
    effects = [call for call in env.bot.calls if call[0] in {"send_group_msg", "upload_group_file"}]
    assert await env.service.execute("send_group_message", args, env.context) == result
    assert effects == [
        call for call in env.bot.calls if call[0] in {"send_group_msg", "upload_group_file"}
    ]


async def current_speaker_mention(env):
    from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.runtime.origin import TurnOrigin
    from qq_ai_bot.social.agent_adapter import invoke_social

    async with env.db.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    actor = ToolActor(
        user_id="10001",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.USER_MESSAGE,
        instruction="test",
        event_id=event_id,
    )
    runtime = SimpleNamespace(
        require_actor=lambda: actor,
        origin=TurnOrigin.USER_MESSAGE,
        read_only=False,
        tools_closed=False,
        inbound=SimpleNamespace(
            sender=SimpleNamespace(user_id="10001"),
            scope_type="group",
            message_id="inbound",
            presence_id=env.presence,
        ),
        mentioned_user_ids=(),
        effective_conversation_id=env.context.conversation_id,
        trigger_message_id="inbound",
        space_id=env.space,
    )
    token = current_invocation.set(ToolInvocationContext(runtime=runtime, call_id="mention-me"))
    try:
        result = await invoke_social(
            env.service,
            "send_group_message",
            {
                "mentions": [{"subject_ref": "current_speaker"}],
                "text": "test",
            },
            runtime,
        )
    finally:
        current_invocation.reset(token)
    assert result["status"] == "succeeded"
    assert env.bot.calls[-1][1]["message"][0] == {"type": "at", "data": {"qq": "10001"}}


async def test_mentions_reject_ambiguous_or_nonmember_accounts(social_env):
    env = social_env
    binding = await add_second_account(env)
    with pytest.raises(SocialError, match="binding_ambiguous"):
        await env.service.execute(
            "send_group_message",
            {"mentions": [{"target_id": env.person}], "text": "hello"},
            env.context,
        )
    env.bot.missing.add("10002")
    with pytest.raises(SocialError, match="group_member_unavailable"):
        await env.service.execute(
            "send_group_message",
            {"mentions": [{"target_id": env.person, "binding_id": binding}]},
            env.context,
        )
    assert not any(action == "send_group_msg" for action, _ in env.bot.calls)


async def test_group_automation_poke_keeps_delegated_boundary(social_env, override, allowed):
    env = social_env
    context = SimpleNamespace(
        authority=SimpleNamespace(
            allowed_capabilities={"social.poke_person"},
            delegated_authority=object(),
            actor_is_superuser=False,
        ),
        automation_run_id=1,
        step_id="poke",
        canonical_conversation_id=env.context.conversation_id,
        canonical_target_space_id=env.space,
        canonical_target_person_id=None,
    )
    adapter = SocialAutomationAdapter(env.service, None, None)
    invoke = adapter.mapping()["social.poke_person"]
    args = {"target_id": env.person, **override}
    if allowed:
        result = await invoke(args, context)
        assert result.data["status"] == "succeeded"
        assert env.bot.calls[-1][1]["group_id"] == 20001
    else:
        with pytest.raises(SocialError, match="delegated_target_not_allowed"):
            await invoke(args, context)
        assert not any(action == "send_poke" for action, _ in env.bot.calls)
    context.authority.delegated_authority = None
    with pytest.raises(SocialError, match="capability_denied"):
        await invoke(args, context)


async def run_identity_scenarios(tmp_path):
    """Extend the existing social safety gate within the repository's test budget."""
    from tests.support.social_history_cases import (
        history_agent_loop,
        history_receipt,
        history_targets_and_delegation,
    )

    scenarios = [
        (history_agent_loop, ()),
        (history_receipt, ()),
        (history_targets_and_delegation, ()),
        (current_speaker_mention, ()),
        *[
            (test_recall_uses_original_presence, (state,))
            for state in ("paused", "switched", "deleted")
        ],
        (test_members_ignore_send_pause_and_try_accessible_connections, ()),
        (test_multiple_space_bindings_require_explicit_selection, ()),
        (test_disabled_presence_cannot_read_or_recall, ()),
        (test_private_poke_uses_pinned_binding_and_rejects_conflict, ()),
        *[
            (test_group_poke_resolves_binding_without_first_item, (selector,))
            for selector in ("explicit", "subject", "ambiguous", "foreign")
        ],
        *[
            (test_real_mentions_preserve_segments_and_replay, (attachment,))
            for attachment in (None, "image", "file")
        ],
        (test_mentions_reject_ambiguous_or_nonmember_accounts, ()),
        *[
            (test_group_automation_poke_keeps_delegated_boundary, (override, allowed))
            for override, allowed in (
                ({}, True),
                ({"scene": "private"}, False),
                ({"space_id": str(uuid4())}, False),
                ({"scene": "current"}, True),
            )
        ],
    ]
    with pytest.MonkeyPatch.context() as monkeypatch:
        scenarios.append((test_recall_rechecks_connection_before_claim, (monkeypatch,)))
        for index, (scenario, arguments) in enumerate(scenarios):
            directory = tmp_path / f"identity-{index}"
            database = Database(f"sqlite+aiosqlite:///{directory / 'test.db'}")
            try:
                await database.create_schema()
                env = await social_env(database, directory)
                await scenario(env, *arguments)
            except Exception as exc:
                exc.add_note(f"Social identity scenario: {scenario.__name__} {arguments}")
                raise
            finally:
                await database.close()
