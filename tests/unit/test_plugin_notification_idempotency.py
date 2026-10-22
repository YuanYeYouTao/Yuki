"""Whole-request publication idempotency for PluginNotificationRepository."""

from __future__ import annotations

import asyncio
import hashlib
import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host import media_artifacts as media_artifacts_module
from yuki.plugin_host import notification_repository as notification_repository_module
from yuki.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_manifest import (
    canonical_json,
    canonical_occurred_at,
    is_sha256_digest,
    media_part_key,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)
_PLUGIN_ID = "test.publication-idempotency"
_TARGET = NotificationTarget(target_type="private", target_id="1001")
_GROUP_TARGET = NotificationTarget(target_type="group", target_id="2001")
_PRESENCE = ""


@pytest.fixture(autouse=True)
async def canonical_notification_targets(database, monkeypatch):
    from yuki.conversation.hydrate import ensure_canonical_conversation
    from yuki.identity.canonical_repository import (
        find_identity_binding,
        find_presence,
        find_space_binding,
    )

    async with database.immediate_session() as session:
        person = await find_identity_binding(session, "1001")
        presence = await find_presence(session, "8000")
        space = await find_space_binding(session, "2001")
        private = await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key="bot:8000:private:1001",
            person_id=person.person_id,
            identity_binding_id=person.id,
            presence_id=presence.id,
        )
        group = await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="bot:8000:group:2001",
            space_id=space.space_id,
            space_binding_id=space.id,
        )
    import sys

    monkeypatch.setattr(
        sys.modules[__name__],
        "_TARGET",
        NotificationTarget(
            target_type="private",
            target_id=person.person_id,
            conversation_id=private.conversation_id,
        ),
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_GROUP_TARGET",
        NotificationTarget(
            target_type="group", target_id=space.space_id, conversation_id=group.conversation_id
        ),
    )
    monkeypatch.setattr(sys.modules[__name__], "_PRESENCE", presence.id, raising=False)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _request(**overrides: Any) -> PublishNotificationRequest:
    values: dict[str, Any] = {
        "event_key": "evt-1",
        "event_type": "fixture.created",
        "external_source": "fixture",
        "target": _TARGET,
        "occurred_at": _NOW,
        "summary": "external summary",
        "payload": {"id": 7, "name": "alpha"},
        "text": "hello",
        "ask_agent": True,
        "agent_intent": "react briefly",
    }
    values.update(overrides)
    return PublishNotificationRequest(**values)


async def _install(
    database: Database, *, target: NotificationTarget | None = None, presence_id: str | None = None
) -> PluginNotificationRepository:
    async with database.immediate_session() as session:
        session.add(
            PluginInstallationModel(
                plugin_id=_PLUGIN_ID,
                name="Publication fixture",
                version="1.0.0",
                plugin_api=PLUGIN_API_VERSION,
                yuki_requires=">=3.8",
                manifest_hash="fixture",
                entrypoint="fixture:plugin",
                status="running",
                enabled=True,
                approved_permissions_json="[]",
                requested_permissions_json="[]",
                failure_count=0,
                last_error_category=None,
                discovered_at=_NOW,
                approved_at=_NOW,
                started_at=_NOW,
                updated_at=_NOW,
            )
        )
    repository = PluginNotificationRepository(database)
    selected = target or _TARGET
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=selected,
        bot_user_id="8000",
        created_by_user_id=selected.target_id,
        presence_id=presence_id if presence_id is not None else _PRESENCE,
    )
    return repository


async def _add_artifact(
    database: Database,
    *,
    handle_id: str,
    digest: str,
    expires_at: datetime | None = None,
) -> None:
    async with database.immediate_session() as session:
        session.add(
            PluginMediaArtifactModel(
                handle_id=handle_id,
                plugin_id=_PLUGIN_ID,
                content_type="image/png",
                filename=f"{handle_id}.png",
                byte_size=8,
                sha256=digest,
                storage_path=f"/tmp/{handle_id}.png",
                created_at=_NOW,
                expires_at=expires_at or (datetime.now(UTC) + timedelta(days=7)),
            )
        )


