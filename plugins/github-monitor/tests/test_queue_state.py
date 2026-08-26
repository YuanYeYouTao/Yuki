from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from github_monitor.models import (
    ActivationState,
    DeliveryUnit,
    PreparedMedia,
    QueuedSourceEvent,
    QueueState,
    TargetDelivery,
    build_prepared_notification,
    delivery_target_key,
)
from github_monitor.state import (
    DIAGNOSTIC_NAMESPACE,
    LEGACY_NAMESPACE,
    QUEUE_NAMESPACE,
    QueueSnapshot,
    QueueStateConflict,
    QueueStateInvariantError,
    clear_queue_diagnostic,
    compare_and_set_queue_state,
    load_queue_state,
    record_queue_diagnostic,
    validate_queue_transition,
)
from pydantic import ValidationError

from yuki_plugin_sdk.testing import FakePluginContext

NOW = datetime(2026, 8, 27, tzinfo=UTC)
FINGERPRINT = "a" * 64


@pytest.mark.asyncio
async def test_queue_diagnostic_is_bounded_and_clearable() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    await record_queue_diagnostic(context, "Owner/Repo", "receipt conflict: do-not-store")
    stored = await context.storage.get(DIAGNOSTIC_NAMESPACE, "owner/repo")
    assert stored == {
        "version": 1,
        "category": "unknown_failure",
        "recorded_at": stored["recorded_at"],
    }
    await clear_queue_diagnostic(context, "owner/repo")
    assert await context.storage.get(DIAGNOSTIC_NAMESPACE, "owner/repo") is None


class RecordingCASStorage:
    def __init__(self, raw: object = None) -> None:
        self.raw = raw
        self.expected_values: list[object] = []
        self.reject = False
        self.set_calls = 0
        self.delete_calls = 0

    async def get(self, namespace: str, key: str) -> object:
        del key
        return self.raw if namespace == QUEUE_NAMESPACE else None

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: object,
        value: object,
    ) -> bool:
        del namespace, key
        self.expected_values.append(expected)
        if self.reject or expected is not self.raw:
            return False
        self.raw = copy.deepcopy(value)
        return True

    async def set(self, *_args: object) -> None:
        self.set_calls += 1
        raise AssertionError("authoritative state must not use storage.set")

    async def delete(self, *_args: object) -> bool:
        self.delete_calls += 1
        raise AssertionError("authoritative state must not use storage.delete")

    async def list(self, _namespace: str) -> dict[str, object]:
        return {}


def _source(event_id: str, marker: str = "a") -> QueuedSourceEvent:
    return QueuedSourceEvent(
        github_event_id=event_id,
        source_fingerprint=marker * 64,
        skip_reason="filtered",
    )


def _prepared(*, sha: str = "a" * 64, handle: str = "h1"):
    return build_prepared_notification(
        event_key="github:owner/repo:event:100",
        event_type="WatchEvent",
        target_type="group",
        target_id="2001",
        occurred_at=NOW,
        summary="starred",
        payload={"repository": "owner/repo", "id": 100},
        text="starred",
        media=(PreparedMedia(index=0, handle_id=handle, sha256=sha, expires_at=NOW),),
        ask_agent=True,
        agent_intent="react",
    )


@pytest.mark.asyncio
async def test_initial_and_existing_cas_use_exact_raw_expected() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = RecordingCASStorage()
    context.storage = storage  # type: ignore[assignment]
    empty = await load_queue_state(context, "Owner/Repo")
    assert empty.raw is None
    accepted = QueueState(
        accepted_cursor="100",
        accepted_fingerprint=FINGERPRINT,
        pending=(_source("100"),),
    )
    saved = await compare_and_set_queue_state(context, "Owner/Repo", empty, accepted)
    assert storage.expected_values == [None]
    assert saved.raw is storage.raw
    exact_raw = storage.raw
    loaded = await load_queue_state(context, "owner/repo")
    assert loaded.raw is exact_raw
    updated = loaded.state.model_copy(update={"last_poll_at": NOW})
    await compare_and_set_queue_state(context, "owner/repo", loaded, updated)
    assert storage.expected_values[-1] is exact_raw
    assert storage.set_calls == 0
    assert storage.delete_calls == 0
    assert saved.state == accepted


