"""Monitor target upgrades retain queued bytes and original Host publication receipts."""

import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update

from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import (
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host import notification_repository as publications
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginConfigValueModel,
    PluginInstallationModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def _modules(monkeypatch):
    root = Path(__file__).parents[2]
    for name in ("github-monitor", "subscription-monitor"):
        monkeypatch.syspath_prepend(str(root / "plugins" / name))
    modules = SimpleNamespace(
        github=importlib.import_module("github_monitor.models"),
        github_config=importlib.import_module("github_monitor.config"),
        polling=importlib.import_module("github_monitor.polling"),
        feed=importlib.import_module("subscription_monitor.polling"),
        feed_config=importlib.import_module("subscription_monitor.config"),
    )
    path = root / "migrations" / "versions" / "0126_monitor_canonical_targets.py"
    spec = importlib.util.spec_from_file_location("monitor_target_upgrade", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return modules, migration


async def _setup(database):
    async with database.immediate_session() as writer:
        person = await find_identity_binding(writer, "1001")
        presence = await find_presence(writer, "8000")
        space = await find_space_binding(writer, "2001")
        private = await ensure_canonical_conversation(
            writer,
            kind="private",
            person_id=person.person_id,
            identity_binding_id=person.id,
            presence_id=presence.id,
            primary_scope_key="bot:8000:private:1001",
        )
        group = await ensure_canonical_conversation(
            writer,
            kind="space",
            space_id=space.space_id,
            space_binding_id=space.id,
            primary_scope_key="bot:8000:group:2001",
        )
        writer.add_all(
            [
                PluginInstallationModel(
                    plugin_id=name,
                    name=name,
                    version="1.0",
                    plugin_api=PLUGIN_API_VERSION,
                    yuki_requires=">=3.9",
                    manifest_hash="original-manifest",
                    entrypoint="fixture:plugin",
                    status="running",
                    enabled=True,
                    approved_permissions_json="[]",
                    requested_permissions_json="[]",
                    failure_count=0,
                    discovered_at=NOW,
                    updated_at=NOW,
                )
                for name in ("github-monitor", "subscription-monitor")
            ]
        )
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                PluginMediaArtifactModel(
                    plugin_id="github-monitor",
                    handle_id=f"frozen-media-{index}",
                    content_type="image/png",
                    filename=f"frozen-{index}.png",
                    byte_size=8,
                    sha256=digest * 64,
                    storage_path=f"/tmp/frozen-{index}.png",
                    created_at=NOW,
                    expires_at=NOW + timedelta(days=7),
                )
                for index, digest in enumerate(("a", "b"))
            ]
        )
    repository = PluginNotificationRepository(database)
    targets = {
        "group": NotificationTarget(
            target_type="group", target_id=space.space_id, conversation_id=group.conversation_id
        ),
        "private": NotificationTarget(
            target_type="private",
            target_id=person.person_id,
            conversation_id=private.conversation_id,
        ),
    }
    for plugin, kind in (
        ("github-monitor", "group"),
        ("github-monitor", "private"),
        ("subscription-monitor", "private"),
    ):
        await repository.grant_target(
            plugin_id=plugin,
            target=targets[kind],
            bot_user_id="8000",
            created_by_user_id=person.person_id,
            presence_id=presence.id,
        )
    async with database.immediate_session() as writer:
        for row in await writer.scalars(select(PluginBackgroundTargetGrantModel)):
            row.target_id = "2001" if row.target_type == "group" else "1001"
    return repository, targets


