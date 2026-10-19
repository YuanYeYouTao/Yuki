"""A retired actor remains a readable recipient alias after same-human merge."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_notification_preparation_sources import prepare_fixture

from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons
from yuki.identity.routing import RouteSendError
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def recipient_fixture(database, tmp_path):
    env, repository, worker, old_event = await prepare_fixture(database, tmp_path, private=True)
    async with database.immediate_session() as writer:
        root = str(uuid4())
        now = datetime.now(UTC)
        writer.add(CanonicalPersonModel(id=root, created_at=now, updated_at=now))
    target = NotificationTarget(
        target_type="private",
        target_id=env.person,
        conversation_id=(await repository.unprepared_outbox())[0].canonical_conversation_id,
    )
    await repository.grant_target(
        plugin_id="prepare-source",
        target=target,
        bot_user_id="80001",
        created_by_user_id=root,
        presence_id=env.presence,
    )
    publication = await repository.publish(
        plugin_id="prepare-source",
        request=PublishNotificationRequest(
            target=target,
            external_source="fixture",
            event_key="root-actor",
            event_type="fixture",
            summary="current actor, original target",
            text="original recipient",
            occurred_at=datetime.now(UTC),
        ),
    )
    return env, repository, worker, old_event, root, publication.source_event_id, target


async def merge_disabled_recipient(database, source, root):
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, source)).enabled = False
        await merge_persons(writer, source, root)


@pytest.mark.asyncio
async def test_original_publication_target_alias_does_not_revoke_current_actor(database, tmp_path):
    env, repository, worker, old_event, root, event_id, _target = await recipient_fixture(
        database, tmp_path
    )
    await merge_disabled_recipient(database, env.person, root)
    await worker._prepare_pending()
    item = await repository.claim_outbox()
    assert item is not None and item.source_event_id == event_id
    assert item.source_person_id == root and item.canonical_target_person_id == env.person
    await worker._deliver(item)
    async with database.sessions() as reader:
        rows = {
            row.source_event_id: row
            for row in await reader.scalars(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.source_event_id.in_((old_event, event_id))
                )
            )
        }
        assert rows[event_id].status == "sent"
        assert rows[event_id].canonical_target_person_id == env.person
        assert rows[event_id].source_person_id == root
        assert rows[old_event].status == "failed"
        assert rows[old_event].last_error_category == "canonical_owner_disabled"
    assert sum(action == "send_private_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_recipient_routes_use_current_person_without_reviving_original_actor(
    database, tmp_path
):
    env, _, _, _, root, _, _target = await recipient_fixture(database, tmp_path)
    async with database.sessions() as reader:
        binding_id = (await find_identity_binding(reader, "10001")).id
    await merge_disabled_recipient(database, env.person, root)
    assert await env.router.cas_takeover_person(root) == "taken"
    route = await env.router.resolve_send_for_person(env.person)
    assert route.binding_id == binding_id and route.external_target_id == "10001"
    async with database.sessions() as reader:
        await env.router.validate_prepared_send(
            reader, route, target_kind="person", target_id=env.person
        )
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, root)).enabled = False
    with pytest.raises(RouteSendError, match="paused"):
        await env.router.resolve_send_for_person(env.person)


@pytest.mark.asyncio
async def test_private_send_receipt_and_recall_preserve_original_entry_after_merge(
    database, tmp_path
):
    env, _, _, _, root, _, target = await recipient_fixture(database, tmp_path)
    context = replace(
        env.context,
        conversation_id=target.conversation_id,
        space_id=None,
        person_refs={"current_speaker": env.person},
    )
    args = {"text": "original private text"}
    original = await env.service.execute("send_message", args, context)
    assert original["status"] == "succeeded"
    await merge_disabled_recipient(database, env.person, root)
    delivered = await env.service.execute(
        "send_message", {"text": "same human"}, replace(context, call_id="current-person")
    )
    assert delivered["status"] == "succeeded"
    assert delivered["target"]["id"] == str(UUID(root))
    assert delivered["delivered_text_available"] and delivered["delivered_text"] == "same human"
    recalled = await env.service.execute(
        "recall_own_message",
        {"event_id": original["event_id"]},
        replace(context, call_id="recall-original"),
    )
    assert recalled["status"] == "succeeded"
    assert env.bot.calls[-1][0] == "delete_msg"
