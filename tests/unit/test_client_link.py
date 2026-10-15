"""Link commands precede automatic identity creation and never become chat input."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from qq_ai_bot.client_api.identity import ClientIdentityService
from qq_ai_bot.identity.canonical_repository import find_identity_binding
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.client_link import ClientLinkCommands
from qq_ai_bot.services.execution_sources import recover_source


async def stack(database):
    registry, _, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    identities = ClientIdentityService(database)
    async with database.sessions() as session:
        target = await find_identity_binding(session, "1001")
    code = await identities.issue_for_operator(target.person_id, str(uuid4()), str(uuid4()))
    return registry, bot, identities, target.person_id, code["code"]


async def test_first_unseen_account_links_without_new_person_ledger_or_main(database):
    registry, bot, identities, target, code = await stack(database)
    harness = build_harness(database, make_settings(database.url))
    harness.processor._client_links = ClientLinkCommands(identities, registry)
    forbidden = AsyncMock(side_effect=AssertionError("link reached ordinary admission"))
    harness.processor._canonical_ingress = SimpleNamespace(pre_admit=forbidden)
    sender = MemorySender()
    sender.bot = bot
    async with database.sessions() as session:
        count = await session.scalar(select(func.count(CanonicalPersonModel.id)))
    result = await harness.processor.handle(
        _message(message_id="link-first", user_id="45454545", text=f"/ai link {code}"), sender
    )
    assert result.reason == "client_link" and sender.calls == 1
    forbidden.assert_not_awaited()
    assert not harness.provider.requests
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(CanonicalPersonModel.id))) == count
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 0
        assert (await find_identity_binding(session, "45454545")).person_id == target


async def test_authenticated_transfer_invalidates_old_source_and_reused_code_writes_nothing(
    database,
):
    registry, bot, identities, target, code = await stack(database)
    handler = ClientLinkCommands(identities, registry)
    _, resolver, uow = await _stack(database)
    # Use the handler's actual registry for ordinary ingress as well.
    resolver._registry = registry
    resolver._router._registry = registry
    incoming = await resolver.pre_admit(bot, _message(message_id="old-message", user_id="1002"))
    original = (await uow.append_inbound(incoming.message, incoming)).event
    source = {
        "origin": "user_message",
        "generation": 1,
        "trigger_event_id": original.id,
        "actor_person_id": original.author_person_id,
        "presence_id": original.ingress_presence_id,
        "bot_user_id": "8000",
    }
    assert await recover_source(
        database, original.canonical_conversation_id, source, request_id="old-work"
    )
    response = await handler.execute(
        bot, _message(message_id="transfer", user_id="1002", text=f"/ai link {code}")
    )
    assert "旧会话" in response
    with pytest.raises(ValueError, match="task_actor_binding_unavailable"):
        await recover_source(
            database, original.canonical_conversation_id, source, request_id="old-work"
        )
    async with database.sessions() as session:
        current = await find_identity_binding(session, "1002")
        assert current.person_id == target
        bindings = await session.scalar(select(func.count(IdentityBindingModel.id)))
    response = await handler.execute(
        bot, _message(message_id="reused", user_id="1002", text=f"/ai link {code}")
    )
    assert "不可用" in response
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(IdentityBindingModel.id))) == bindings
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


async def test_unknown_or_disconnected_account_cannot_issue_a_code(database):
    registry, bot, identities, _, _ = await stack(database)
    handler = ClientLinkCommands(identities, registry)
    unknown = await handler.execute(
        bot, _message(message_id="unseen", user_id="56565656", text="/ai link")
    )
    assert "不可用" in unknown
    registry.disconnect(bot)
    response = await handler.execute(bot, _message(message_id="disconnected", text="/ai link"))
    assert "身份认证" in response
    async with database.sessions() as session:
        assert await find_identity_binding(session, "56565656") is None