def _old_materials(modules):
    model = modules.github
    prepared = model.build_prepared_notification(
        event_key="original-github-source",
        event_type="WatchEvent",
        target_type="group",
        target_id="2001",
        occurred_at=NOW,
        summary="original summary",
        payload={"target": {"target_id": "leave-payload-raw"}, "message": "原正文"},
        text="原消息正文",
        media=tuple(
            model.PreparedMedia(
                index=index,
                handle_id=f"frozen-media-{index}",
                sha256=digest * 64,
                expires_at=NOW + timedelta(days=7),
            )
            for index, digest in enumerate(("a", "b"))
        ),
        ask_agent=True,
        agent_intent="原判断意图",
    )
    policy = model.TargetPolicySnapshot(
        target_type="group", target_id="2001", send_text=True, send_card=False, ask_agent=True
    )

    def source(number, fingerprint):
        return model.QueuedSourceEvent(
            github_event_id=number,
            source_fingerprint=fingerprint * 64,
            source_created_at=NOW,
            normalized=model.NormalizedGitHubEvent(
                github_event_id=number,
                repository="owner/repo",
                event_type="WatchEvent",
                actor="original",
                created_at=NOW,
                summary="source",
                event_key=f"source:{number}",
                payload={"target": "leave-source-payload-raw"},
            ),
            target_snapshot=(policy,),
        )

    delivery = model.TargetDelivery(
        target_key="group:2001",
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=False,
        ask_agent=True,
        status="attempting",
        prepared=prepared,
    )
    queue = model.QueueState(
        accepted_cursor="102",
        accepted_fingerprint="c" * 64,
        committed_cursor="100",
        committed_fingerprint="a" * 64,
        committed_created_at=NOW,
        pending=(source("102", "c"),),
        inflight=model.DeliveryUnit(
            unit_id="original-unit",
            members=(source("101", "b"),),
            deliveries=(delivery,),
            sealed_at=NOW,
        ),
        activation=model.ActivationState(
            activation_id="original-activation", occurred_at=NOW, deliveries=(delivery,)
        ),
    ).model_dump(mode="json")
    github_config = [
        {
            "repository": "owner/repo",
            "targets": [
                {
                    "target_type": "group",
                    "target_id": "2001",
                    "send_text": True,
                    "send_card": False,
                    "ask_agent": True,
                }
            ],
        }
    ]
    feed_request = PublishNotificationRequest(
        event_key="original-feed-source",
        event_type="feed.entry",
        external_source="feed",
        target=NotificationTarget(target_type="private", target_id="1001"),
        occurred_at=NOW,
        summary="original feed summary",
        payload={"target_id": "leave-feed-payload-raw"},
        text="原订阅正文",
        ask_agent=True,
        agent_intent="原订阅判断",
    )
    feed = modules.feed.FeedState(
        url="https://example.invalid/feed",
        initialized=True,
        seen=("original-fingerprint",),
        unmarked=("pending-entry",),
        pending=(feed_request,),
        etag="original-etag",
        accepted=7,
        filtered=2,
    ).model_dump(mode="json")
    feed_config = [
        {
            "id": "original-feed",
            "url": "https://example.invalid/feed",
            "targets": [
                {"target_type": "private", "target_id": "1001"},
            ],
        }
    ]
    return queue, github_config, feed, feed_config


async def _store(database, queue, github_config, feed, feed_config):
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                PluginStateModel(
                    plugin_id=plugin,
                    namespace=namespace,
                    key="original-key",
                    value_json=json.dumps(value),
                    version=13,
                    updated_at=NOW,
                )
                for plugin, namespace, value in (
                    ("github-monitor", "github_monitor_queue_v1", queue),
                    ("subscription-monitor", "feed_state_v1", feed),
                )
            ]
        )
        writer.add_all(
            [
                PluginConfigValueModel(
                    plugin_id=plugin,
                    scope_type="global",
                    key=key,
                    value_json=json.dumps(value),
                    version=5,
                    updated_at=NOW,
                )
                for plugin, key, value in (
                    ("github-monitor", "repositories", github_config),
                    ("subscription-monitor", "subscriptions", feed_config),
                )
            ]
        )


async def _apply(database, migration):
    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(upgrade)


