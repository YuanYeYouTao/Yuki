"""Target contracts from the backend audit, using actual identity and ledger owners."""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from sqlalchemy import func, select
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.event_seed import seed_scoped_event
from tests.support.gateway import builtin_registry
from tests.support.social_identity_cases import social_env
from tests.support.user_profiles_helpers import observe_members
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_normalizer import group_event

from yuki.adapters.onebot.normalizer import normalize_event
from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import mention_snapshots
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError
from yuki.social.service import SocialContext


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def paired_owner(env, database):
    async with database.read_session() as session:
        person = (await find_identity_binding(session, "1001")).person_id
    code = await env.identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
    response = await env.client.post(
        "/client/v1/pair", json={"code": code["code"], "device_label": "audit-contract"}
    )
    assert response.status_code == 200
    return response.json()["data"], person


async def finish_app_turns(env):
    if env.gateway.tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway.tasks)), 10)


@dataclass
class MentionBot:
    self_id: str = "9999"

    async def call_api(self, *_args, **_kwargs):
        return {}


async def admit_mention(database, message):
    registry = builtin_registry()

    async def member(*_args, **_kwargs):
        return True

    router = PresenceRouter(database, registry, membership_probe=member)
    resolver = CanonicalIngressResolver(database, registry, router)
    ingress = CanonicalIngressUnitOfWork(database, router)
    await observe_members(database, ("1001", "1002"))
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "9999")
    bot = MentionBot()
    registry.connect(bot, provider_id="snowluma", presence_id=presence)
    native = replace(normalize_event(group_event(message)), occurred_at=datetime.now(UTC))
    admitted = await resolver.pre_admit(bot, native)
    recorded = await ingress.append_inbound(admitted.message, admitted)
    return native, admitted, recorded.event


@pytest.mark.parametrize("paused", [False, True])
async def test_edit_person_references_follow_the_native_snapshot_even_when_target_paused(
    database, paused
):
    native, admitted, original = await admit_mention(
        database, Message([MessageSegment.at(1002), MessageSegment.text(" question")])
    )
    target = admitted.message.mentions[0].person_id
    assert target is not None
    if paused:
        async with database.immediate_session() as writer:
            (await find_identity_binding(writer, "1002")).status = "disabled"
    await ScopedEventLedgerUnitOfWork(database).update_platform_message(
        original.id,
        native,
        presence_id=admitted.presence_id,
        edited_at=datetime.now(UTC) + timedelta(seconds=1),
        revision=2,
    )
    current = await EventLedgerRepository(database).get_event(original.id)
    assert current.id == original.id and current.author_person_id == original.author_person_id
    assert current.canonical_conversation_id == original.canonical_conversation_id
    assert mention_snapshots(current.segments)[0]["person_id"] == target
    assert current.mentioned_person_ids == (target,)


async def test_edit_does_not_invent_a_person_relation_from_an_unlocated_account_list(database):
    native, admitted, original = await admit_mention(database, Message("plain @card-1002"))
    assert not original.mentioned_person_ids and not admitted.message.mentions
    await ScopedEventLedgerUnitOfWork(database).update_platform_message(
        original.id,
        replace(native, mentioned_user_ids=("1002",)),
        presence_id=admitted.presence_id,
        edited_at=datetime.now(UTC) + timedelta(seconds=1),
        revision=2,
    )
    current = await EventLedgerRepository(database).get_event(original.id)
    assert not mention_snapshots(current.segments)
    assert current.mentioned_person_ids == ()


