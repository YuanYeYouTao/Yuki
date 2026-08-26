"""CAS-only durable continuity state for GitHub Monitor."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import JsonValue

from .models import ActivationState, DeliveryUnit, QueueState, RepositoryState

LEGACY_NAMESPACE = "github_monitor"
QUEUE_NAMESPACE = "github_monitor_queue_v1"


class QueueStateConflict(RuntimeError):
    """Another coordinator changed the repository state."""


class QueueStateInvariantError(RuntimeError):
    """A proposed transition would weaken the persisted WAL."""


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    raw: JsonValue
    state: QueueState


async def load_queue_state(context: PluginContext, repository: str) -> QueueSnapshot:
    """Load the exact raw CAS value and parse its immutable projection."""

    key = repository.casefold()
    raw = await context.storage.get(QUEUE_NAMESPACE, key)
    if raw is not None:
        return QueueSnapshot(raw=raw, state=QueueState.model_validate(raw))
    legacy_raw = await context.storage.get(LEGACY_NAMESPACE, key)
    if legacy_raw is None:
        return QueueSnapshot(raw=None, state=QueueState())
    legacy = RepositoryState.model_validate(legacy_raw)
    return QueueSnapshot(raw=None, state=_import_legacy(legacy, key))


async def compare_and_set_queue_state(
    context: PluginContext,
    repository: str,
    snapshot: QueueSnapshot,
    state: QueueState,
    *,
    allow_rebaseline: bool = False,
) -> QueueSnapshot:
    """Validate and CAS one authoritative value using the unmodified raw expected."""

    state = QueueState.model_validate(state.model_dump(mode="json"))
    validate_queue_transition(snapshot.state, state, allow_rebaseline=allow_rebaseline)
    raw = state.model_dump(mode="json")
    swapped = await context.storage.compare_and_set(
        QUEUE_NAMESPACE,
        repository.casefold(),
        snapshot.raw,
        raw,
    )
    if not swapped:
        raise QueueStateConflict("github_queue_state_conflict")
    persisted = await context.storage.get(QUEUE_NAMESPACE, repository.casefold())
    if persisted is None:
        raise QueueStateConflict("github_queue_state_missing_after_cas")
    persisted_state = QueueState.model_validate(persisted)
    if persisted_state != state:
        raise QueueStateConflict("github_queue_state_changed_after_cas")
    return QueueSnapshot(raw=persisted, state=persisted_state)


async def mirror_legacy_state(context: PluginContext, repository: str, state: QueueState) -> None:
    """Mirror observability only after the authoritative queue is fully drained."""

    if state.pending or state.inflight is not None:
        raise QueueStateInvariantError("legacy_mirror_requires_drained_queue")
    await context.storage.set(
        LEGACY_NAMESPACE,
        repository.casefold(),
        repository_state_projection(state).model_dump(mode="json"),
    )


async def load_repository_state(context: PluginContext, repository: str) -> RepositoryState:
    """Compatibility read projection while C4B migrates the poller and commands."""

    snapshot = await load_queue_state(context, repository)
    return repository_state_projection(snapshot.state)


def validate_queue_transition(
    previous: QueueState,
    current: QueueState,
    *,
    allow_rebaseline: bool = False,
) -> None:
    if previous.gap_reason and not current.gap_reason and not allow_rebaseline:
        raise QueueStateInvariantError("gap_requires_explicit_rebaseline")
    if not allow_rebaseline:
        _require_cursor_monotonic(previous.accepted_cursor, current.accepted_cursor, "accepted")
        _require_cursor_monotonic(previous.committed_cursor, current.committed_cursor, "committed")
        _require_boundary_fingerprint(
            previous.accepted_cursor,
            previous.accepted_fingerprint,
            current.accepted_cursor,
            current.accepted_fingerprint,
            "accepted",
        )
        _require_boundary_fingerprint(
            previous.committed_cursor,
            previous.committed_fingerprint,
            current.committed_cursor,
            current.committed_fingerprint,
            "committed",
        )
    if previous.legacy_boundary and current.legacy_boundary != previous.legacy_boundary:
        raise QueueStateInvariantError("legacy_boundary_is_immutable")
    if (
        previous.committed_cursor == current.committed_cursor
        and previous.committed_created_at != current.committed_created_at
    ):
        raise QueueStateInvariantError("committed_time_is_immutable")
    if previous.activation is not None and current.activation is None:
        raise QueueStateInvariantError("activation_cannot_disappear")
    if previous.activation is None and current.activation is not None and previous.accepted_cursor:
        raise QueueStateInvariantError("activation_requires_uninitialized_stream")
    if previous.inflight is not None and current.inflight is not None:
        _require_same_sealed_unit(
            previous.inflight,
            current.inflight,
            allow_explicit_abort=allow_rebaseline and bool(previous.gap_reason),
        )
        if previous.pending != current.pending:
            raise QueueStateInvariantError("pending_cannot_change_while_inflight")
    elif previous.inflight is None and current.inflight is not None:
        member_count = len(current.inflight.members)
        if previous.pending[:member_count] != current.inflight.members:
            raise QueueStateInvariantError("inflight_must_seal_pending_fifo_prefix")
        if previous.pending[member_count:] != current.pending:
            raise QueueStateInvariantError("sealing_must_only_remove_inflight_members")
    elif previous.inflight is not None and current.inflight is None:
        if any(
            item.status not in {"completed", "skipped"} for item in previous.inflight.deliveries
        ):
            raise QueueStateInvariantError("inflight_must_be_terminal_before_dequeue")
        last_member = previous.inflight.members[-1]
        if current.committed_cursor != last_member.github_event_id:
            raise QueueStateInvariantError("dequeue_must_commit_exact_inflight_tail")
        if current.committed_fingerprint != last_member.source_fingerprint:
            raise QueueStateInvariantError("dequeue_must_commit_exact_inflight_fingerprint")
        expected_created_at = last_member.source_created_at
        if current.committed_created_at != expected_created_at:
            raise QueueStateInvariantError("dequeue_must_commit_exact_inflight_time")
        if previous.pending != current.pending:
            raise QueueStateInvariantError("dequeue_cannot_change_pending")
    elif previous.pending != current.pending:
        if current.pending[: len(previous.pending)] != previous.pending:
            raise QueueStateInvariantError("pending_may_only_append_before_seal")
        appended = current.pending[len(previous.pending) :]
        if not appended or current.accepted_cursor == previous.accepted_cursor:
            raise QueueStateInvariantError("pending_append_must_advance_accepted_cursor")
        if (
            appended[-1].github_event_id != current.accepted_cursor
            or appended[-1].source_fingerprint != current.accepted_fingerprint
        ):
            raise QueueStateInvariantError("accepted_boundary_must_match_appended_tail")
    elif previous.accepted_cursor != current.accepted_cursor and not allow_rebaseline:
        raise QueueStateInvariantError("accepted_cursor_requires_appended_work")
    if (
        previous.inflight is None
        and current.inflight is None
        and previous.committed_cursor != current.committed_cursor
        and not allow_rebaseline
    ):
        raise QueueStateInvariantError("committed_cursor_requires_inflight_dequeue")
    previous_pending = {item.github_event_id: item for item in previous.pending}
    current_pending = {item.github_event_id: item for item in current.pending}
    for event_id in previous_pending.keys() & current_pending.keys():
        if previous_pending[event_id] != current_pending[event_id]:
            raise QueueStateInvariantError("accepted_source_event_is_immutable")
    if previous.activation is not None and current.activation is not None:
        if (
            previous.activation.activation_id != current.activation.activation_id
            or previous.activation.occurred_at != current.activation.occurred_at
        ):
            raise QueueStateInvariantError("activation_identity_is_immutable")
        _require_delivery_progress(
            previous.activation.deliveries,
            current.activation.deliveries,
            allow_explicit_abort=allow_rebaseline and bool(previous.gap_reason),
        )


def repository_state_projection(state: QueueState) -> RepositoryState:
    return RepositoryState(
        last_event_id=state.committed_cursor,
        last_event_created_at=state.committed_created_at,
        etag=state.etag,
        last_modified=state.last_modified,
        last_poll_at=state.last_poll_at,
        last_success_at=state.last_success_at,
        consecutive_failures=state.consecutive_failures,
        paused_until=state.paused_until,
        rate_limit_remaining=state.rate_limit_remaining,
        rate_limit_reset_at=state.rate_limit_reset_at,
        last_request_id=state.last_request_id,
        backlog_truncated=state.backlog_pending,
        baseline_notified=bool(state.activation and state.activation.complete),
    )


def _import_legacy(legacy: RepositoryState, repository: str) -> QueueState:
    cursor = legacy.last_event_id
    activation = (
        ActivationState(
            activation_id=_legacy_activation_id(repository),
            occurred_at=(
                legacy.last_success_at or legacy.last_poll_at or datetime(1970, 1, 1, tzinfo=UTC)
            ),
            deliveries=(),
        )
        if legacy.baseline_notified
        else None
    )
    if cursor and not cursor.isdecimal():
        return QueueState(
            legacy_imported=True,
            activation=activation,
            etag=legacy.etag,
            last_modified=legacy.last_modified,
            last_poll_at=legacy.last_poll_at,
            last_success_at=legacy.last_success_at,
            consecutive_failures=legacy.consecutive_failures,
            paused_until=legacy.paused_until,
            rate_limit_remaining=legacy.rate_limit_remaining,
            rate_limit_reset_at=legacy.rate_limit_reset_at,
            last_request_id=legacy.last_request_id,
            backlog_pending=legacy.backlog_truncated,
            gap_reason="legacy_cursor_not_numeric",
            gap_at=datetime.now(UTC),
        )
    return QueueState(
        accepted_cursor=cursor,
        accepted_fingerprint="",
        committed_cursor=cursor,
        committed_fingerprint="",
        committed_created_at=legacy.last_event_created_at,
        legacy_boundary=cursor,
        legacy_imported=True,
        activation=activation,
        etag=legacy.etag,
        last_modified=legacy.last_modified,
        last_poll_at=legacy.last_poll_at,
        last_success_at=legacy.last_success_at,
        consecutive_failures=legacy.consecutive_failures,
        paused_until=legacy.paused_until,
        rate_limit_remaining=legacy.rate_limit_remaining,
        rate_limit_reset_at=legacy.rate_limit_reset_at,
        last_request_id=legacy.last_request_id,
        backlog_pending=legacy.backlog_truncated,
    )


def _legacy_activation_id(repository: str) -> str:
    digest = hashlib.sha256(repository.casefold().encode("utf-8")).hexdigest()[:32]
    return f"legacy-{digest}"


def _require_cursor_monotonic(previous: str, current: str, label: str) -> None:
    if previous and (not current or int(current) < int(previous)):
        raise QueueStateInvariantError(f"{label}_cursor_regressed")


def _require_boundary_fingerprint(
    previous_cursor: str,
    previous_fingerprint: str,
    current_cursor: str,
    current_fingerprint: str,
    label: str,
) -> None:
    if previous_cursor == current_cursor and previous_fingerprint:
        if current_fingerprint != previous_fingerprint:
            raise QueueStateInvariantError(f"{label}_fingerprint_changed")


def _require_same_sealed_unit(
    previous: DeliveryUnit,
    current: DeliveryUnit,
    *,
    allow_explicit_abort: bool = False,
) -> None:
    if (
        previous.unit_id != current.unit_id
        or previous.members != current.members
        or previous.sealed_at != current.sealed_at
    ):
        raise QueueStateInvariantError("sealed_unit_identity_is_immutable")
    _require_delivery_progress(
        previous.deliveries,
        current.deliveries,
        allow_explicit_abort=allow_explicit_abort,
    )


def _require_delivery_progress(
    previous: tuple[Any, ...],
    current: tuple[Any, ...],
    *,
    allow_explicit_abort: bool = False,
) -> None:
    old = {item.target_key: item for item in previous}
    new = {item.target_key: item for item in current}
    if set(old) != set(new):
        raise QueueStateInvariantError("sealed_targets_are_immutable")
    allowed = {
        "pending": {"pending", "prepared", "skipped"},
        "prepared": {"prepared", "attempting", "skipped"},
        "attempting": {"attempting", "completed"},
        "completed": {"completed"},
        "skipped": {"skipped"},
    }
    if allow_explicit_abort:
        allowed["attempting"] = {*allowed["attempting"], "skipped"}
    for key, old_item in old.items():
        new_item = new[key]
        if (
            old_item.target_type != new_item.target_type
            or old_item.target_id != new_item.target_id
            or old_item.send_text != new_item.send_text
            or old_item.send_card != new_item.send_card
            or old_item.ask_agent != new_item.ask_agent
        ):
            raise QueueStateInvariantError("sealed_target_policy_is_immutable")
        if new_item.status not in allowed[old_item.status]:
            raise QueueStateInvariantError("delivery_status_transition_invalid")
        if old_item.prepared is not None:
            if old_item.prepared != new_item.prepared:
                raise QueueStateInvariantError("prepared_request_is_immutable")
        if old_item.status in {"completed", "skipped"} and old_item != new_item:
            raise QueueStateInvariantError("terminal_delivery_is_immutable")
