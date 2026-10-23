"""Link commands precede automatic identity creation and never become chat input."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.client_identity import client_identity_service
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from yuki.client_api.db_models import ClientLinkCodeModel
from yuki.domain.messages import MessageMention
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import current_person_id
from yuki.persistence.models import ChatEventModel
from yuki.services.client_link import ClientLinkCommands
from yuki.services.execution_sources import recover_source


async def stack(database):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    identities = client_identity_service(database, resolver._router)
    async with database.read_session() as session:
        target = await find_identity_binding(session, "1001")
    code = await identities.issue_for_operator(target.person_id, str(uuid4()), str(uuid4()))
    return registry, bot, identities, target.person_id, code["code"]


@pytest.mark.parametrize("native_at", [False, True])
async def test_first_unseen_account_links_without_new_person_ledger_or_main(database, native_at):
    registry, bot, identities, target, code = await stack(database)
    harness = build_harness(database, make_settings(database.url))
    harness.processor._client_links = ClientLinkCommands(identities, registry)
    forbidden = AsyncMock(side_effect=AssertionError("link reached ordinary admission"))
    harness.processor._canonical_ingress = SimpleNamespace(pre_admit=forbidden)
    sender = MemorySender()
    sender.bot = bot
    async with database.read_session() as session:
        count = await session.scalar(select(func.count(CanonicalPersonModel.id)))
    incoming = _message(message_id="link-first", user_id="45454545", text=f"/ai link {code}")
    if native_at:
        incoming = replace(
            incoming,
            text="@Renamed " + incoming.text,
            mentions_bot=True,
            mentions=(MessageMention("yuki", 0, 0, 8, "8000", display_name="Renamed"),),
        )
    source = incoming.text, incoming.mentions
    result = await harness.processor.handle(incoming, sender)
    assert result.reason == "client_link" and sender.calls == 1
    forbidden.assert_not_awaited()
    assert not harness.provider.requests
    assert (incoming.text, incoming.mentions) == source
    async with database.read_session() as session:
        assert await session.scalar(select(func.count(CanonicalPersonModel.id))) == count
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 0
        assert (await find_identity_binding(session, "45454545")).person_id == target


async def test_native_bot_address_without_argument_issues_a_real_link_code(database):
    registry, bot, identities, person, _ = await stack(database)
    handler = ClientLinkCommands(identities, registry)
    incoming = replace(
        _message(message_id="native-issue", text="@Renamed /ai link"),
        mentions_bot=True,
        mentions=(MessageMention("yuki", 0, 0, 8, "8000", display_name="Renamed"),),
    )
    assert handler.is_request(incoming)
    response = await handler.execute(bot, incoming)
    assert response.startswith("绑定码：")
    async with database.read_session() as session:
        issued = await session.scalar(
            select(ClientLinkCodeModel).where(ClientLinkCodeModel.issuer_source == "qq")
        )
        assert issued is not None and issued.person_id == person and issued.used_at is None
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 0


async def test_configured_public_url_generates_the_original_private_chat_code_link(database):
    from urllib.parse import parse_qs, urlsplit

    registry, bot, identities, person, _ = await stack(database)
    handler = ClientLinkCommands(identities, registry, "https://public.example/role/one")
    response = await handler.execute(bot, _message(message_id="public-link", text="/ai link"))
    link = response.split("绑定链接：", 1)[1]
    query = parse_qs(urlsplit(link).query)
    assert query["server"] == ["https://public.example/role/one"]
    assert query["code"][0] in response.splitlines()[0]
    result = await identities.pair(query["code"][0], "desktop")
    assert (await identities.authenticate(result["token"])).person_id == person


@pytest.mark.parametrize("native_member", [False, True])
def test_literal_or_other_member_address_is_not_a_link_request(native_member):
    incoming = _message(message_id="literal-link", text="@Renamed /ai link")
    if native_member:
        incoming = replace(
            incoming,
            mentions=(MessageMention("member", 0, 0, 8, "45454545", display_name="Renamed"),),
        )
    assert not ClientLinkCommands(None, None).is_request(incoming)


async def test_authenticated_merge_preserves_old_source_and_reused_code_writes_nothing(
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
    assert "身份已合并" in response
    assert await recover_source(
        database, original.canonical_conversation_id, source, request_id="old-work"
    )
    async with database.read_session() as session:
        current = await find_identity_binding(session, "1002")
        assert current.person_id == original.author_person_id
        assert await current_person_id(session, current.person_id) == target
        bindings = await session.scalar(select(func.count(IdentityBindingModel.id)))
    response = await handler.execute(
        bot, _message(message_id="reused", user_id="1002", text=f"/ai link {code}")
    )
    assert "不可用" in response
    async with database.read_session() as session:
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
    async with database.read_session() as session:
        assert await find_identity_binding(session, "56565656") is None


async def test_connection_cannot_prove_possession_on_another_platform(database):
    registry, bot, identities, _, code = await stack(database)
    handler = ClientLinkCommands(identities, registry)
    response = await handler.execute(
        bot,
        replace(
            _message(message_id="wrong-platform", user_id="45454545", text=f"/ai link {code}"),
            platform="telegram",
        ),
    )
    assert "不可用" in response
    async with database.read_session() as session:
        assert (await session.scalar(select(ClientLinkCodeModel))).used_at is None
        assert await find_identity_binding(session, "45454545", platform="telegram") is None
        assert await find_identity_binding(session, "45454545", platform="qq") is None
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 0


@pytest.mark.parametrize("committed", [True, None])
async def test_link_does_not_reprepare_after_a_commit_acknowledgement_error(
    database, monkeypatch, committed
):
    registry, bot, identities, target, code = await stack(database)
    handler = ClientLinkCommands(identities, registry)
    original_writer = database.immediate_session
    calls = 0
    error = CanonicalIdentityError("person_merge_preparation_changed")
    error.add_note("database_write_phase:cleanup" if committed else "database_write_phase:commit")
    if committed:
        error.add_note("database_commit:confirmed")

    @asynccontextmanager
    async def lost_acknowledgement():
        nonlocal calls
        calls += 1
        async with original_writer() as writer:
            yield writer
        # The physical commit happened; the caller cannot infer that from a
        # preparation-change category carried by a failed acknowledgement.
        raise error

    monkeypatch.setattr(database, "immediate_session", lost_acknowledgement)
    with pytest.raises(CanonicalIdentityError) as raised:
        await handler.execute(
            bot, _message(message_id="ack-lost", user_id="45454545", text=f"/ai link {code}")
        )
    assert raised.value is error and calls == 1
    async with database.read_session() as reader:
        assert (await find_identity_binding(reader, "45454545")).person_id == target
        assert (await reader.scalar(select(ClientLinkCodeModel))).used_at is not None
