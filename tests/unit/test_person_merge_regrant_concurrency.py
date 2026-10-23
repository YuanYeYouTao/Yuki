"""Same-human merge and explicit plugin regrant retain their separate sources."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from tests.support.canonical_identity import ensure_person
from tests.support.client_identity import client_identity_service
from tests.unit.test_notification_preparation_sources import prepare_fixture

from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.operations.base import CommandExecutor
from yuki.admin.operations.identity import IdentityOperations
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.identity import PrincipalId
from yuki.identity.person_identity import current_person_id
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.plugin_host import notification_repository as notifications
from yuki.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginNotificationOutboxModel,
)
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


def row_values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@pytest.mark.parametrize("first_commit", ["merge", "regrant"])
async def test_concurrent_merge_regrant_preserves_new_grant_and_original_publication(
    database, tmp_path, monkeypatch, first_commit
):
    (
        env,
        repository,
        _,
        _,
    ) = await prepare_fixture(database, tmp_path, private=True)
    async with database.immediate_session() as writer:
        root = await ensure_person(writer, "10002")
    original_intents = await repository.unprepared_outbox()
    conversation_id = original_intents[0].canonical_conversation_id
    target = NotificationTarget(
        target_type="private", target_id=env.person, conversation_id=conversation_id
    )
    request = PublishNotificationRequest(
        target=target,
        external_source="concurrent-regrant-fixture",
        event_key="original-A",
        event_type="accepted-work",
        summary="original accepted publication",
        text="original accepted text",
        ask_agent=True,
        agent_intent="original accepted task",
        occurred_at=datetime.now(UTC),
    )
    published = await repository.publish(plugin_id="prepare-source", request=request)
    # Frozen accepted result/budget facts: this offline regression never calls a model.
    async with database.immediate_session() as writer:
        paid_job = await writer.scalar(
            select(PluginBackgroundTurnJobModel).where(
                PluginBackgroundTurnJobModel.source_event_id == published.source_event_id
            )
        )
        paid_job.model_requests, paid_job.tool_calls_used = 2, 1
        paid_job.generated_text = "original generated result"
    async with database.read_session() as reader:
        original_event = await reader.get(ChatEventModel, published.source_event_id)
        original_bytes = original_event.segments_json
        original_parts = [
            row_values(row)
            for row in await reader.scalars(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.source_event_id == published.source_event_id
                )
            )
        ]
        original_job = await reader.scalar(
            select(PluginBackgroundTurnJobModel).where(
                PluginBackgroundTurnJobModel.source_event_id == published.source_event_id
            )
        )
        original_job_values = row_values(original_job)
        grant_id = await reader.scalar(
            select(PluginBackgroundTargetGrantModel.id).where(
                PluginBackgroundTargetGrantModel.plugin_id == "prepare-source",
                PluginBackgroundTargetGrantModel.canonical_conversation_id == conversation_id,
            )
        )
    operations = IdentityOperations(
        CommandExecutor(database),
        client_identity_service(database, env.router),
        PeopleRepository(database),
        router=env.router,
    )
    principal = ControlPrincipal(
        principal_id=PrincipalId(uuid4()),
        person_id=None,
        source=PrincipalSource.CLI,
        granted_capabilities=("identity.person.merge",),
        authenticated=True,
        active=True,
    )
    request_id = str(uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    writes_before_release = []

    async def wait_after_preparation():
        if not entered.is_set():
            entered.set()
            await release.wait()

    if first_commit == "merge":
        prepare_presence = notifications._grant_presence

        async def prepared_grant(*args, **kwargs):
            result = await prepare_presence(*args, **kwargs)
            await wait_after_preparation()
            return result

        monkeypatch.setattr(notifications, "_grant_presence", prepared_grant)
    else:
        prepare = env.router.prepare_person_merge_routes

        async def prepared_merge(*args, **kwargs):
            result = await prepare(*args, **kwargs)
            await wait_after_preparation()
            return result

        monkeypatch.setattr(env.router, "prepare_person_merge_routes", prepared_merge)

    def writing(_connection, _cursor, statement, *_args):
        if (
            asyncio.current_task() is pending
            and not release.is_set()
            and statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE"))
        ):
            writes_before_release.append(statement)

    async def merge():
        return await operations.change_person(principal, env.person, request_id, to_person=root)

    async def regrant():
        return await repository.grant_target(
            plugin_id="prepare-source",
            target=target,
            bot_user_id="80001",
            created_by_user_id=root,
            presence_id=env.presence,
        )

    event.listen(database.engine.sync_engine, "before_cursor_execute", writing)
    try:
        pending = asyncio.create_task(regrant() if first_commit == "merge" else merge())
        await asyncio.wait_for(entered.wait(), 10)
        assert not writes_before_release and not pending.done()
        first = await (merge() if first_commit == "merge" else regrant())
        assert first and not pending.done()
        release.set()
        assert await asyncio.wait_for(pending, 10)
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", writing)
    replay = await merge()
    assert replay["replay"] and replay["person_id"] == root
    async with database.read_session() as reader:
        assert await current_person_id(reader, env.person) == root
        grants = list(
            await reader.scalars(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == "prepare-source",
                    PluginBackgroundTargetGrantModel.canonical_conversation_id == conversation_id,
                )
            )
        )
        assert len(grants) == 1 and grants[0].id == grant_id
        assert grants[0].canonical_created_by_person_id == root and grants[0].enabled
        original_event = await reader.get(ChatEventModel, published.source_event_id)
        assert original_event.segments_json == original_bytes
        assert notifications._publication_data(original_event)["source_person_id"] == env.person
        assert [
            row_values(row)
            for row in await reader.scalars(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.source_event_id == published.source_event_id
                )
            )
        ] == original_parts
        assert (
            row_values(await reader.get(PluginBackgroundTurnJobModel, original_job_values["id"]))
            == original_job_values
        )
    newer = await repository.publish(
        plugin_id="prepare-source",
        request=request.model_copy(update={"event_key": "new-B", "summary": "new authorization"}),
    )
    async with database.read_session() as reader:
        event_b = await reader.get(ChatEventModel, newer.source_event_id)
        assert notifications._publication_data(event_b)["source_person_id"] == root
        job = await reader.scalar(
            select(PluginBackgroundTurnJobModel).where(
                PluginBackgroundTurnJobModel.source_event_id == newer.source_event_id
            )
        )
        assert job.source_person_id == root
    repeated = await repository.publish(plugin_id="prepare-source", request=request)
    assert repeated.source_event_id == published.source_event_id and repeated.deduplicated
    async with database.read_session() as reader:
        receipts = list(
            await reader.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.principal_id == principal.principal_id.text,
                    ControlCommandReceiptModel.request_id == request_id,
                )
            )
        )
        assert len(receipts) == 1
