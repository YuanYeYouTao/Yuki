"""Ambient execution IDs and in-memory per-turn phase timings."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from yuki.runtime.origin import TurnOrigin


def new_runtime_turn_id() -> str:
    """Random opaque turn id; carries no timing or identity information."""

    return uuid.uuid4().hex


def identifier_hash(value: str | None) -> str | None:
    """Return a stable short hash without exposing a QQ, group, or conversation key."""

    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


_STABLE_IDENTIFIER_SALT = "yuki-planner-v1"


def stable_identifier_hash(value: str, *, kind: str) -> str:
    """Domain-separated SHA-256 used by cadence and 0039 backfill.

    The salt string is historical and load-bearing; do not change the payload.
    """

    payload = f"{_STABLE_IDENTIFIER_SALT}\0{kind}\0{value}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class RuntimeTurnCorrelation:
    """Execution correlation inherited by its original asyncio task tree."""

    turn_id: str
    origin: TurnOrigin
    phase_seconds: dict[str, float] = field(default_factory=dict)


_CURRENT_CORRELATION: ContextVar[RuntimeTurnCorrelation | None] = ContextVar(
    "yuki_runtime_turn_correlation", default=None
)


def current_runtime_turn_correlation() -> RuntimeTurnCorrelation | None:
    return _CURRENT_CORRELATION.get()


def claim_runtime_turn_id() -> str | None:
    """Read the original ambient ID; unbound work retains an unknown ID."""

    correlation = _CURRENT_CORRELATION.get()
    if correlation is None:
        return None
    return correlation.turn_id


@contextmanager
def bind_runtime_turn(correlation: RuntimeTurnCorrelation) -> Iterator[RuntimeTurnCorrelation]:
    """Bind ``correlation`` to the current task tree for the duration."""

    token = _CURRENT_CORRELATION.set(correlation)
    try:
        yield correlation
    finally:
        _CURRENT_CORRELATION.reset(token)
