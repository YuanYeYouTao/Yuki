"""An accepted publication resumes from its receipt after the original media deadline."""

import asyncio
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.unit.test_migration_0126 import (
    NOW,
)
from tests.unit.test_migration_0126 import (
    test_monitor_upgrade_parses_new_models_and_replays_original_publication as seed_upgrade,
)

from yuki.persistence.models import ChatEventModel
from yuki.plugin_host import notification_repository as publication
from yuki.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository


@pytest.mark.parametrize("activation", [False, True])
async def test_migrated_attempting_delivery_reads_original_receipt_after_expiry(
    database, monkeypatch, activation
):
    await seed_upgrade(database, monkeypatch)
    model = importlib.import_module("github_monitor.models")
    polling = importlib.import_module("github_monitor.polling")
    state = importlib.import_module("github_monitor.state")
    async with database.sessions() as reader:
        row = await reader.scalar(
            select(PluginStateModel).where(PluginStateModel.plugin_id == "github-monitor")
        )
        queue = model.QueueState.model_validate_json(row.value_json)
        original_events = tuple(await reader.scalars(select(ChatEventModel.id)))
        original_parts = tuple(await reader.scalars(select(PluginNotificationOutboxModel.id)))
        original_jobs = tuple(await reader.scalars(select(PluginBackgroundTurnJobModel.id)))
    selected = queue.activation if activation else queue.inflight
    delivery = selected.deliveries[0]
    assert delivery.status == "attempting" and delivery.prepared.media

    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            value = NOW + timedelta(days=8)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(publication, "datetime", AfterDeadline)
    monkeypatch.setattr(polling, "datetime", AfterDeadline)
    repository = PluginNotificationRepository(database)

    async def publish(request):
        return await repository.publish(plugin_id="github-monitor", request=request)

    context = SimpleNamespace(notifications=SimpleNamespace(publish=AsyncMock(side_effect=publish)))
    poller = polling.GitHubPoller(context, asyncio.Event())
    snapshot = state.QueueSnapshot(raw=None, state=queue)
    replace = AsyncMock(return_value=snapshot)
    method = "_replace_activation_delivery" if activation else "_replace_unit_delivery"
    monkeypatch.setattr(poller, method, replace)
    drain = poller._retire_activation_target if activation else poller._drain_unit_target
    await drain(SimpleNamespace(repository="owner/repo"), snapshot, delivery.target_key)
    completed = replace.await_args.args[2]
    assert completed.status == "completed"
    assert completed.notification_id == delivery.notification_id
    assert completed.source_event_id == delivery.source_event_id
    context.notifications.publish.assert_awaited_once()
    async with database.sessions() as reader:
        assert tuple(await reader.scalars(select(ChatEventModel.id))) == original_events
        assert (
            tuple(await reader.scalars(select(PluginNotificationOutboxModel.id))) == original_parts
        )
        assert tuple(await reader.scalars(select(PluginBackgroundTurnJobModel.id))) == original_jobs