@pytest.mark.asyncio
async def test_cas_conflict_is_stable_and_does_not_overwrite() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = RecordingCASStorage()
    storage.reject = True
    context.storage = storage  # type: ignore[assignment]
    snapshot = QueueSnapshot(raw=None, state=QueueState())
    state = QueueState(
        accepted_cursor="100",
        accepted_fingerprint=FINGERPRINT,
        pending=(_source("100"),),
    )
    with pytest.raises(QueueStateConflict, match="github_queue_state_conflict"):
        await compare_and_set_queue_state(context, "owner/repo", snapshot, state)
    assert storage.raw is None


@pytest.mark.asyncio
async def test_legacy_non_numeric_cursor_fails_closed_as_gap() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    await context.storage.set(
        LEGACY_NAMESPACE,
        "owner/repo",
        {"last_event_id": "business-key", "baseline_notified": True},
    )

    snapshot = await load_queue_state(context, "Owner/Repo")

    assert snapshot.raw is None
    assert snapshot.state.legacy_imported is True
    assert snapshot.state.accepted_cursor == ""
    assert snapshot.state.committed_cursor == ""
    assert snapshot.state.gap_reason == "legacy_cursor_not_numeric"
    assert snapshot.state.gap_at is not None
    assert snapshot.state.activation is not None
    assert snapshot.state.activation.complete is True


def test_queue_and_unit_invariants_fail_closed() -> None:
    with pytest.raises(ValidationError, match="numeric FIFO"):
        QueueState(
            accepted_cursor="100",
            accepted_fingerprint=FINGERPRINT,
            pending=(_source("100"), _source("99", "b")),
        )
    with pytest.raises(ValidationError, match="unique"):
        QueueState(
            accepted_cursor="100",
            accepted_fingerprint=FINGERPRINT,
            pending=(_source("100"), _source("100")),
        )
    with pytest.raises(ValidationError, match="committed cursor"):
        QueueState(
            accepted_cursor="99",
            accepted_fingerprint=FINGERPRINT,
            committed_cursor="100",
            committed_fingerprint=FINGERPRINT,
        )
    unit = DeliveryUnit(
        unit_id="github-unit:99-100",
        members=(_source("99"), _source("100", "b")),
        deliveries=(),
        sealed_at=NOW,
    )
    assert len(unit.members) == 2
    with pytest.raises(ValidationError, match="newer than the committed"):
        QueueState(
            accepted_cursor="100",
            accepted_fingerprint=FINGERPRINT,
            committed_cursor="99",
            committed_fingerprint="b" * 64,
            pending=(_source("98"), _source("100")),
        )
    with pytest.raises(ValidationError, match="drained queue"):
        QueueState(
            accepted_cursor="100",
            accepted_fingerprint=FINGERPRINT,
            committed_cursor="98",
            committed_fingerprint="b" * 64,
        )
    crossed = DeliveryUnit(
        unit_id="github-unit:102",
        members=(_source("102"),),
        deliveries=(),
        sealed_at=NOW,
    )
    with pytest.raises(ValidationError, match="precede pending"):
        QueueState(
            accepted_cursor="102",
            accepted_fingerprint=FINGERPRINT,
            committed_cursor="99",
            committed_fingerprint="b" * 64,
            inflight=crossed,
            pending=(_source("101", "c"),),
        )


def test_transition_cannot_drop_pending_skip_inflight_or_activation() -> None:
    source_99 = _source("99")
    source_100 = _source("100", "b")
    previous = QueueState(
        accepted_cursor="100",
        accepted_fingerprint="b" * 64,
        committed_cursor="98",
        committed_fingerprint="c" * 64,
        pending=(source_99, source_100),
    )
    with pytest.raises(QueueStateInvariantError, match="pending"):
        validate_queue_transition(
            previous,
            previous.model_copy(update={"pending": (source_100,)}),
        )
    with pytest.raises(QueueStateInvariantError, match="committed_cursor"):
        validate_queue_transition(
            previous,
            previous.model_copy(
                update={
                    "committed_cursor": "99",
                    "committed_fingerprint": source_99.source_fingerprint,
                }
            ),
        )
    active = previous.model_copy(
        update={
            "activation": ActivationState(
                activation_id="activation-1", occurred_at=NOW, deliveries=()
            )
        }
    )
    with pytest.raises(QueueStateInvariantError, match="activation"):
        validate_queue_transition(active, active.model_copy(update={"activation": None}))
    gap = previous.model_copy(update={"gap_reason": "cursor_gap", "gap_at": NOW})
    cleared = gap.model_copy(update={"gap_reason": "", "gap_at": None})
    with pytest.raises(QueueStateInvariantError, match="explicit_rebaseline"):
        validate_queue_transition(gap, cleared)
    validate_queue_transition(gap, cleared, allow_rebaseline=True)


