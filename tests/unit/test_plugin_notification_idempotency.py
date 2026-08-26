"""Whole-request publication idempotency for PluginNotificationRepository."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host import notification_repository as notification_repository_module
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.notification_manifest import (
    canonical_json,
    canonical_occurred_at,
    is_sha256_digest,
    legacy_media_part_key,
    media_part_key,
    parse_media_part_key,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)
_PLUGIN_ID = "test.publication-idempotency"
_TARGET = NotificationTarget(target_type="private", target_id="1001")


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


async def _install(database: Database) -> PluginNotificationRepository:
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
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=_TARGET,
        bot_user_id="8000",
        created_by_user_id="1001",
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
    async with database.sessions() as session:
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
    async with database.sessions() as session:
        return [
            (row.part_key, row.media_handle_id)
            for row in (
                await session.scalars(
                    select(PluginNotificationOutboxModel).order_by(PluginNotificationOutboxModel.id)
                )
            ).all()
            if row.part_type == "media"
        ]


async def _count(database: Database, model: type[Any]) -> int:
    async with database.sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


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
async def test_deleted_requested_child_conflicts_without_backfill(
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
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert exc.value.category == "receipt_conflict"
    assert await _state(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ("text", "media", "job"))
async def test_child_target_provenance_mismatch_fails_closed(
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
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert exc.value.category == "receipt_conflict"
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
    async with database.sessions() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_type == "media"
            )
        )
        assert row is not None
        assert row.media_handle_id is None
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
        row.part_key = legacy_media_part_key(index=0, handle_id="legacy-handle")
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
    assert parts == [(legacy_media_part_key(index=0, handle_id="legacy-handle"), "legacy-handle")]


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
    assert parse_media_part_key(key) == (0, digest)
    assert is_sha256_digest(digest)
    assert parse_media_part_key(legacy_media_part_key(index=0, handle_id="h1")) == (0, "h1")


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
async def test_legacy_key_without_artifact_proof_fails_closed(database: Database) -> None:
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
        row.part_key = legacy_media_part_key(index=0, handle_id="legacy-handle")
        artifact = await session.get(PluginMediaArtifactModel, "legacy-handle")
        assert artifact is not None
        await session.delete(artifact)
    before = await _state(database)
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(
            plugin_id=_PLUGIN_ID,
            request=_request(media_handles=("alias-handle",)),
        )
    assert exc.value.category == "receipt_conflict"
    assert await _state(database) == before
    parts = await _media_parts(database)
    assert len(parts) == 1


@pytest.mark.asyncio
async def test_legacy_key_must_match_the_outbox_media_handle(database: Database) -> None:
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
        row.part_key = legacy_media_part_key(index=0, handle_id="legacy-handle")
        row.media_handle_id = "other-handle"
    before = await _state(database)
    with pytest.raises(CanonicalIdentityError) as exc:
        await repository.publish(plugin_id=_PLUGIN_ID, request=request)
    assert exc.value.category == "receipt_conflict"
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
    group = NotificationTarget(target_type="group", target_id="2001")
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=group,
        bot_user_id="8000",
        created_by_user_id="1001",
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
    assert PLUGIN_API_VERSION == "2.0"
    assert second.deduplicated is True
    assert second.source_event_id == first.source_event_id
    assert other_target.source_event_id != first.source_event_id
    assert other_key.source_event_id != first.source_event_id
    assert other_target.notification_id != first.notification_id
    assert other_key.notification_id != first.notification_id
    assert len(event_ids) == 3
    async with database.sessions() as session:
        first_row = await session.get(ChatEventModel, first.source_event_id)
        group_row = await session.get(ChatEventModel, other_target.source_event_id)
        other_row = await session.get(ChatEventModel, other_key.source_event_id)
        assert first_row is not None and group_row is not None and other_row is not None
        assert first_row.external_event_key == "shared-key"
        assert first_row.external_target_id == "1001"
        assert first_row.scope_type == "private"
        assert group_row.external_event_key == "shared-key"
        assert group_row.external_target_id == "2001"
        assert group_row.scope_type == "group"
        assert other_row.external_event_key == "other-key"
