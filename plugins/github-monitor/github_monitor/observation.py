"""Read-only, bounded views of the plugin's authoritative queue and diagnostics."""

from __future__ import annotations

from pydantic import ValidationError

from yuki_plugin_sdk.observation import (
    JsonObject,
    PluginObservationContext,
    PluginObservationRequest,
)

from .config import CONFIG_KEYS, GitHubMonitorConfig
from .models import QueueState
from .state import DIAGNOSTIC_NAMESPACE, QUEUE_NAMESPACE


async def observe_queue(
    context: PluginObservationContext, request: PluginObservationRequest
) -> JsonObject:
    values = {}
    for key in CONFIG_KEYS:
        value = await context.get_config(key)
        if value is not None:
            values[key] = value
    config = GitHubMonitorConfig.model_validate(values)
    repositories = sorted(config.repositories, key=lambda item: item.repository.casefold())
    if request.cursor is not None and request.cursor not in {
        item.repository.casefold() for item in repositories
    }:
        raise ValueError("observation cursor no longer configured")
    repositories = [
        item
        for item in repositories
        if request.cursor is None or item.repository.casefold() > request.cursor
    ]
    selected = repositories[: request.limit]
    rows = []
    for subscription in selected:
        repository = subscription.repository.casefold()
        raw = await context.get_state(QUEUE_NAMESPACE, repository)
        row = {
            "repository": subscription.repository,
            "enabled": subscription.enabled,
            "queue_available": raw is not None,
        }
        diagnostic = await context.get_state(DIAGNOSTIC_NAMESPACE, repository)
        if isinstance(diagnostic, dict):
            category = diagnostic.get("category")
            if (
                isinstance(category, str)
                and len(category) <= 64
                and all(char.isascii() and (char.isalnum() or char == "_") for char in category)
            ):
                row["error_category"] = category
        if raw is not None:
            try:
                state = QueueState.model_validate(raw)
                snapshot = state.model_dump(mode="json")
                row.update(
                    {
                        key: snapshot[key]
                        for key in (
                            "accepted_cursor",
                            "committed_cursor",
                            "last_poll_at",
                            "last_success_at",
                            "consecutive_failures",
                            "paused_until",
                            "rate_limit_remaining",
                            "rate_limit_reset_at",
                            "backlog_pending",
                            "gap_reason",
                            "gap_at",
                        )
                    }
                )
                row["pending_count"] = len(state.pending)
                row["pending"] = [
                    {
                        "github_event_id": event.github_event_id,
                        "source_created_at": event.source_created_at.isoformat()
                        if event.source_created_at
                        else None,
                        "event_type": event.normalized.event_type if event.normalized else None,
                        "skip_reason": event.skip_reason,
                        "target_count": len(event.target_snapshot),
                    }
                    for event in state.pending[:10]
                ]
                row["pending_has_more"] = len(state.pending) > 10
                unit = state.inflight
                row["inflight"] = (
                    None
                    if unit is None
                    else {
                        "unit_id": unit.unit_id,
                        "member_count": len(unit.members),
                        "delivery_count": len(unit.deliveries),
                        "sealed_at": unit.sealed_at.isoformat(),
                        "deliveries": [
                            {
                                "status": delivery.status,
                                "notification_id": delivery.notification_id,
                                "source_event_id": delivery.source_event_id,
                                "completed_at": delivery.completed_at.isoformat()
                                if delivery.completed_at
                                else None,
                                "skipped_reason": delivery.skipped_reason,
                            }
                            for delivery in unit.deliveries[:10]
                        ],
                        "deliveries_has_more": len(unit.deliveries) > 10,
                    }
                )
            except ValidationError:
                row["state_error"] = "invalid_queue_state"
        rows.append(row)
    return {
        "repositories": rows,
        "next_cursor": selected[-1].repository.casefold()
        if len(repositories) > request.limit
        else None,
        "config": {
            "poll_interval_seconds": config.poll_interval_seconds,
            "coalesce": config.coalesce,
        },
    }