async def _state(database: Database) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
    async with database.read_session() as session:
        event_ids = tuple(
            row.id
            for row in (
                await session.scalars(
                    select(ChatEventModel)
                    .where(ChatEventModel.event_kind == "external_event")
                    .order_by(ChatEventModel.id)
                )
            ).all()
        )
        part_keys = tuple(
            row.part_key
            for row in (
                await session.scalars(
                    select(PluginNotificationOutboxModel).order_by(PluginNotificationOutboxModel.id)
                )
            ).all()
        )
        intents = tuple(
            row.agent_intent
            for row in (
                await session.scalars(
                    select(PluginBackgroundTurnJobModel).order_by(PluginBackgroundTurnJobModel.id)
                )
            ).all()
        )
    return event_ids, part_keys, intents


async def _media_parts(database: Database) -> list[tuple[str, str | None]]:
    async with database.read_session() as session:
        return [
            (row.part_key, row.media_handle_id)
            for row in (
                await session.scalars(
                    select(PluginNotificationOutboxModel).order_by(PluginNotificationOutboxModel.id)
                )
            ).all()
            if row.part_type == "media"
        ]


async def _prepare_text_delivery(repository):
    from yuki.gateway.social import TransportMessage, transport_payload

    for pending in await repository.unprepared_outbox():
        await repository.prepare_outbox(
            pending,
            (
                {
                    "message": transport_payload(TransportMessage(pending.text)),
                    "route": {
                        "platform": "qq",
                        "presence_id": pending.canonical_presence_id,
                        "conversation_id": pending.canonical_conversation_id,
                    },
                },
            ),
        )


async def _count(database: Database, model: type[Any]) -> int:
    async with database.read_session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_first_publication_rechecks_original_media_in_its_writer(database, monkeypatch):
    repository = await _install(database)
    clock = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(notification_repository_module, "datetime", Clock)
    await _add_artifact(
        database,
        handle_id="original-media",
        digest="a" * 64,
        expires_at=clock + timedelta(seconds=1),
    )
    request = _request(media_handles=("original-media",))
    prepared, continue_publish = asyncio.Event(), asyncio.Event()
    original_writer = database.immediate_session

    @asynccontextmanager
    async def after_preparation():
        prepared.set()
        await continue_publish.wait()
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", after_preparation)
    publishing = asyncio.create_task(repository.publish(plugin_id=_PLUGIN_ID, request=request))
    try:
        async with asyncio.timeout(5):
            await prepared.wait()
        clock += timedelta(seconds=2)
        continue_publish.set()
        with pytest.raises(PluginPermissionError, match="expired"):
            await publishing
    finally:
        continue_publish.set()
        await asyncio.gather(publishing, return_exceptions=True)
    assert await _state(database) == ((), (), ())


@pytest.mark.parametrize("change", ["expiry", "hash", "removed"])
async def test_concurrent_original_publication_reads_winner_before_current_media(
    database, monkeypatch, change
):
    repository = await _install(database)
    await _add_artifact(database, handle_id="original-media", digest="a" * 64)
    request = _request(media_handles=("original-media",))
    prepared, continue_publish = asyncio.Event(), asyncio.Event()
    original_writer = database.immediate_session
    delayed = None

    @asynccontextmanager
    async def delay_one_publisher():
        if asyncio.current_task() is delayed:
            prepared.set()
            await continue_publish.wait()
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", delay_one_publisher)
    delayed = asyncio.create_task(repository.publish(plugin_id=_PLUGIN_ID, request=request))
    try:
        async with asyncio.timeout(5):
            await prepared.wait()
        original = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
        before = await _state(database)
        async with original_writer() as writer:
            row = await writer.get(PluginMediaArtifactModel, "original-media")
            if change == "removed":
                await writer.delete(row)
            elif change == "expiry":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                row.sha256 = "b" * 64
        continue_publish.set()
        repeated = await delayed
    finally:
        continue_publish.set()
        await asyncio.gather(delayed, return_exceptions=True)
    assert repeated.notification_id == original.notification_id
    assert repeated.source_event_id == original.source_event_id
    assert repeated.deduplicated and not repeated.event_created
    assert not repeated.delivery_enqueued and not repeated.agent_turn_enqueued
    assert await _state(database) == before


