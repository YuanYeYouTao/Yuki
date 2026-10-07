"""Turn-local read exposure and attribution handoff; mutations own their receipts."""

from __future__ import annotations

from enum import StrEnum

from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import MemoryTurnContract
from qq_ai_bot.memory.runtime.errors import IllegalMemoryTransitionError, MemorySessionClosedError
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


class AttributionHandoff(StrEnum):
    """Delivery-side attribution job lifecycle.  The worker never holds a session."""

    NONE = "none"
    EXPOSURE_FROZEN = "exposure_frozen"
    QUEUED = "queued"
    SKIPPED = "skipped"


class AttributionHandoffMachine:
    """Freeze exposures, then queue or skip.  Never re-opens after a terminal."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = AttributionHandoff.NONE

    @property
    def state(self) -> AttributionHandoff:
        return self._state

    def freeze_exposures(self) -> None:
        if self._state is AttributionHandoff.NONE:
            self._state = AttributionHandoff.EXPOSURE_FROZEN
            return
        if self._state is AttributionHandoff.EXPOSURE_FROZEN:
            return
        raise IllegalMemoryTransitionError(
            self._state.value,
            AttributionHandoff.EXPOSURE_FROZEN.value,
            "attribution already handed off",
        )

    def queue(self) -> None:
        if self._state is AttributionHandoff.QUEUED:
            return
        if self._state is not AttributionHandoff.EXPOSURE_FROZEN:
            raise IllegalMemoryTransitionError(
                self._state.value,
                AttributionHandoff.QUEUED.value,
                "queue requires frozen exposures",
            )
        self._state = AttributionHandoff.QUEUED

    def skip(self) -> None:
        if self._state in {AttributionHandoff.QUEUED, AttributionHandoff.SKIPPED}:
            if self._state is AttributionHandoff.QUEUED:
                raise IllegalMemoryTransitionError(
                    self._state.value,
                    AttributionHandoff.SKIPPED.value,
                    "already queued",
                )
            return
        if self._state is AttributionHandoff.NONE:
            self._state = AttributionHandoff.SKIPPED
            return
        if self._state is AttributionHandoff.EXPOSURE_FROZEN:
            self._state = AttributionHandoff.SKIPPED
            return
        raise IllegalMemoryTransitionError(self._state.value, AttributionHandoff.SKIPPED.value)


class RecallHandle:
    """One automatic or tool read that actually entered a model request.

    ``receipt_turn_id`` keeps the pre-existing receipt-row identity.  It is
    never interchangeable with ``runtime_turn_id``.
    """

    __slots__ = (
        "injected_fact_ids",
        "purpose",
        "receipt_turn_id",
        "runtime_turn_id",
    )

    def __init__(
        self,
        *,
        runtime_turn_id: str,
        receipt_turn_id: str,
        purpose: MemoryRecallPurpose,
        injected_fact_ids: tuple[int, ...] = (),
    ) -> None:
        if not runtime_turn_id:
            raise IllegalMemoryTransitionError("recall", "handle", "runtime_turn_id is required")
        if not receipt_turn_id:
            raise IllegalMemoryTransitionError("recall", "handle", "receipt_turn_id is required")
        self.runtime_turn_id = runtime_turn_id
        self.receipt_turn_id = receipt_turn_id
        self.purpose = purpose
        self.injected_fact_ids = injected_fact_ids


class RecallLedger:
    """Append-only list of recall handles for one turn."""

    __slots__ = ("_handles",)

    def __init__(self) -> None:
        self._handles: list[RecallHandle] = []

    def append(self, handle: RecallHandle) -> None:
        if any(item.receipt_turn_id == handle.receipt_turn_id for item in self._handles):
            raise IllegalMemoryTransitionError(
                handle.receipt_turn_id,
                "append",
                "receipt_turn_id already recorded",
            )
        self._handles.append(handle)

    def snapshot(self) -> tuple[RecallHandle, ...]:
        return tuple(self._handles)

    def extend_exposures(self, receipt_turn_id: str, fact_ids: tuple[int, ...]) -> None:
        for index, handle in enumerate(self._handles):
            if handle.receipt_turn_id == receipt_turn_id:
                self._handles[index] = RecallHandle(
                    runtime_turn_id=handle.runtime_turn_id,
                    receipt_turn_id=handle.receipt_turn_id,
                    purpose=handle.purpose,
                    injected_fact_ids=tuple(dict.fromkeys((*handle.injected_fact_ids, *fact_ids))),
                )
                return
        raise ValueError("recall receipt is not registered")

    def __len__(self) -> int:
        return len(self._handles)


class MemorySessionState:
    def __init__(self, contract: MemoryTurnContract, scope: ResolvedMemoryScope) -> None:
        self.contract = contract
        self.scope = scope
        self._attribution = AttributionHandoffMachine()
        self._recalls = RecallLedger()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def attribution(self) -> AttributionHandoff:
        return self._attribution.state

    def recall_handles(self) -> tuple[RecallHandle, ...]:
        return self._recalls.snapshot()

    def record_recall(self, handle: RecallHandle) -> None:
        self._require_open()
        self._recalls.append(handle)

    def extend_recall_exposures(self, receipt_turn_id: str, fact_ids: tuple[int, ...]) -> None:
        self._require_open()
        self._recalls.extend_exposures(receipt_turn_id, fact_ids)

    def freeze_exposures(self) -> None:
        self._require_open()
        self._attribution.freeze_exposures()

    def queue_attribution(self) -> None:
        self._require_open()
        self._attribution.queue()

    def skip_attribution(self) -> None:
        self._attribution.skip()

    def close(self) -> None:
        self._closed = True

    def require_open(self) -> None:
        """Public guard: session must still be open before I/O."""

        self._require_open()

    def _require_open(self) -> None:
        if self._closed:
            raise MemorySessionClosedError("memory session already closed")