async def test_monitor_upgrade_parses_new_models_and_replays_original_publication(
    database, monkeypatch
):
    modules, migration = _modules(monkeypatch)
    repository, targets = await _setup(database)
    queue, github_config, feed, feed_config = _old_materials(modules)
    old = deepcopy(queue)
    prepared = modules.github.PreparedNotification.model_validate(
        queue["inflight"]["deliveries"][0]["prepared"]
    )
    request = modules.polling.GitHubPoller._publish_request(prepared).model_copy(
        update={"target": targets["group"]}
    )
    receipt = await repository.publish(plugin_id="github-monitor", request=request)
    old_id = publications._notification_id("github-monitor", request.event_key, "group", "2001")
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, receipt.source_event_id)
        event.external_target_id = "2001"
        event.platform_message_id = "external-" + old_id
        for model in (PluginNotificationOutboxModel, PluginBackgroundTurnJobModel):
            for row in await writer.scalars(select(model).where(model.source_event_id == event.id)):
                row.target_id = "2001"
                if isinstance(row, PluginNotificationOutboxModel):
                    row.notification_id = old_id
    for delivery in (queue["inflight"]["deliveries"][0], queue["activation"]["deliveries"][0]):
        delivery["notification_id"], delivery["source_event_id"] = old_id, receipt.source_event_id
    await _store(database, queue, github_config, feed, feed_config)
    async with database.sessions() as reader:
        events = list(await reader.scalars(select(ChatEventModel.id)))
        outbox = [
            (row.id, row.notification_id, row.source_event_id, row.status, row.target_id)
            for row in await reader.scalars(select(PluginNotificationOutboxModel))
        ]
        jobs = [
            (row.id, row.source_event_id, row.status, row.target_id)
            for row in await reader.scalars(select(PluginBackgroundTurnJobModel))
        ]
    await _apply(database, migration)
    async with database.sessions() as reader:
        states = {
            row.namespace: (json.loads(row.value_json), row.version)
            for row in await reader.scalars(select(PluginStateModel))
        }
        configs = {
            row.key: (json.loads(row.value_json), row.version)
            for row in await reader.scalars(select(PluginConfigValueModel))
        }
    converted = modules.github.QueueState.model_validate(states["github_monitor_queue_v1"][0])
    current = converted.inflight.deliveries[0]
    assert states["github_monitor_queue_v1"][1] == 13
    assert current.target_id == targets["group"].target_id
    assert current.conversation_id == targets["group"].conversation_id
    assert current.notification_id == old_id and current.source_event_id == receipt.source_event_id
    assert current.status == "attempting" and current.prepared.payload_json == prepared.payload_json
    assert (
        current.prepared.text == prepared.text and current.prepared.event_key == prepared.event_key
    )
    assert current.prepared.request_hash != prepared.request_hash
    assert current.prepared.media == prepared.media
    assert converted.pending[0].source_fingerprint == old["pending"][0]["source_fingerprint"]
    assert converted.accepted_cursor == "102" and converted.committed_cursor == "100"
    assert converted.pending[0].normalized.payload["target"] == "leave-source-payload-raw"
    assert converted.activation.deliveries[0].conversation_id == targets["group"].conversation_id
    modules.github_config.GitHubMonitorConfig(repositories=configs["repositories"][0])
    modules.feed_config.MonitorConfig(subscriptions=configs["subscriptions"][0])
    feed_state = modules.feed.FeedState.model_validate(states["feed_state_v1"][0])
    assert feed_state.pending[0].target == targets["private"]
    assert feed_state.pending[0].payload["target_id"] == "leave-feed-payload-raw"
    assert feed_state.pending[0].event_key == "original-feed-source" and feed_state.accepted == 7
    retry = await repository.publish(
        plugin_id="github-monitor",
        request=modules.polling.GitHubPoller._publish_request(current.prepared),
    )
    assert retry.notification_id == old_id and retry.source_event_id == receipt.source_event_id
    assert retry.deduplicated and not retry.event_created and not retry.delivery_enqueued
    async with database.sessions() as reader:
        assert list(await reader.scalars(select(ChatEventModel.id))) == events
        assert [
            (row.id, row.notification_id, row.source_event_id, row.status, row.target_id)
            for row in await reader.scalars(select(PluginNotificationOutboxModel))
        ] == outbox
        assert [
            (row.id, row.source_event_id, row.status, row.target_id)
            for row in await reader.scalars(select(PluginBackgroundTurnJobModel))
        ] == jobs
    with pytest.raises(CanonicalIdentityError) as conflict:
        await repository.publish(
            plugin_id="github-monitor", request=request.model_copy(update={"text": "changed"})
        )
    assert conflict.value.category == "receipt_conflict"