async def test_cleanup_keeps_media_accepted_by_the_original_inflight_publication(
    database, tmp_path, monkeypatch
):
    repository = await _install(database)
    clock = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(notification_repository_module, "datetime", Clock)
    monkeypatch.setattr(media_artifacts_module, "datetime", Clock)
    store = PluginMediaArtifactStore(database, tmp_path / "plugin-originals")
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(image, "PNG")
    handle = await store.create(
        plugin_id=_PLUGIN_ID,
        data=image.getvalue(),
        content_type="image/png",
        filename="original.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    original = await store.resolve(plugin_id=_PLUGIN_ID, handle_id=handle.handle_id)
    checked, continue_publish, cleanup_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resolve_media = notification_repository_module._resolve_request_media
    original_writer = database.immediate_session
    calls = 0
    cleanup_task = None

    async def validated(*args, **kwargs):
        nonlocal calls
        result = await resolve_media(*args, **kwargs)
        calls += 1
        if calls == 2:
            checked.set()
            await continue_publish.wait()
        return result

    @asynccontextmanager
    async def observe_writer():
        if asyncio.current_task() is cleanup_task:
            cleanup_waiting.set()
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(notification_repository_module, "_resolve_request_media", validated)
    monkeypatch.setattr(database, "immediate_session", observe_writer)
    publishing = asyncio.create_task(
        repository.publish(
            plugin_id=_PLUGIN_ID, request=_request(media_handles=(handle.handle_id,))
        )
    )
    try:
        async with asyncio.timeout(5):
            await checked.wait()
            clock += timedelta(seconds=61)
            cleanup_task = asyncio.create_task(store.cleanup())
            await cleanup_waiting.wait()
            continue_publish.set()
            receipt = await publishing
            assert await cleanup_task == 0
        assert receipt.event_created and receipt.delivery_enqueued
        assert original.local_path.read_bytes() == image.getvalue()
        async with database.read_session() as reader:
            assert await reader.get(PluginMediaArtifactModel, handle.handle_id) is not None
    finally:
        continue_publish.set()
        await asyncio.gather(
            publishing, *([cleanup_task] if cleanup_task else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_exact_retry_is_read_only_and_deduplicated(database: Database) -> None:
    repository = await _install(database)
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=_request())
    assert first.event_created is True
    assert first.delivery_enqueued is True
    assert first.agent_turn_enqueued is True
    assert first.deduplicated is False
    before = await _state(database)
    second = await repository.publish(plugin_id=_PLUGIN_ID, request=_request())
    assert second.event_created is False
    assert second.delivery_enqueued is False
    assert second.agent_turn_enqueued is False
    assert second.deduplicated is True
    assert second.notification_id == first.notification_id
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_canonical_payload_key_order_retries_without_new_work(database: Database) -> None:
    repository = await _install(database)
    first = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(payload={"b": 1, "a": 2}),
    )
    before = await _state(database)
    second = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(payload={"a": 2, "b": 1}),
    )
    assert second.deduplicated is True
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"external_source": "other-source"},
        {"event_type": "fixture.other"},
        {"occurred_at": _NOW + timedelta(seconds=1)},
        {"payload": {"id": 8, "name": "alpha"}},
        {"summary": "other summary"},
        {"text": "other text"},
        {"ask_agent": False, "agent_intent": ""},
        {"agent_intent": "other intent"},
    ],
    ids=(
        "external_source",
        "event_type",
        "occurred_at",
        "payload",
        "summary",
        "text",
        "ask_agent",
        "agent_intent",
    ),
)
async def test_manifest_field_mismatch_is_receipt_conflict(
    database: Database,
    update: dict[str, Any],
) -> None:
    repository = await _install(database)
    await repository.publish(plugin_id=_PLUGIN_ID, request=_request())
    before = await _state(database)
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(plugin_id=_PLUGIN_ID, request=_request(**update))
    assert exc.value.category == "receipt_conflict"
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_missing_children_are_not_backfilled_on_retry(database: Database) -> None:
    repository = await _install(database)
    digest = _sha(b"card-bytes")
    await _add_artifact(database, handle_id="handle-silent", digest=digest)
    first = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(text="", media_handles=(), ask_agent=False, agent_intent=""),
    )
    assert first.delivery_enqueued is False
    assert first.agent_turn_enqueued is False
    before = await _state(database)
    assert before[1] == ()
    assert before[2] == ()
    with pytest.raises(CanonicalIdentityError) as text_exc:
        await repository.publish(plugin_id=_PLUGIN_ID, request=_request(media_handles=()))
    assert text_exc.value.category == "receipt_conflict"
    with pytest.raises(CanonicalIdentityError) as media_exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(
                text="",
                ask_agent=False,
                agent_intent="",
                media_handles=("handle-silent",),
            ),
        )
    assert media_exc.value.category == "receipt_conflict"
    with pytest.raises(CanonicalIdentityError) as job_exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(text="", media_handles=(), ask_agent=True),
        )
    assert job_exc.value.category == "receipt_conflict"
    assert await _state(database) == before
    assert await _count(database, PluginNotificationOutboxModel) == 0
    assert await _count(database, PluginBackgroundTurnJobModel) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ("text", "media", "job"))
