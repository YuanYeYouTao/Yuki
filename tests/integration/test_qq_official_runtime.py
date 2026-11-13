"""QQ official Bot events → real SQLite/Processor/Main/Social, with an offline platform.

Failure cases written before the implementation:

- A private message's reply doesn't carry the triggering ``msg_id``/``msg_seq`` 1 or
  isn't kept as the conversation's outbound event with a succeeded receipt.
12. A group reply refused with 40034128 is recorded as delivered (or uncertain), is
    retried, or goes anywhere else.
13. The official Bot's openid is merged with a OneBot QQ account of the same text, or a
    Person is inferred from it; a member_openid and the same user_openid become two
    accounts.
16. In full-message mode Yuki's own message pushed back by the platform is admitted as
    a new inbound message and buys another model request.
- Another Bot's message in the group triggers a reply.
- A quoted message (message_type 103) loses its link to the original event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.qq_official import FakeQQPlatform
from tests.support.service_dependencies import social_service

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.qq_official import QQOfficialClient
from yuki.gateway.providers.qq_official_ingress import QQOfficialGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_person_binding, create_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import AccountModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.main_agent_contract import MainAgentContract
from yuki.social.db_models import SocialOperationModel
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


@pytest.fixture
async def qq_env(database, tmp_path):
    def respond(request):
        if request.messages[-1].role == "tool":
            return "done"
        return ChatResponse(
            "", 0, tool_calls=(ToolCall("send", ToolFunction("send", '{"text":"收到"}')),)
        )

    platform = FakeQQPlatform()
    model = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), model)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    harness.processor._group_recovery = NativeGroupRecovery(
        harness.settings, database, registry, router
    )
    social = social_service(database, router, harness.scoped_events)
    harness.processor._chat._tools.social_service = social
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(WorkspaceStore(tmp_path / "workspace"))
    )
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        client.self_id = "BOT"
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id="BOT", platform="qq_official", now=datetime.now(UTC)
            )
        gateway = QQOfficialGateway(
            database, registry, router, harness.processor, harness.scoped_events, client
        )
        gateway.connection.presence_id = presence.id
        registry.connect(gateway.connection, provider_id="qq_official", presence_id=presence.id)
        yield SimpleNamespace(gateway=gateway, model=model, platform=platform)
        await gateway.close()


def private(identifier="C2C-1", text="你好", **extra):
    return {
        "id": identifier,
        "content": text,
        "timestamp": "2026-11-13T10:00:00+08:00",
        "author": {"id": "AUTHOR-1", "user_openid": "MEMBER-1"},
        **extra,
    }


def group(identifier="G-1", text="你好", **extra):
    return {
        "id": identifier,
        "content": text,
        "group_openid": "GROUP-1",
        "timestamp": "2026-11-13T10:00:00+08:00",
        "author": {"id": "AUTHOR-1", "member_openid": "MEMBER-1"},
        **extra,
    }


async def drain(env):
    if env.gateway._turns:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway._turns)), 10)


async def events(database):
    async with database.read_session() as session:
        return list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))


async def receipts(database):
    async with database.read_session() as session:
        return list(await session.scalars(select(SocialOperationModel)))


async def test_private_reply_is_passive_to_the_trigger(qq_env, database):
    env = qq_env
    await env.gateway.receive("C2C_MESSAGE_CREATE", private())
    await drain(env)
    sent = env.platform.messages("/v2/users/MEMBER-1/")
    assert [item.body for item in sent] == [
        {"msg_type": 0, "content": "收到", "msg_id": "C2C-1", "msg_seq": 1}
    ]
    rows = await events(database)
    assert [(row.direction, row.native_message_id) for row in rows] == [
        ("inbound", "C2C-1"),
        ("outbound", "OUT-1"),
    ]
    assert rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
    assert [row.status for row in await receipts(database)] == ["succeeded"]


async def test_group_reply_limit_is_a_failed_receipt_without_retry(qq_env, database):
    env = qq_env
    env.platform.reply("POST", "/v2/groups/GROUP-1/messages", 400, code=40034128, message="limit")
    await env.gateway.receive("GROUP_AT_MESSAGE_CREATE", group(text=" 在吗"))
    await drain(env)
    assert len(env.platform.messages()) == 1
    assert env.platform.messages()[0].body["msg_id"] == "G-1"
    [receipt] = await receipts(database)
    assert receipt.status == "failed" and receipt.error_category == "QQOfficialReplyLimit"
    assert [row.direction for row in await events(database)] == ["inbound"]


async def test_openid_is_its_own_account_and_never_onebot(qq_env, database):
    env = qq_env
    async with database.immediate_session() as session:
        onebot = await create_person_binding(
            session, external_account_id="MEMBER-1", display_name="", now=datetime.now(UTC)
        )
    await env.gateway.receive("C2C_MESSAGE_CREATE", private())
    await env.gateway.receive("GROUP_AT_MESSAGE_CREATE", group(text=" 在吗"))
    await drain(env)
    async with database.read_session() as session:
        accounts = list(
            await session.scalars(
                select(AccountModel).where(AccountModel.external_account_id == "MEMBER-1")
            )
        )
        bindings = list(
            await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == "MEMBER-1"
                )
            )
        )
    assert sorted(account.platform for account in accounts) == ["qq", "qq_official"]
    official = [binding for binding in bindings if binding.platform == "qq_official"]
    assert len(official) == 1 and official[0].person_id != onebot.person_id
    authors = {row.author_person_id for row in await events(database) if row.direction == "inbound"}
    assert authors == {official[0].person_id}


async def test_own_echo_and_other_bots_do_not_start_main(qq_env, database):
    env = qq_env
    await env.gateway.receive("GROUP_AT_MESSAGE_CREATE", group(text=" 在吗"))
    await drain(env)
    requests, rows = len(env.model.requests), len(await events(database))
    assert [row.native_message_id for row in await events(database)] == ["G-1", "OUT-1"]

    echo = group("OUT-1", "收到", author={"id": "BOT-ID", "member_openid": "BOT-M", "bot": True})
    await env.gateway.receive("GROUP_MESSAGE_CREATE", echo)
    other = group(
        "G-2",
        "<@BOTX> 我也是机器人",
        author={"id": "OTHER", "member_openid": "OTHER-M", "bot": True},
        mentions=[{"id": "BOTX", "is_you": True}],
    )
    await env.gateway.receive("GROUP_MESSAGE_CREATE", other)
    await drain(env)
    assert len(env.model.requests) == requests
    assert len(await events(database)) == rows
    assert len(env.platform.messages()) == 1


async def test_quote_links_to_the_original_event(qq_env, database):
    env = qq_env
    await env.gateway.receive(
        "C2C_MESSAGE_CREATE",
        private(message_scene={"ext": ["msg_idx=REFIDX_first"]}),
    )
    await drain(env)
    await env.gateway.receive(
        "C2C_MESSAGE_CREATE",
        private(
            "C2C-2",
            "这句呢",
            message_type=103,
            msg_elements=[{"content": "你好"}],
            message_scene={"ext": ["msg_idx=REFIDX_second", "ref_msg_idx=REFIDX_first"]},
        ),
    )
    await drain(env)
    first, *_rest, quoted = [row for row in await events(database) if row.direction == "inbound"]
    assert quoted.native_message_id == "C2C-2"
    assert quoted.reply_to_message_id == "C2C-1" and quoted.reply_to_event_id == first.id
