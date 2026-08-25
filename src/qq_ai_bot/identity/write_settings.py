"""Process-wide identity classification snapshot for C8 dual-write.

Writers read this immutable process snapshot. Task-local storage is not
used: worker tasks created before configure must see the same values as
the parent. Missing configuration fail-closes instead of silently
treating everyone as a non-superuser, non-ignored account.
"""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.config import Settings
from qq_ai_bot.identity.errors import IdentityDualWriteError


@dataclass(frozen=True, slots=True)
class IdentityWriteSettings:
    """Frozen SUPERUSERS / IGNORED_BOT_USERS for live identity classification."""

    superusers: frozenset[str] = frozenset()
    ignored_bot_users: frozenset[str] = frozenset()


_SNAPSHOT: IdentityWriteSettings | None = None


def identity_write_settings() -> IdentityWriteSettings:
    snapshot = _SNAPSHOT
    if snapshot is None:
        raise IdentityDualWriteError("identity_write_settings")
    return snapshot


def configure_identity_write_settings(settings: IdentityWriteSettings | Settings) -> None:
    """Replace the process-wide classification snapshot."""

    global _SNAPSHOT
    if isinstance(settings, Settings):
        snapshot = IdentityWriteSettings(
            superusers=settings.superusers,
            ignored_bot_users=settings.ignored_bot_users,
        )
    else:
        snapshot = settings
    _SNAPSHOT = snapshot


def reset_identity_write_settings() -> None:
    """Clear the process snapshot. Tests use this to avoid leakage."""

    global _SNAPSHOT
    _SNAPSHOT = None


def identity_write_settings_from_app(settings: Settings) -> IdentityWriteSettings:
    return IdentityWriteSettings(
        superusers=settings.superusers,
        ignored_bot_users=settings.ignored_bot_users,
    )
