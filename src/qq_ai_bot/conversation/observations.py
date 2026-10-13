"""Validate original observation references retained by paid Work journals."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.observation_models import ContextObservationModel


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _valid_observation_root(
    identity: str,
    versions: dict[str, int],
    parents: dict[str, tuple[tuple[str, int], ...]],
    valid: dict[str, bool],
) -> bool:
    """Coverage needs complete same-grant metadata, not just a selected ID.

    Iterative postorder avoids recursion limits; memoization visits shared
    ancestors once. Missing/changed parents and cycles grant no coverage.
    """
    pending = [(identity, False)]
    active = set()
    while pending:
        current, finish = pending.pop()
        if current in valid:
            continue
        if current not in versions:
            valid[current] = False
            continue
        if finish:
            active.discard(current)
            valid[current] = all(
                versions.get(parent) == version and valid.get(parent, False)
                for parent, version in parents[current]
            )
        elif current in active:
            valid[current] = False
        elif any(versions.get(parent) != version for parent, version in parents[current]):
            valid[current] = False
        else:
            active.add(current)
            pending.append((current, True))
            pending.extend((parent, False) for parent, _ in parents[current])
    return valid[identity]


async def validate_observations(
    session: AsyncSession,
    conversation_id: str,
    generation: int,
    actor_id: str,
    read_scope: str,
    sources: tuple[tuple[str, int], ...],
    *,
    snapshot_privacy_generation: int | None = None,
) -> bool:
    """Validate the DAG in this snapshot; supplied privacy must come from this transaction."""
    if not sources:
        return True
    ids = tuple(identity for identity, _ in sources)
    if len(set(ids)) != len(ids) or not actor_id or not read_scope:
        return False
    pending = dict(sources)
    verified: dict[str, int] = {}
    parents: dict[str, tuple[tuple[str, int], ...]] = {}
    privacy = (
        await privacy_generation(session)
        if snapshot_privacy_generation is None
        else snapshot_privacy_generation
    )
    while pending:
        rows: list[Row[tuple[str, int, str]]] = []
        identities = tuple(pending)
        for start in range(0, len(identities), 256):
            rows.extend(
                (
                    await session.execute(
                        select(
                            ContextObservationModel.id,
                            ContextObservationModel.version,
                            ContextObservationModel.parent_sources_json,
                        ).where(
                            ContextObservationModel.id.in_(identities[start : start + 256]),
                            ContextObservationModel.conversation_id == conversation_id,
                            ContextObservationModel.generation == generation,
                            ContextObservationModel.actor_id == actor_id,
                            ContextObservationModel.read_scope == read_scope,
                            ContextObservationModel.privacy_generation == privacy,
                        )
                    )
                ).all()
            )
        if {identity: version for identity, version, _ in rows} != pending:
            return False
        verified.update(pending)
        pending = {}
        for root, _, parent_json in rows:
            parents[root] = tuple(
                (identity, int(version)) for identity, version in json.loads(parent_json)
            )
            for identity, version in parents[root]:
                if identity in verified:
                    if verified[identity] != version:
                        return False
                elif identity in pending and pending[identity] != version:
                    return False
                else:
                    pending[identity] = version
    # Published summaries point to previously verified sources, so valid
    # factory output is a DAG. Corrupt cyclic metadata must not authorize a
    # projection which the observation reader would correctly reject.
    valid: dict[str, bool] = {}
    return all(_valid_observation_root(identity, verified, parents, valid) for identity in ids)


async def privacy_generation(session: AsyncSession) -> int:
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

    return int(
        await session.scalar(
            select(ExecutionTraceStateModel.privacy_generation).where(
                ExecutionTraceStateModel.id == 1
            )
        )
        or 0
    )
