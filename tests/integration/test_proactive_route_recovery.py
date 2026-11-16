"""Offline route recovery through real Registry, Social, SQLite and native receipts.

Failure cases specified before implementation:
- Losing a pinned sender with multiple replacements must not persist a pause, move
  the destination, change its generation, or send through an arbitrary candidate.
- Both the original sender returning and a single remaining candidate must recover
  Person/Space proactive delivery; confirmed sends must not replay after reconnect.
- Legacy/explicit pauses survive reconciliation, even with a healthy pinned sender.
  Group person_id sends stop; the same person's QQ/TG conversations still reply.
- The owner can explicitly restore the same destination with the existing revisioned
  client command; stale revisions and foreign/ended bindings cannot restore it.
- Account transfer pauses the original owner and invalidates the original entry;
  reconciliation and private replies must not route to the new account owner.

All native traffic uses an in-memory QQ bot or httpx.MockTransport. The shared
evidence fixture records route snapshots, calls, commands and delivery receipts.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.client_identity import client_identity_service
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import Bot, social_env

from yuki.client_api.errors import ClientError
from yuki.client_api.routes import ClientProactiveRoute
from yuki.conversation.canonical_db_models import PersonActiveRouteModel, SpaceActiveRouteModel
from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import create_presence, ensure_account, replace_assignment
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.routing import RouteMonitor, RouteSendError
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import SocialError
from yuki.social.service import SocialContext


async def route_snapshot(env):
    async with env.db.read_session() as session:
        result = {}
        for name, model, key in (
            ("person", PersonActiveRouteModel, env.person),
            ("space", SpaceActiveRouteModel, env.space),
        ):
            row = await session.get(model, key)
            result[name] = {
                column.name: getattr(row, column.name) for column in model.__table__.columns
            }
        return result


async def delivery_evidence(env, result):
    async with env.db.read_session() as session:
        row = await session.get(SocialOperationModel, result["operation_id"])
        return {
            "status": row.status,
            "event_id": row.event_id,
            "source_conversation_id": row.source_conversation_id,
            "conversation_id": row.original_conversation_id,
            "route": json.loads(row.prepared_json)["route"],
            "platform_reference": row.platform_reference,
        }


@pytest.mark.parametrize("recovery", ["original", "unique"])
async def test_ambiguous_disconnect_recovers_proactive_delivery(
    database, tmp_path, tool_delivery_evidence, recovery
):
    _, evidence = tool_delivery_evidence
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    bots = [Bot("80002"), Bot("80003")]
    presences = []
    async with database.immediate_session() as session:
        for bot in bots:
            presences.append(await ensure_presence(session, bot.self_id))
    for bot, presence in zip(bots, presences, strict=True):
        env.registry.connect(bot, provider_id="snowluma", presence_id=presence)
    monitor = RouteMonitor(env.router)
    await monitor.on_connection_change()
    initial = await route_snapshot(env)
    evidence.update(recovery=recovery, initial=initial)
    env.registry.disconnect(env.bot)
    await monitor.on_connection_change()
    evidence["ambiguous"] = await route_snapshot(env)
    assert evidence["ambiguous"] == initial

    private_args = {"text": "proactive private", "target": {"person_id": env.person}}
    with pytest.raises(RouteSendError, match="disconnected"):
        await env.service.execute("send", private_args, replace(env.context, call_id="offline"))
    assert not [call for bot in bots for call in bot.calls if call[0].startswith("send")]

    if recovery == "original":
        env.registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)
        expected_presence = env.presence
    else:
        env.registry.disconnect(bots[1])
        expected_presence = presences[0]
    await monitor.on_connection_change()
    recovered = await route_snapshot(env)
    evidence["recovered"] = recovered
    for kind, row in recovered.items():
        assert not row["paused"] and row["presence_id"] == expected_presence
        assert row["route_generation"] == initial[kind]["route_generation"] + (recovery == "unique")

    private = await env.service.execute("send", private_args, env.context)
    reply_context = SocialContext(
        "private-turn",
        "group-send",
        (await delivery_evidence(env, private))["conversation_id"],
        person_refs={"current_speaker": env.person},
    )
    group = await env.service.execute(
        "send", {"text": "proactive group", "target": {"space_id": env.space}}, reply_context
    )
    evidence["deliveries"] = [await delivery_evidence(env, r) for r in (private, group)]
    assert all(
        r["status"] == "succeeded" and r["platform_reference"] for r in evidence["deliveries"]
    )
    assert all(r["route"]["presence_id"] == expected_presence for r in evidence["deliveries"])
    assert evidence["deliveries"][0]["source_conversation_id"] == env.context.conversation_id
    assert evidence["deliveries"][0]["conversation_id"] != env.context.conversation_id
    before = [len(bot.calls) for bot in [env.bot, *bots]]
    assert await env.service.execute("send", private_args, env.context) == private
    assert [len(bot.calls) for bot in [env.bot, *bots]] == before
    evidence["gateway_calls"] = {bot.self_id: bot.calls for bot in [env.bot, *bots]}


async def test_legacy_pause_current_replies_explicit_restore_and_account_transfer(
    database, tmp_path, tool_delivery_evidence
):
    _, evidence = tool_delivery_evidence
    env = await social_env(database, tmp_path)
    wire = []

    def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        wire.append((method, params))
        if method == "getMe":
            result = {"id": 80010, "is_bot": True, "username": "offline_yuki"}
        else:
            assert method == "sendMessage"
            result = {"message_id": 1000 + len(wire), "chat": {"id": params["chat_id"]}}
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        tg = TelegramClient("offline-only-token", client=transport)
        await tg.get_me()
        now = datetime.now(UTC)
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id=tg.self_id, platform="telegram", now=now
            )
            account = await ensure_account(session, "10010", platform="telegram", now=now)
            owner = await session.get(CanonicalPersonModel, env.person)
            binding = await replace_assignment(session, account, None, owner, now=now)
            tg_binding, tg_presence = binding.id, presence.id
            await create_presence(session, external_account_id="yuki-app", platform="app", now=now)
            session.add(
                PersonActiveRouteModel(
                    person_id=env.person,
                    identity_binding_id=tg_binding,
                    presence_id=tg_presence,
                    route_generation=7,
                    revision=7,
                    paused=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        env.registry.connect(tg, provider_id="telegram", presence_id=tg_presence)
        contexts = []
        for platform, bot_id, user_id in [("qq", "80001", "10001"), ("telegram", "80010", "10010")]:
            event = (
                await seed_scoped_event(
                    env.service.writer,
                    scope=ConversationScope.private(bot_id, user_id, platform=platform),
                    origin_key=f"{platform}-request",
                    sender_user_id=user_id,
                    direction="inbound",
                    content="offline reply request",
                )
            ).event
            contexts.append(
                SocialContext(
                    f"{platform}-turn",
                    "reply",
                    event.canonical_conversation_id,
                    person_refs={"current_speaker": env.person},
                    trigger_event_id=event.id,
                    presence_id=event.ingress_presence_id,
                )
            )
        before = await route_snapshot(env)
        evidence["legacy_pause"] = before
        await RouteMonitor(env.router).on_connection_change()
        assert await route_snapshot(env) == before
        args = {"text": "same person", "target": {"person_id": env.person}}
        with pytest.raises(SocialError, match="route_paused"):
            await env.service.execute("send", args, env.context)
        qq_result = await env.service.execute("send", args, contexts[0])
        tg_result = await env.service.execute("send", {"text": "current TG reply"}, contexts[1])
        evidence["current_replies"] = [
            await delivery_evidence(env, r) for r in (qq_result, tg_result)
        ]
        assert [r["route"]["platform"] for r in evidence["current_replies"]] == ["qq", "telegram"]
        assert all(r["route"]["kind"] == "conversation" for r in evidence["current_replies"])
        assert await route_snapshot(env) == before

        identity = client_identity_service(database, env.router)
        issued = await identity.issue_for_operator(env.person, str(uuid4()), str(uuid4()))
        device = await identity.pair(issued["code"], "offline owner")
        principal = await identity.authenticate(device["token"])
        client_route = ClientProactiveRoute(identity)
        request_id = str(uuid4())
        restored = await client_route.set(principal, request_id, 7, tg_binding)
        assert restored["binding_id"] == tg_binding and restored["platform"] == "telegram"
        assert not restored["paused"] and restored["revision"] == 8
        assert await client_route.set(principal, request_id, 7, tg_binding) == {
            **restored,
            "replay": True,
        }
        with pytest.raises(ClientError, match="version_conflict"):
            await client_route.set(principal, str(uuid4()), 7, tg_binding)
        evidence["explicit_restore"] = restored
        proactive = await env.service.execute(
            "send", args, replace(env.context, call_id="restored")
        )
        evidence["proactive"] = await delivery_evidence(env, proactive)
        assert evidence["proactive"]["route"]["platform"] == "telegram"
        assert evidence["proactive"]["route"]["kind"] == "person"

        async with database.immediate_session() as session:
            other_person = await ensure_person(session, "10002")
            binding = await session.get(IdentityBindingModel, tg_binding)
            await identity.transfer_binding(session, binding, other_person, now=datetime.now(UTC))
        transferred = await route_snapshot(env)
        await RouteMonitor(env.router).on_connection_change()
        assert transferred == await route_snapshot(env)
        assert transferred["person"]["paused"]
        calls = (len(env.bot.calls), len(wire))
        with pytest.raises(SocialError, match="route_paused"):
            await env.service.execute("send", args, replace(env.context, call_id="after-transfer"))
        with pytest.raises(RouteSendError, match="conversation_entry_unavailable"):
            await env.service.execute("send", args, replace(contexts[1], call_id="after-transfer"))
        with pytest.raises(ClientError, match="not_found"):
            await client_route.set(principal, str(uuid4()), 9, tg_binding)
        assert (len(env.bot.calls), len(wire)) == calls
        evidence.update(after_transfer=transferred, qq_calls=env.bot.calls, telegram_wire=wire)