def test_prepared_request_hash_covers_media_order_sha_and_handle() -> None:
    first = _prepared()
    assert first == _prepared()
    assert first.request_hash != _prepared(sha="b" * 64).request_hash
    assert first.request_hash != _prepared(handle="h2").request_hash
    dumped = first.model_dump(mode="json")
    assert type(first).model_validate(dumped) == first
    with pytest.raises(ValidationError, match="hash mismatch"):
        type(first).model_validate({**dumped, "text": "changed"})


def test_sealed_and_attempting_state_cannot_be_rewritten() -> None:
    source = _source("100")
    target_key = delivery_target_key("group", "2001")
    pending = TargetDelivery(
        target_key=target_key,
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=True,
        ask_agent=True,
    )
    unit = DeliveryUnit(
        unit_id="github-unit:100",
        members=(source,),
        deliveries=(pending,),
        sealed_at=NOW,
    )
    accepted = QueueState(
        accepted_cursor="100", accepted_fingerprint=FINGERPRINT, pending=(source,)
    )
    sealed = QueueState(accepted_cursor="100", accepted_fingerprint=FINGERPRINT, inflight=unit)
    validate_queue_transition(accepted, sealed)
    with pytest.raises(QueueStateInvariantError, match="sealed_unit_identity"):
        validate_queue_transition(
            sealed,
            sealed.model_copy(
                update={"inflight": unit.model_copy(update={"sealed_at": NOW.replace(year=2025)})}
            ),
        )
    with pytest.raises(QueueStateInvariantError, match="sealed_target_policy"):
        validate_queue_transition(
            sealed,
            sealed.model_copy(
                update={
                    "inflight": unit.model_copy(
                        update={"deliveries": (pending.model_copy(update={"ask_agent": False}),)}
                    )
                }
            ),
        )
    prepared_delivery = pending.model_copy(update={"status": "prepared", "prepared": _prepared()})
    prepared = sealed.model_copy(
        update={"inflight": unit.model_copy(update={"deliveries": (prepared_delivery,)})}
    )
    validate_queue_transition(sealed, prepared)
    attempting_delivery = prepared_delivery.model_copy(update={"status": "attempting"})
    attempting = prepared.model_copy(
        update={"inflight": unit.model_copy(update={"deliveries": (attempting_delivery,)})}
    )
    validate_queue_transition(prepared, attempting)
    changed_delivery = attempting_delivery.model_copy(update={"prepared": _prepared(handle="h2")})
    changed = attempting.model_copy(
        update={"inflight": unit.model_copy(update={"deliveries": (changed_delivery,)})}
    )
    with pytest.raises(QueueStateInvariantError, match="immutable"):
        validate_queue_transition(attempting, changed)
    rewritten_on_attempt = prepared.model_copy(
        update={
            "inflight": unit.model_copy(
                update={
                    "deliveries": (
                        pending.model_copy(
                            update={"status": "attempting", "prepared": _prepared(handle="h2")}
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(QueueStateInvariantError, match="prepared_request"):
        validate_queue_transition(prepared, rewritten_on_attempt)


def test_dequeue_requires_terminal_delivery_and_cursor_advance() -> None:
    source = _source("100")
    delivery = TargetDelivery(
        target_key=delivery_target_key("group", "2001"),
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=False,
        ask_agent=False,
    )
    unit = DeliveryUnit(
        unit_id="github-unit:100",
        members=(source,),
        deliveries=(delivery,),
        sealed_at=NOW,
    )
    before = QueueState(accepted_cursor="100", accepted_fingerprint=FINGERPRINT, inflight=unit)
    with pytest.raises(QueueStateInvariantError, match="terminal"):
        validate_queue_transition(
            before,
            QueueState(
                accepted_cursor="100",
                accepted_fingerprint=FINGERPRINT,
                committed_cursor="100",
                committed_fingerprint=FINGERPRINT,
            ),
        )