async def test_original_manifest_survives_deleted_child_without_backfill(
    database: Database,
    child_kind: str,
) -> None:
    repository = await _install(database)
    digest = _sha(b"requested-media")
    await _add_artifact(database, handle_id="requested-handle", digest=digest)
    request = _request(media_handles=("requested-handle",))
    await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as session:
        if child_kind == "job":
            child = await session.scalar(select(PluginBackgroundTurnJobModel))
        else:
            child = await session.scalar(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.part_type == child_kind
                )
            )
        assert child is not None
        await session.delete(child)
    before = await _state(database)
    replay = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert replay.deduplicated and not replay.delivery_enqueued and not replay.agent_turn_enqueued
    assert await _state(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ("text", "media", "job"))
async def test_original_manifest_ignores_mutable_child_target(
    database: Database,
    child_kind: str,
) -> None:
    repository = await _install(database)
    digest = _sha(b"provenance-media")
    await _add_artifact(database, handle_id="provenance-handle", digest=digest)
    request = _request(media_handles=("provenance-handle",))
    await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as session:
        if child_kind == "job":
            child = await session.scalar(select(PluginBackgroundTurnJobModel))
        else:
            child = await session.scalar(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.part_type == child_kind
                )
            )
        assert child is not None
        child.target_id = "corrupted-target"
    before = await _state(database)
    replay = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert replay.deduplicated and not replay.delivery_enqueued and not replay.agent_turn_enqueued
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_new_media_part_key_uses_index_and_sha(database: Database) -> None:
    repository = await _install(database)
    digest = _sha(b"same-bytes")
    await _add_artifact(database, handle_id="handle-new", digest=digest)
    receipt = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("handle-new",)),
    )
    parts = await _media_parts(database)
    assert len(parts) == 1
    assert parts[0][0] == media_part_key(index=0, sha256=digest)
    assert parts[0][1] == "handle-new"
    assert "handle-new" not in parts[0][0]
    assert receipt.delivery_enqueued is True


@pytest.mark.asyncio
async def test_same_handle_and_sha_retry_does_not_enqueue_again(database: Database) -> None:
    repository = await _install(database)
    digest = _sha(b"same-bytes")
    await _add_artifact(database, handle_id="handle-same", digest=digest)
    request = _request(media_handles=("handle-same",))
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    before = await _state(database)
    second = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert second.deduplicated is True
    assert second.delivery_enqueued is False
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before