@pytest.mark.parametrize("change", ["active", "disabled", "unbound"])
async def test_authenticated_device_never_silently_changes_new_message_person(
    app_env, database, change
):
    env = app_env
    device, person = await paired_owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    headers = bearer(device["token"])
    original_request = {"request_id": str(uuid4()), "text": "original admitted input"}
    response = await env.client.post("/client/v1/messages", headers=headers, json=original_request)
    assert response.status_code == 200
    original_result = response.json()["data"]
    await finish_app_turns(env)
    async with database.read_session() as reader:
        binding = await reader.get(IdentityBindingModel, principal.binding_id)
        revision = binding.revision
        before_people = await reader.scalar(select(func.count()).select_from(CanonicalPersonModel))
        original = await reader.get(ChatEventModel, original_result["event_id"])
        original_fact = (
            original.author_person_id,
            original.canonical_conversation_id,
            original.content,
        )
    if change != "active":
        action = "disable" if change == "disabled" else "unbind"
        altered = await env.client.post(
            f"/admin/v1/bindings/{binding.id}/{action}",
            headers=bearer(TOKEN),
            json={"request_id": str(uuid4()), "expected_revision": revision},
        )
        assert altered.status_code == 200 and altered.json()["data"]["success"]
    replay = await env.client.post("/client/v1/messages", headers=headers, json=original_request)
    assert (
        replay.status_code == 200
        and replay.json()["data"]["event_id"] == original_result["event_id"]
    )
    assert (
        await env.client.get(
            f"/client/v1/requests/{original_request['request_id']}", headers=headers
        )
    ).status_code == 200
    assert (await env.client.get("/client/v1/me", headers=headers)).json()["data"][
        "person_id"
    ] == person
    calls = len(env.calls)
    new_text = f"new input after {change}"
    response = await env.client.post(
        "/client/v1/messages", headers=headers, json={"request_id": str(uuid4()), "text": new_text}
    )
    await finish_app_turns(env)
    async with database.read_session() as reader:
        new = list(
            await reader.scalars(select(ChatEventModel).where(ChatEventModel.content == new_text))
        )
        old = await reader.get(ChatEventModel, original_result["event_id"])
        assert (old.author_person_id, old.canonical_conversation_id, old.content) == original_fact
        assert (
            await reader.scalar(select(func.count()).select_from(CanonicalPersonModel))
            == before_people
        )
    if response.status_code == 200:
        assert [row.author_person_id for row in new] == [person]
        assert new[0].canonical_conversation_id == principal.conversation_id
    else:
        assert response.status_code in {401, 403, 409} and not new and len(env.calls) == calls
    if change == "active":
        assert response.status_code == 200
    if change == "disabled":
        assert response.status_code == 401 and not new


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("selector", ["operation", "conversation"])
async def test_reading_a_successful_receipt_is_independent_of_space_send_qualification(
    database, tmp_path, paused, selector
):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "confirmed original"}, env.context)
    if paused:
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalSpaceModel, env.space)).enabled = False
    calls = list(env.bot.calls)
    args = (
        {"view": "recent", "target": {"operation_id": sent["operation_id"]}}
        if selector == "operation"
        else {"view": "recent", "target": {"conversation_id": env.context.conversation_id}}
    )
    result = await env.service.execute("query_chat_history", args, env.context)
    assert any(row["event_id"] == sent["event_id"] for row in result["events"])
    receipt = await env.service.receipts.get(sent["operation_id"])
    assert receipt.status == "succeeded" and receipt.event_id == sent["event_id"]
    assert env.bot.calls == calls


@pytest.mark.parametrize("paused", [False, True])
async def test_other_source_cannot_read_a_successful_operation_receipt(database, tmp_path, paused):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "original"}, env.context)
    if paused:
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalSpaceModel, env.space)).enabled = False
    with pytest.raises(SocialError, match=r"history_(receipt_)?unavailable"):
        await env.service.execute(
            "query_chat_history",
            {
                "view": "recent",
                "event_kind": "all",
                "target": {"operation_id": sent["operation_id"]},
            },
            replace(env.context, conversation_id=str(uuid4())),
        )


@pytest.mark.parametrize("disabled", [False, True])
async def test_delegated_history_reads_person_material_without_send_availability(
    database, tmp_path, disabled
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        target = await ensure_person(writer, "10002")
    original = (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.private("80001", "10002"),
            platform_message_id="private-material",
            sender_user_id="10002",
            direction="inbound",
            content="private original",
        )
    ).event
    if disabled:
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, target)).enabled = False
    runtime = ToolRuntime(
        inbound=None,
        conversation_id=env.context.conversation_id,
        execution_id="delegated-read",
        read_scope=ConversationScope.private("80001", "10002"),
        read_target_id=target,
    )
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="delegated-read"))
    calls = list(env.bot.calls)
    try:
        result = await invoke_social(
            env.service,
            "query_chat_history",
            {"view": "recent", "target": {"conversation_id": original.canonical_conversation_id}},
            runtime,
        )
    finally:
        current_invocation.reset(token)
    assert [row["event_id"] for row in result["events"]] == [original.id]
    assert env.bot.calls == calls


async def test_delegated_history_scope_is_still_required(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        await ensure_person(writer, "10002")
    original = (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.private("80001", "10002"),
            platform_message_id="other-person-material",
            sender_user_id="10002",
            direction="inbound",
            content="private",
        )
    ).event
    runtime = ToolRuntime(
        inbound=None,
        conversation_id=env.context.conversation_id,
        execution_id="wrong-read",
        read_scope=ConversationScope.private("80001", "10001"),
        read_target_id=env.person,
    )
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="wrong-read"))
    try:
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service,
                "query_chat_history",
                {
                    "view": "recent",
                    "target": {"conversation_id": original.canonical_conversation_id},
                },
                runtime,
            )
    finally:
        current_invocation.reset(token)


async def test_paired_empty_app_history_does_not_require_an_inbound_message(app_env, database):
    device, person = await paired_owner(app_env, database)
    principal = await app_env.identity.authenticate(device["token"])
    result = await app_env.social.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"conversation_id": principal.conversation_id},
        },
        SocialContext(
            "empty-history",
            "empty-history",
            principal.conversation_id,
            person_refs={"current_speaker": person},
        ),
    )
    assert result["events"] == [] and not app_env.calls
