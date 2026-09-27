"""Map durable mutation results into the active memory session state."""

from __future__ import annotations

from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationOutcome,
    MemoryMutationResult,
)
from qq_ai_bot.memory.runtime.state import (
    MutationState,
)


def mutation_state_for_result(result: MemoryMutationResult) -> MutationState:
    """Map a durable result onto the session mutation ledger."""

    if result.reason_code == "memory_candidate_ambiguous":
        return MutationState.AMBIGUOUS
    if result.reason_code == "memory_candidate_not_found":
        return MutationState.NOT_FOUND
    if result.outcome is MemoryMutationOutcome.REJECTED or not result.ok:
        return MutationState.REJECTED
    if (
        result.applied_operation is MemoryMutationAppliedOperation.NOOP
        or result.outcome is MemoryMutationOutcome.NO_CHANGE
    ):
        return MutationState.NO_CHANGE
    if (
        result.outcome is MemoryMutationOutcome.DEDUPLICATED
        or result.applied_operation is MemoryMutationAppliedOperation.MERGE_EVIDENCE
    ):
        return MutationState.DEDUPLICATED
    if (
        result.outcome is MemoryMutationOutcome.COMMITTED_AS_CONTESTED
        or result.applied_operation is MemoryMutationAppliedOperation.CONTEST
    ):
        return MutationState.COMMITTED_AS_CONTESTED
    return MutationState.COMMITTED