@pytest.mark.parametrize("legacy", [False, True])
async def test_original_publication_receipt_ignores_media_expiry(database, legacy):
    repository = await _install(database)
    digest = _sha(b"original-receipt")
    await _add_artifact(database, handle_id="original-expiring", digest=digest)
    request = _request(media_handles=("original-expiring",))
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as session:
        artifact = await session.get(PluginMediaArtifactModel, "original-expiring")
        artifact.expires_at = datetime.now(UTC) - timedelta(days=1)
        if legacy:
            part = await session.scalar(
                select(PluginNotificationOutboxModel).where(
                    PluginNotificationOutboxModel.part_type == "media"
                )
            )
            part.part_key = "media:0:original-expiring"
    before = await _state(database)
    replay = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert replay.deduplicated and replay.source_event_id == first.source_event_id
    assert replay.notification_id == first.notification_id
    assert (
        not replay.event_created and not replay.delivery_enqueued and not replay.agent_turn_enqueued
    )
    with pytest.raises(CanonicalIdentityError) as conflict:
        await repository.publish(
            plugin_id=_PLUGIN_ID, request=request.model_copy(update={"text": "changed"})
        )
    assert conflict.value.category == "receipt_conflict"
    with pytest.raises(PluginPermissionError, match="media handle"):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=request.model_copy(update={"event_key": "new-publication"}),
        )
    assert await _state(database) == before


@pytest.mark.parametrize(
    "status", ["pending", "processing", "sent", "failed", "uncertain", "cancelled"]
)
@pytest.mark.parametrize("legacy", [False, True])
async def test_original_receipt_survives_actual_media_lifecycle_cleanup(
    database, tmp_path, status, legacy
):
    repository = await _install(database)
    store = PluginMediaArtifactStore(database, tmp_path / "plugin-media")
    handle = await store.create(
        plugin_id=_PLUGIN_ID,
        data=b"\x89PNG\r\n\x1a\noriginal media",
        content_type="image/png",
        filename="original.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    request = _request(media_handles=(handle.handle_id,))
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as writer:
        artifact = await writer.get(PluginMediaArtifactModel, handle.handle_id)
        path = Path(artifact.storage_path)
        artifact.expires_at = datetime.now(UTC) - timedelta(days=1)
        part = await writer.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        part.status = status
        if legacy:
            part.part_key = f"media:0:{handle.handle_id}"
    assert await asyncio.to_thread(path.is_file)
    pending = status in {"pending", "processing"}
    assert await store.cleanup() == (0 if pending else 1)
    assert await asyncio.to_thread(path.exists) == pending
    async with database.read_session() as reader:
        assert (await reader.get(PluginMediaArtifactModel, handle.handle_id) is not None) == pending
        retained = await reader.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert retained.media_handle_id == handle.handle_id and retained.status == status
    before = await _state(database)
    replay = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert (
        replay.notification_id == first.notification_id
        and replay.source_event_id == first.source_event_id
    )
    assert (
        replay.deduplicated
        and not replay.event_created
        and not replay.delivery_enqueued
        and not replay.agent_turn_enqueued
    )
    assert await _state(database) == before
    with pytest.raises(CanonicalIdentityError) as conflict:
        await repository.publish(
            plugin_id=_PLUGIN_ID, request=request.model_copy(update={"text": "changed"})
        )
    assert conflict.value.category == "receipt_conflict"
    with pytest.raises(PluginPermissionError, match="original publication proof"):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=request.model_copy(update={"media_handles": ("unknown-handle",)}),
        )
    with pytest.raises(PluginPermissionError, match="media handle"):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=request.model_copy(update={"event_key": "independent-call"}),
        )
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_same_bytes_different_handle_dedupes_without_new_work(database: Database) -> None:
    repository = await _install(database)
    digest = _sha(b"same-bytes")
    await _add_artifact(database, handle_id="handle-one", digest=digest)
    await _add_artifact(database, handle_id="handle-two", digest=digest)
    first = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("handle-one",)),
    )
    before = await _state(database)
    second = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("handle-two",)),
    )
    assert second.deduplicated is True
    assert second.delivery_enqueued is False
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before
    parts = await _media_parts(database)
    assert parts == [(media_part_key(index=0, sha256=digest), "handle-one")]


