"""Process binary identity epoch. Independent of product version strings."""

from __future__ import annotations

from typing import Final

from qq_ai_bot.identity.errors import IdentityDualWriteError

IDENTITY_BINARY_EPOCH: Final[str] = "v2"


def refuse_identity_binary_epoch(runtime_state: str) -> None:
    """v2 binary refuses v1 data; v1 binary refuses v2 data."""

    if IDENTITY_BINARY_EPOCH not in {"v1", "v2"}:
        raise IdentityDualWriteError("identity_binary_epoch")
    if runtime_state not in {"v1", "v2"}:
        raise IdentityDualWriteError("identity_runtime_state")
    if IDENTITY_BINARY_EPOCH != runtime_state:
        raise IdentityDualWriteError("identity_binary_epoch")
