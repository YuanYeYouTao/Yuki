"""Frozen monitor rows upgrade without using today's publisher on yesterday's schema."""

import importlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0110 import insert_row

from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def _modules(monkeypatch):
    root = Path(__file__).parents[2]
    for name in ("github-monitor", "subscription-monitor"):
        monkeypatch.syspath_prepend(str(root / "plugins" / name))
    return SimpleNamespace(
        github=importlib.import_module("github_monitor.models"),
        github_config=importlib.import_module("github_monitor.config"),
        feed=importlib.import_module("subscription_monitor.polling"),
        feed_config=importlib.import_module("subscription_monitor.config"),
    )


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


def seed_conversation(
    connection, person, presence, space, now, *, private, binding_id, space_binding_id, native
):
    conversation, alias = str(uuid4()), str(uuid4())
    insert_row(
        connection,
        "canonical_conversations",
        {
            "id": conversation,
            "kind": "private" if private else "space",
            "person_id": person if private else None,
            "space_id": None if private else space,
            "primary_alias_id": alias,
            "primary_marker": 1,
            "generation": 1,
            "starts_after_event_id": 0,
            "last_event_id": 1,
            "last_generation_change_event_id": 0,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "identity_binding_id": binding_id if private else None,
            "entry_presence_id": presence,
            "space_binding_id": None if private else space_binding_id,
        },
    )
    insert_row(
        connection,
        "conversation_legacy_aliases",
        {
            "id": alias,
            "conversation_id": conversation,
            "scope_key": f"bot:8000:{'private' if private else 'group'}:{native}",
            "is_primary": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    return conversation


def seed_original(path, monkeypatch, values, *, ambiguous=False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    command.upgrade(Config("alembic.ini"), "0124")
    now = NOW.strftime("%Y-%m-%d %H:%M:%S.%f")
    person, presence = str(uuid4()), str(uuid4())
    targets = {}
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO persons VALUES(?,1,1,?,?)", (person, now, now))
        connection.execute(
            "INSERT INTO presences VALUES(?,'qq','8000',1,1,1,?,?)", (presence, now, now)
        )
        binding = str(uuid4())
        insert_row(
            connection,
            "identity_bindings",
            {
                "id": binding,
                "person_id": person,
                "platform": "qq",
                "external_account_id": "1001",
                "display_name": "原人",
                "status": "active",
                "revision": 1,
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        space, space_binding = str(uuid4()), str(uuid4())
        connection.execute("INSERT INTO spaces VALUES(?,'原群',1,0,1,1,?,?)", (space, now, now))
        insert_row(
            connection,
            "space_bindings",
            {
                "id": space_binding,
                "space_id": space,
                "platform": "qq",
                "external_space_id": "2001",
                "display_name": "原群",
                "status": "active",
                "revision": 1,
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        for kind in ("private", "group"):
            conversation = seed_conversation(
                connection,
                person,
                presence,
                space,
                now,
                private=kind == "private",
                binding_id=binding,
                space_binding_id=space_binding,
                native="1001" if kind == "private" else "2001",
            )
            targets[kind] = NotificationTarget(
                target_type=kind,
                target_id=person if kind == "private" else space,
                conversation_id=conversation,
            )
        for plugin in ("github-monitor", "subscription-monitor"):
            insert_row(
                connection,
                "plugin_installations",
                {
                    "plugin_id": plugin,
                    "name": plugin,
                    "version": "original",
                    "plugin_api": "4.0",
                    "yuki_requires": ">=3.9",
                    "entrypoint": "fixture:plugin",
                    "manifest_hash": "original-manifest",
                    "status": "running",
                    "enabled": 1,
                    "approved_permissions_json": "[]",
                    "requested_permissions_json": "[]",
                    "failure_count": 0,
                    "discovered_at": now,
                    "updated_at": now,
                },
            )

        def grant(plugin, kind, target=None):
            target = target or targets[kind]
            insert_row(
                connection,
                "plugin_background_target_grants",
                {
                    "plugin_id": plugin,
                    "target_type": kind,
                    "target_id": "2001" if kind == "group" else "1001",
                    "bot_user_id": "8000",
                    "enabled": 1,
                    "created_by_user_id": "1001",
                    "created_at": now,
                    "updated_at": now,
                    "canonical_target_person_id": target.target_id if kind == "private" else None,
                    "canonical_target_space_id": target.target_id if kind == "group" else None,
                    "canonical_created_by_person_id": person,
                    "canonical_presence_id": presence,
                    "canonical_conversation_id": target.conversation_id,
                },
            )

        grant("github-monitor", "group")
        grant("github-monitor", "private")
        grant("subscription-monitor", "private")
        if ambiguous:
            other_space, other_binding = str(uuid4()), str(uuid4())
            connection.execute(
                "INSERT INTO spaces VALUES(?,'另一群',1,0,1,1,?,?)", (other_space, now, now)
            )
            insert_row(
                connection,
                "space_bindings",
                {
                    "id": other_binding,
                    "space_id": other_space,
                    "platform": "qq",
                    "external_space_id": "2002",
                    "display_name": "另一群",
                    "status": "active",
                    "revision": 1,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            other_conversation = seed_conversation(
                connection,
                person,
                presence,
                other_space,
                now,
                private=False,
                binding_id=binding,
                space_binding_id=other_binding,
                native="2002",
            )
            grant(
                "github-monitor",
                "group",
                NotificationTarget(
                    target_type="group", target_id=other_space, conversation_id=other_conversation
                ),
            )
        for index, digest in enumerate(("a", "b")):
            insert_row(
                connection,
                "plugin_media_artifacts",
                {
                    "plugin_id": "github-monitor",
                    "handle_id": f"frozen-media-{index}",
                    "content_type": "image/png",
                    "filename": f"frozen-{index}.png",
                    "byte_size": 8,
                    "sha256": digest * 64,
                    "storage_path": f"/original/frozen-{index}.png",
                    "created_at": now,
                    "expires_at": "2026-10-23 12:00:00.000000",
                },
            )
        insert_row(
            connection,
            "chat_events",
            {
                "id": 1,
                "bot_user_id": "8000",
                "platform_message_id": "external-original-notification",
                "scope_type": "group",
                "group_id": "2001",
                "sender_user_id": "8000",
                "direction": "external",
                "content": "original summary",
                "visual_summary": "",
                "segments_json": "[]",
                "origin": "plugin_background",
                "occurred_at": now,
                "observed_at": now,
                "canonical_event_id": str(uuid4()),
                "canonical_conversation_id": targets["group"].conversation_id,
                "author_kind": "system",
                "ingress_presence_id": presence,
                "event_kind": "external_event",
                "source_plugin_id": "github-monitor",
                "external_source": "github",
                "external_event_key": "original-github-source",
                "external_event_type": "WatchEvent",
                "external_payload_json": values[0]["inflight"]["deliveries"][0]["prepared"][
                    "payload_json"
                ],
                "external_target_id": "2001",
                "suppression_status": "keeper",
            },
        )
        common = {
            "source_event_id": 1,
            "plugin_id": "github-monitor",
            "target_type": "group",
            "target_id": "2001",
            "bot_user_id": "8000",
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
            "canonical_target_space_id": space,
            "canonical_conversation_id": targets["group"].conversation_id,
            "canonical_presence_id": presence,
        }
        insert_row(
            connection,
            "plugin_background_turn_jobs",
            {
                **common,
                "id": 1,
                "agent_intent": "原判断意图",
                "generated_text": "",
                "tool_calls_used": 0,
                "model_requests": 0,
            },
        )
        insert_row(
            connection,
            "plugin_notification_outbox",
            {
                **common,
                "id": 1,
                "notification_id": "original-notification",
                "part_key": "text",
                "part_type": "text",
                "text": "原消息正文",
                "status": "sent",
                "platform_message_id": "original-native-ack",
                "sent_at": now,
            },
        )
        for index in range(2):
            insert_row(
                connection,
                "plugin_notification_outbox",
                {
                    **common,
                    "id": index + 2,
                    "notification_id": "original-notification",
                    "part_key": f"media:{index}:frozen-media-{index}",
                    "part_type": "media",
                    "text": "",
                    "media_handle_id": f"frozen-media-{index}",
                },
            )
        queue, github_config, feed, feed_config = values
        for delivery in (queue["inflight"]["deliveries"][0], queue["activation"]["deliveries"][0]):
            delivery["notification_id"], delivery["source_event_id"] = "original-notification", 1
        for plugin, namespace, value in (
            ("github-monitor", "github_monitor_queue_v1", queue),
            ("subscription-monitor", "feed_state_v1", feed),
        ):
            insert_row(
                connection,
                "plugin_state",
                {
                    "plugin_id": plugin,
                    "namespace": namespace,
                    "key": "original-key",
                    "value_json": json.dumps(value),
                    "version": 13,
                    "updated_at": now,
                },
            )
        for plugin, key, value in (
            ("github-monitor", "repositories", github_config),
            ("subscription-monitor", "subscriptions", feed_config),
        ):
            insert_row(
                connection,
                "plugin_config_values",
                {
                    "plugin_id": plugin,
                    "scope_type": "global",
                    "key": key,
                    "value_json": json.dumps(value),
                    "version": 5,
                    "updated_at": now,
                },
            )
    return targets


def saved_rows(connection, table):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]


def state_values(connection):
    return {
        row["namespace"]: (json.loads(row["value_json"]), row["version"])
        for row in connection.execute("SELECT * FROM plugin_state")
    }


def test_monitor_upgrade_keeps_original_publication_and_delivery_evidence(tmp_path, monkeypatch):
    modules = _modules(monkeypatch)
    values = _old_materials(modules)
    queue = values[0]
    original = deepcopy(queue)
    prepared = modules.github.PreparedNotification.model_validate(
        queue["inflight"]["deliveries"][0]["prepared"]
    )
    path = tmp_path / "monitor.db"
    targets = seed_original(path, monkeypatch, values)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        evidence = {
            table: saved_rows(connection, table)
            for table in (
                "chat_events",
                "plugin_notification_outbox",
                "plugin_background_turn_jobs",
            )
        }
    command.upgrade(Config("alembic.ini"), "0126")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        states = state_values(connection)
        configs = {
            row["key"]: (json.loads(row["value_json"]), row["version"])
            for row in connection.execute("SELECT * FROM plugin_config_values")
        }
        for table, original_rows in evidence.items():
            assert saved_rows(connection, table) == original_rows
    converted = modules.github.QueueState.model_validate(states["github_monitor_queue_v1"][0])
    current = converted.inflight.deliveries[0]
    assert states["github_monitor_queue_v1"][1] == 13
    assert current.target_id == targets["group"].target_id
    assert current.conversation_id == targets["group"].conversation_id
    assert (current.notification_id, current.source_event_id) == ("original-notification", 1)
    assert current.status == "attempting" and current.prepared.payload_json == prepared.payload_json
    assert (
        current.prepared.text == prepared.text and current.prepared.event_key == prepared.event_key
    )
    assert (
        current.prepared.request_hash != prepared.request_hash
        and current.prepared.media == prepared.media
    )
    assert converted.pending[0].source_fingerprint == original["pending"][0]["source_fingerprint"]
    assert converted.accepted_cursor == "102" and converted.committed_cursor == "100"
    assert converted.pending[0].normalized.payload["target"] == "leave-source-payload-raw"
    assert converted.activation.deliveries[0].conversation_id == targets["group"].conversation_id
    modules.github_config.GitHubMonitorConfig(repositories=configs["repositories"][0])
    modules.feed_config.MonitorConfig(subscriptions=configs["subscriptions"][0])
    feed = modules.feed.FeedState.model_validate(states["feed_state_v1"][0])
    assert feed.pending[0].target == targets["private"]
    assert feed.pending[0].payload["target_id"] == "leave-feed-payload-raw"
    assert feed.pending[0].event_key == "original-feed-source" and feed.accepted == 7
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        # No new publisher/parser is used to replay a retired protocol.
        for table in ("plugin_notification_outbox", "plugin_background_turn_jobs"):
            for before, after in zip(evidence[table], saved_rows(connection, table), strict=True):
                assert {key: after[key] for key in before} == before
        after_event = saved_rows(connection, "chat_events")[0]
        for key in (
            "id",
            "platform_message_id",
            "content",
            "external_payload_json",
            "canonical_conversation_id",
            "ingress_presence_id",
        ):
            assert after_event[key] == evidence["chat_events"][0][key]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("reason", ["missing", "ambiguous", "hash_mismatch"])
def test_unresolved_monitor_row_keeps_exact_original_json_and_upgrade_diagnostic(
    tmp_path, monkeypatch, caplog, reason
):
    modules = _modules(monkeypatch)
    values = _old_materials(modules)
    queue, github_config, _feed, _feed_config = values
    if reason == "missing":
        queue["pending"][0]["target_snapshot"][0]["target_id"] = "no-original-grant"
        github_config[0]["targets"][0]["target_id"] = "no-original-grant"
    elif reason == "hash_mismatch":
        queue["inflight"]["deliveries"][0]["prepared"]["text"] = "changed after prepare"
    path = tmp_path / "unresolved-monitor.db"
    targets = seed_original(path, monkeypatch, values, ambiguous=reason == "ambiguous")
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT value_json FROM plugin_state WHERE plugin_id='github-monitor'"
        ).fetchone()[0]
        original_config = connection.execute(
            "SELECT value_json FROM plugin_config_values WHERE plugin_id='github-monitor'"
        ).fetchone()[0]
    command.upgrade(Config("alembic.ini"), "0126")
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT value_json,version FROM plugin_state WHERE plugin_id='github-monitor'"
        ).fetchone()
        assert row == (original, 13)
        if reason in {"missing", "ambiguous"}:
            assert connection.execute(
                "SELECT value_json,version FROM plugin_config_values "
                "WHERE plugin_id='github-monitor'"
            ).fetchone() == (original_config, 5)
        feed_json = connection.execute(
            "SELECT value_json FROM plugin_state WHERE plugin_id='subscription-monitor'"
        ).fetchone()[0]
        assert (
            modules.feed.FeedState.model_validate_json(feed_json).pending[0].target
            == targets["private"]
        )
    assert "monitor_target_upgrade_unresolved" in caplog.text
    assert "原正文" not in caplog.text and "changed after prepare" not in caplog.text