@pytest.mark.asyncio
async def test_sha_key_survives_artifact_cleanup_and_dedupes_new_handle(
    database: Database,
) -> None:
    repository = await _install(database)
    digest = _sha(b"same-bytes")
    await _add_artifact(database, handle_id="cleaned-handle", digest=digest)
    await _add_artifact(database, handle_id="replacement-handle", digest=digest)
    request = _request(media_handles=("cleaned-handle",))
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as session:
        artifact = await session.get(PluginMediaArtifactModel, "cleaned-handle")
        assert artifact is not None
        await session.delete(artifact)
    async with database.read_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert row is not None
        assert row.media_handle_id == "cleaned-handle"
    before = await _state(database)
    second = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("replacement-handle",)),
    )
    assert second.deduplicated is True
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_different_sha_at_same_index_conflicts(database: Database) -> None:
    repository = await _install(database)
    await _add_artifact(database, handle_id="media-a", digest=_sha(b"bytes-a"))
    await _add_artifact(database, handle_id="media-b", digest=_sha(b"bytes-b"))
    await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("media-a",)),
    )
    before = await _state(database)
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("media-b",)),
        )
    assert exc.value.category == "receipt_conflict"
    assert await _state(database) == before
    assert await _media_parts(database) == [
        (media_part_key(index=0, sha256=_sha(b"bytes-a")), "media-a")
    ]


@pytest.mark.asyncio
async def test_media_count_and_order_mismatches_conflict(database: Database) -> None:
    repository = await _install(database)
    digest_a = _sha(b"bytes-a")
    digest_b = _sha(b"bytes-b")
    await _add_artifact(database, handle_id="media-a", digest=digest_a)
    await _add_artifact(database, handle_id="media-b", digest=digest_b)
    await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("media-a", "media-b")),
    )
    before = await _state(database)
    with pytest.raises(CanonicalIdentityError) as sha_exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("media-b", "media-a")),
        )
    assert sha_exc.value.category == "receipt_conflict"
    with pytest.raises(CanonicalIdentityError) as count_exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("media-a",)),
        )
    assert count_exc.value.category == "receipt_conflict"
    with pytest.raises(CanonicalIdentityError) as single_sha_exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("media-b", "media-b")),
        )
    assert single_sha_exc.value.category == "receipt_conflict"
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_legacy_handle_key_is_accepted_only_when_artifact_sha_matches(
    database: Database,
) -> None:
    repository = await _install(database)
    digest = _sha(b"legacy-bytes")
    other = _sha(b"other-bytes")
    await _add_artifact(database, handle_id="legacy-handle", digest=digest)
    await _add_artifact(database, handle_id="alias-handle", digest=digest)
    await _add_artifact(database, handle_id="conflict-handle", digest=other)
    first = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("legacy-handle",)),
    )
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert row is not None
        row.part_key = "media:0:legacy-handle"
    before = await _state(database)
    same_handle = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("legacy-handle",)),
    )
    assert same_handle.deduplicated is True
    assert same_handle.source_event_id == first.source_event_id
    alias = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("alias-handle",)),
    )
    assert alias.deduplicated is True
    assert await _state(database) == before
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("conflict-handle",)),
        )
    assert exc.value.category == "receipt_conflict"
    assert await _state(database) == before
    parts = await _media_parts(database)
    assert parts == [("media:0:legacy-handle", "legacy-handle")]


@pytest.mark.asyncio
async def test_invalid_media_rolls_back_first_publish(database: Database) -> None:
    repository = await _install(database)
    with pytest.raises(PluginPermissionError, match="media handle"):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("missing-handle",)),
        )
    assert await _state(database) == ((), (), ())
    assert await _count(database, ChatEventModel) == 0


def test_canonical_json_and_utc_tokens_are_stable() -> None:
    digest = _sha(b"bytes")
    naive = datetime(2026, 8, 26, 15, 0, 0)
    offset = datetime(2026, 8, 26, 23, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_occurred_at(naive) == canonical_occurred_at(_NOW)
    assert canonical_occurred_at(offset) == canonical_occurred_at(_NOW)
    key = media_part_key(index=0, sha256=digest)
    assert key == f"media:0:{digest}"
    assert is_sha256_digest(digest)


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"value": float("nan")})


@pytest.mark.asyncio
async def test_silent_agent_intent_is_rejected_before_any_write(database: Database) -> None:
    repository = await _install(database)
    with pytest.raises(ValueError, match="agent_intent requires ask_agent"):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(ask_agent=False, agent_intent="unused but identity-bearing"),
        )
    assert await _state(database) == ((), (), ())


