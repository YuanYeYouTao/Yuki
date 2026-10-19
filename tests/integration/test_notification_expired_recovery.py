"""The Host returns an accepted publication after its original media deadline."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from tests.unit.test_migration_0126 import NOW, _old_materials, seed_original

from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host import notification_repository as publication
from yuki.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


@pytest.mark.parametrize("activation", [False, True])
async def test_migrated_attempting_delivery_reads_original_receipt_after_expiry(
    tmp_path, monkeypatch, activation
):
    path = tmp_path / "original-notification.db"

    def upgrade():
        seed_original(path, monkeypatch, _old_materials())
        command.upgrade(Config("alembic.ini"), "head")

    await asyncio.to_thread(upgrade)
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await replay_expired(database, monkeypatch, activation)
    finally:
        await database.close()


async def replay_expired(database, monkeypatch, activation):
    tables = (
        ChatEventModel.__table__,
        PluginNotificationOutboxModel.__table__,
        PluginBackgroundTurnJobModel.__table__,
    )
    async with database.sessions() as reader:
        row = await reader.scalar(
            select(PluginStateModel).where(PluginStateModel.plugin_id == "github-monitor")
        )
        queue = json.loads(row.value_json)
        original = {
            table.name: [dict(item) for item in (await reader.execute(select(table))).mappings()]
            for table in tables
        }
        expires = tuple(await reader.scalars(select(PluginMediaArtifactModel.expires_at)))
    delivery = queue["activation" if activation else "inflight"]["deliveries"][0]
    prepared = delivery["prepared"]
    assert delivery["status"] == "attempting" and prepared["media"]

    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            value = NOW + timedelta(days=8)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    assert expires and all(value < AfterDeadline.now().replace(tzinfo=None) for value in expires)
    monkeypatch.setattr(publication, "datetime", AfterDeadline)

    async def media_must_not_be_resolved(*args, **kwargs):
        raise AssertionError("an accepted publication must not reread expired media")

    monkeypatch.setattr(publication, "_resolve_request_media", media_must_not_be_resolved)
    request = PublishNotificationRequest(
        event_key=prepared["event_key"],
        event_type=prepared["event_type"],
        external_source=prepared["external_source"],
        target=NotificationTarget(
            target_type=prepared["target_type"],
            target_id=prepared["target_id"],
            conversation_id=prepared["conversation_id"],
        ),
        occurred_at=datetime.fromisoformat(prepared["occurred_at"]),
        summary=prepared["summary"],
        payload=json.loads(prepared["payload_json"]),
        text=prepared["text"],
        media_handles=tuple(item["handle_id"] for item in prepared["media"]),
        ask_agent=prepared["ask_agent"],
        agent_intent=prepared["agent_intent"],
    )
    receipt = await PluginNotificationRepository(database).publish(
        plugin_id="github-monitor", request=request
    )
    assert receipt.deduplicated and not receipt.event_created
    assert receipt.notification_id == delivery["notification_id"]
    assert receipt.source_event_id == delivery["source_event_id"]
    async with database.sessions() as reader:
        for table in tables:
            assert [dict(item) for item in (await reader.execute(select(table))).mappings()] == (
                original[table.name]
            )
