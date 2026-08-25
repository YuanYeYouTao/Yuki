"""Identity epoch helpers. Complete v2 is the only ingress activation gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.errors import CanonicalIdentityError

COMPLETE_V2_REQUIRED: Final[frozenset[str]] = frozenset(
    {"state", "cutover_id", "source_fingerprint", "completed_at"}
)


@final
@dataclass(frozen=True, slots=True)
class IdentityRuntimeSnapshot:
    state: str
    complete_v2: bool
    revision: int
    cutover_id: str | None
    source_fingerprint: str | None
    completed_at: datetime | None


def is_complete_v2_row(row: IdentityRuntimeStateModel) -> bool:
    return (
        str(row.state) == "v2"
        and bool(row.cutover_id)
        and bool(row.source_fingerprint)
        and row.completed_at is not None
    )


async def load_identity_runtime(session: AsyncSession) -> IdentityRuntimeSnapshot:
    rows = list(
        await session.scalars(
            select(IdentityRuntimeStateModel).order_by(IdentityRuntimeStateModel.id)
        )
    )
    if len(rows) != 1 or int(rows[0].id) != 1:
        raise CanonicalIdentityError("identity_runtime_state")
    row = rows[0]
    state = str(row.state)
    if state not in {"v1", "v2"}:
        raise CanonicalIdentityError("identity_runtime_state")
    return IdentityRuntimeSnapshot(
        state=state,
        complete_v2=is_complete_v2_row(row),
        revision=int(row.revision),
        cutover_id=row.cutover_id,
        source_fingerprint=row.source_fingerprint,
        completed_at=row.completed_at,
    )


async def require_complete_v2_runtime(session: AsyncSession) -> IdentityRuntimeSnapshot:
    snapshot = await load_identity_runtime(session)
    if not snapshot.complete_v2:
        raise CanonicalIdentityError("identity_runtime_state")
    return snapshot


async def require_identity_runtime(
    session: AsyncSession,
    *,
    allowed: frozenset[str],
) -> IdentityRuntimeSnapshot:
    snapshot = await load_identity_runtime(session)
    if snapshot.state not in allowed:
        raise CanonicalIdentityError("identity_runtime_state")
    if snapshot.state == "v2" and not snapshot.complete_v2:
        raise CanonicalIdentityError("identity_runtime_state")
    return snapshot


async def identity_runtime_is_complete_v2(session: AsyncSession) -> bool:
    try:
        return (await load_identity_runtime(session)).complete_v2
    except CanonicalIdentityError:
        return False