@pytest.mark.asyncio
async def test_integrity_error_rolls_back_without_retry(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = await _install(database)
    calls = 0

    async def fail_outbox_once(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        raise IntegrityError("forced child failure", {}, RuntimeError("boom"))

    monkeypatch.setattr(
        notification_repository_module,
        "_ensure_outbox_part",
        fail_outbox_once,
    )
    with pytest.raises(IntegrityError):
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(ask_agent=False, agent_intent=""),
        )
    assert calls == 1
    assert await _state(database) == ((), (), ())


@pytest.mark.asyncio
async def test_equivalent_utc_occurred_at_deduplicates(database: Database) -> None:
    repository = await _install(database)
    first = await repository.publish(plugin_id=_PLUGIN_ID, request=_request())
    before = await _state(database)
    offset = datetime(2026, 8, 26, 23, tzinfo=timezone(timedelta(hours=8)))
    second = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(occurred_at=offset),
    )
    assert second.deduplicated is True
    assert second.source_event_id == first.source_event_id
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_original_manifest_does_not_require_outbox_artifact_proof(database: Database) -> None:
    repository = await _install(database)
    digest = _sha(b"legacy-bytes")
    await _add_artifact(database, handle_id="legacy-handle", digest=digest)
    await _add_artifact(database, handle_id="alias-handle", digest=digest)
    await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("legacy-handle",)),
    )
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert row is not None
        row.part_key = "media:0:legacy-handle"
        artifact = await session.get(PluginMediaArtifactModel, "legacy-handle")
        assert artifact is not None
        await session.delete(artifact)
    before = await _state(database)
    replay = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(media_handles=("alias-handle",)),
    )
    assert replay.deduplicated
    assert await _state(database) == before
    parts = await _media_parts(database)
    assert len(parts) == 1


@pytest.mark.asyncio
async def test_original_manifest_is_independent_of_mutable_outbox_handle(
    database: Database,
) -> None:
    repository = await _install(database)
    digest = _sha(b"legacy-bytes")
    await _add_artifact(database, handle_id="legacy-handle", digest=digest)
    await _add_artifact(database, handle_id="other-handle", digest=digest)
    request = _request(media_handles=("legacy-handle",))
    await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert row is not None
        row.part_key = "media:0:legacy-handle"
        row.media_handle_id = "other-handle"
    before = await _state(database)
    replay = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert replay.deduplicated
    assert await _state(database) == before


@pytest.mark.asyncio
async def test_unique_event_key_is_unchanged(database: Database) -> None:
    repository = await _install(database)
    first = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(event_key="shared-key"),
    )
    second = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(event_key="shared-key"),
    )
    group = _GROUP_TARGET
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=group,
        bot_user_id="8000",
        created_by_user_id=_TARGET.target_id,
        presence_id=_PRESENCE,
    )
    other_target = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(event_key="shared-key", target=group),
    )
    other_key = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=_request(event_key="other-key"),
    )
    event_ids, _parts, _intents = await _state(database)
    assert second.deduplicated is True
    assert second.source_event_id == first.source_event_id
    assert other_target.source_event_id != first.source_event_id
    assert other_key.source_event_id != first.source_event_id
    assert other_target.notification_id != first.notification_id
    assert other_key.notification_id != first.notification_id
    assert len(event_ids) == 3
    async with database.read_session() as session:
        first_row = await session.get(ChatEventModel, first.source_event_id)
        group_row = await session.get(ChatEventModel, other_target.source_event_id)
        other_row = await session.get(ChatEventModel, other_key.source_event_id)
        assert first_row is not None and group_row is not None and other_row is not None
        assert first_row.external_event_key == "shared-key"
        assert first_row.external_target_id == _TARGET.target_id
        assert first_row.scope_type == "private"
        assert group_row.external_event_key == "shared-key"
        assert group_row.external_target_id == _GROUP_TARGET.target_id
        assert group_row.scope_type == "group"
        assert other_row.external_event_key == "other-key"


