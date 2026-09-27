"""Administrative reads never poll, seal, write or expose frozen notification bodies."""

import json
from datetime import UTC, datetime

import pytest
from github_monitor.models import (
    DeliveryUnit,
    QueuedSourceEvent,
    QueueState,
    TargetDelivery,
    build_prepared_notification,
)
from github_monitor.observation import observe_queue
from github_monitor.state import DIAGNOSTIC_NAMESPACE, QUEUE_NAMESPACE

from yuki_plugin_sdk.observation import PluginObservationContext, PluginObservationRequest
from yuki_plugin_sdk.testing import FakePluginContext


async def setup(repositories):
    fake = FakePluginContext(plugin_id="github-monitor")
    await fake.config.set(
        "repositories",
        [
            {"repository": repository, "targets": [{"target_type": "group", "target_id": "2001"}]}
            for repository in repositories
        ],
    )
    context = PluginObservationContext(fake.config.get, fake.storage.get)
    return fake, context


async def test_queue_projection_preserves_internal_event_identity_and_is_read_only():
    fake, context = await setup(["Owner/Repo"])
    now = datetime.now(UTC)
    source = QueuedSourceEvent(
        github_event_id="100", source_fingerprint="a" * 64, skip_reason="filtered"
    )
    prepared = build_prepared_notification(
        event_key="github:100",
        event_type="PushEvent",
        target_type="group",
        target_id="2001",
        occurred_at=now,
        summary="private-summary",
        payload={"private": "private-payload"},
        text="private-body",
    )
    delivery = TargetDelivery(
        target_key="group:2001",
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=False,
        ask_agent=False,
        status="completed",
        prepared=prepared,
        notification_id="original-notification",
        source_event_id=6912,
        completed_at=now,
    )
    state = QueueState(
        accepted_cursor="100",
        accepted_fingerprint="a" * 64,
        inflight=DeliveryUnit(
            unit_id="original-unit", members=(source,), deliveries=(delivery,), sealed_at=now
        ),
        last_poll_at=now,
        consecutive_failures=2,
        paused_until=now,
    )
    raw = state.model_dump(mode="json")
    await fake.storage.set(QUEUE_NAMESPACE, "owner/repo", raw)
    await fake.storage.set(
        DIAGNOSTIC_NAMESPACE, "owner/repo", {"category": "github_cursor_payload_conflict"}
    )
    result = await observe_queue(context, PluginObservationRequest())
    row = result["repositories"][0]
    assert row["accepted_cursor"] == "100" and row["committed_cursor"] == ""
    assert row["pending_count"] == 0 and row["consecutive_failures"] == 2
    assert row["error_category"] == "github_cursor_payload_conflict"
    projected = row["inflight"]["deliveries"][0]
    assert (
        projected["source_event_id"] == 6912
        and projected["notification_id"] == "original-notification"
    )
    assert "private" not in json.dumps(result) and "prepared" not in json.dumps(result)
    assert await fake.storage.get(QUEUE_NAMESPACE, "owner/repo") == raw


async def test_repository_pagination_missing_queue_and_malformed_state_are_explicit():
    fake, context = await setup(["Z/repo", "A/repo", "M/repo"])
    await fake.storage.set(QUEUE_NAMESPACE, "m/repo", {"invalid": "private-raw"})
    first = await observe_queue(context, PluginObservationRequest(limit=1))
    assert first["next_cursor"] == "a/repo"
    assert first["repositories"][0]["queue_available"] is False
    assert "pending_count" not in first["repositories"][0]
    second = await observe_queue(
        context, PluginObservationRequest(cursor=first["next_cursor"], limit=1)
    )
    assert second["repositories"][0]["state_error"] == "invalid_queue_state"
    assert "private-raw" not in json.dumps(second)
    with pytest.raises(ValueError, match="no longer configured"):
        await observe_queue(context, PluginObservationRequest(cursor="never/repo"))


async def test_pending_events_are_bounded_without_mutating_queue():
    fake, context = await setup(["Owner/Repo"])
    sources = tuple(
        QueuedSourceEvent(
            github_event_id=str(index), source_fingerprint="a" * 64, skip_reason="filtered"
        )
        for index in range(1, 16)
    )
    raw = QueueState(
        accepted_cursor="15", accepted_fingerprint="a" * 64, pending=sources
    ).model_dump(mode="json")
    await fake.storage.set(QUEUE_NAMESPACE, "owner/repo", raw)
    row = (await observe_queue(context, PluginObservationRequest()))["repositories"][0]
    assert row["pending_count"] == 15 and len(row["pending"]) == 10 and row["pending_has_more"]
    assert await fake.storage.get(QUEUE_NAMESPACE, "owner/repo") == raw