@pytest.mark.parametrize("reason", ["missing", "ambiguous", "hash_mismatch"])
async def test_unresolved_monitor_row_keeps_exact_original_json_and_upgrade_diagnostic(
    database, monkeypatch, caplog, reason
):
    modules, migration = _modules(monkeypatch)
    repository, _targets = await _setup(database)
    values = _old_materials(modules)
    queue, github_config, _feed, _feed_config = values
    if reason == "missing":
        queue["pending"][0]["target_snapshot"][0]["target_id"] = "no-original-grant"
        github_config[0]["targets"][0]["target_id"] = "no-original-grant"
    elif reason == "hash_mismatch":
        queue["inflight"]["deliveries"][0]["prepared"]["text"] = "changed after prepare"
    else:
        async with database.immediate_session() as writer:
            other = await find_space_binding(writer, "2002")
            conversation = await ensure_canonical_conversation(
                writer,
                kind="space",
                space_id=other.space_id,
                space_binding_id=other.id,
                primary_scope_key="bot:8000:group:2002",
            )
            creator = await find_identity_binding(writer, "1001")
            presence = await find_presence(writer, "8000")
        await repository.grant_target(
            plugin_id="github-monitor",
            target=NotificationTarget(
                target_type="group",
                target_id=other.space_id,
                conversation_id=conversation.conversation_id,
            ),
            bot_user_id="8000",
            created_by_user_id=creator.person_id,
            presence_id=presence.id,
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(PluginBackgroundTargetGrantModel)
                .where(
                    PluginBackgroundTargetGrantModel.plugin_id == "github-monitor",
                    PluginBackgroundTargetGrantModel.canonical_target_space_id == other.space_id,
                )
                .values(target_id="2001")
            )
    await _store(database, *values)
    async with database.sessions() as reader:
        original = (
            (
                await reader.scalars(
                    select(PluginStateModel).where(PluginStateModel.plugin_id == "github-monitor")
                )
            )
            .one()
            .value_json
        )
        original_config = (
            (
                await reader.scalars(
                    select(PluginConfigValueModel).where(
                        PluginConfigValueModel.plugin_id == "github-monitor"
                    )
                )
            )
            .one()
            .value_json
        )
    await _apply(database, migration)
    async with database.sessions() as reader:
        row = (
            await reader.scalars(
                select(PluginStateModel).where(PluginStateModel.plugin_id == "github-monitor")
            )
        ).one()
        assert row.value_json == original and row.version == 13
        if reason in {"missing", "ambiguous"}:
            config = (
                await reader.scalars(
                    select(PluginConfigValueModel).where(
                        PluginConfigValueModel.plugin_id == "github-monitor"
                    )
                )
            ).one()
            assert config.value_json == original_config and config.version == 5
        feed = (
            await reader.scalars(
                select(PluginStateModel).where(PluginStateModel.plugin_id == "subscription-monitor")
            )
        ).one()
        parsed_feed = modules.feed.FeedState.model_validate_json(feed.value_json)
        assert parsed_feed.pending[0].target == _targets["private"]
    assert "monitor_target_upgrade_unresolved" in caplog.text
    assert "原正文" not in caplog.text and "changed after prepare" not in caplog.text