@pytest.mark.asyncio
async def test_idle_notification_poll_does_not_reserve_writer(database: Database) -> None:
    repository = await _install(database)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await repository.claim_outbox() is None
        assert await repository.claim_turn() is None
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(
        sql.startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")) for sql in statements
    )


@pytest.mark.asyncio
async def test_expired_send_is_quarantined_and_late_original_receipt_is_retained(
    database: Database,
) -> None:
    repository = await _install(database)
    await repository.publish(
        plugin_id=_PLUGIN_ID, request=_request(ask_agent=False, agent_intent="")
    )
    await _prepare_text_delivery(repository)
    claimed = await repository.claim_outbox()
    assert claimed is not None
    async with database.immediate_session() as session:
        row = await session.get(PluginNotificationOutboxModel, claimed.id)
        assert row is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert await repository.claim_outbox() is None
    assert not await repository.retry_outbox(
        claimed.id,
        attempt=claimed.attempts,
        error_category="gateway_disconnected",
    )
    assert not await repository.finish_outbox(
        claimed.id,
        attempt=claimed.attempts + 1,
        status="sent",
        platform_message_id="wrong",
    )
    assert await repository.finish_outbox(
        claimed.id,
        attempt=claimed.attempts,
        status="uncertain",
        platform_message_id="accepted",
        error_category="post_send_persistence_failed",
    )
    assert not await repository.finish_outbox(
        claimed.id,
        attempt=claimed.attempts,
        status="sent",
        platform_message_id="conflicting",
    )
    assert await repository.finish_outbox(
        claimed.id,
        attempt=claimed.attempts,
        status="sent",
        platform_message_id="accepted",
    )
    assert await repository.claim_outbox() is None
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, claimed.id)
        assert row is not None
        assert (row.status, row.platform_message_id, row.attempts) == (
            "sent",
            "accepted",
            claimed.attempts,
        )


@pytest.mark.asyncio
async def test_only_proven_presend_failures_can_retry(database: Database) -> None:
    repository = await _install(database)
    await repository.publish(
        plugin_id=_PLUGIN_ID, request=_request(ask_agent=False, agent_intent="")
    )
    await _prepare_text_delivery(repository)
    claimed = await repository.claim_outbox()
    assert claimed is not None
    assert not await repository.retry_outbox(
        claimed.id,
        attempt=claimed.attempts,
        error_category="RuntimeError",
    )
    assert await repository.retry_outbox(
        claimed.id,
        attempt=claimed.attempts,
        error_category="bot_unavailable",
    )


async def test_original_raw_target_publication_returns_same_receipt_after_canonical_migration(
    database,
):
    repository = await _install(database)
    request = _request()
    receipt = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    # This is the persisted pre-canonical target representation. It retains its
    # real event/children/receipt; a selector migration cannot buy another send.
    original_id = notification_repository_module._notification_id(
        _PLUGIN_ID, request.event_key, "private", "1001"
    )
    async with database.immediate_session() as session:
        stored = await session.get(ChatEventModel, receipt.source_event_id)
        stored.external_target_id = "1001"
        stored.platform_message_id = "external-" + original_id
        import json

        segments = json.loads(stored.segments_json)
        data = next(part["data"] for part in segments if part["type"] == "yuki_context")
        data["notification_id"] = original_id
        stored.segments_json = json.dumps(segments)
        for model in (PluginNotificationOutboxModel, PluginBackgroundTurnJobModel):
            rows = await session.scalars(select(model).where(model.source_event_id == stored.id))
            for row in rows:
                row.target_id = "1001"
                if isinstance(row, PluginNotificationOutboxModel):
                    row.notification_id = original_id
    before = await _state(database)
    retry = await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert retry.deduplicated and retry.source_event_id == receipt.source_event_id
    assert retry.notification_id == original_id
    assert not retry.event_created and not retry.delivery_enqueued and not retry.agent_turn_enqueued
    assert await _state(database) == before
    with pytest.raises(CanonicalIdentityError) as conflict:
        await repository.publish(plugin_id=_PLUGIN_ID, request=_request(text="different body"))
    assert conflict.value.category == "receipt_conflict"
    assert await _state(database) == before
