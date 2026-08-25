"""Parse the configured origin whitelist for rollup model compaction."""

from __future__ import annotations

from qq_ai_bot.runtime.origin import TurnOrigin

_DEFAULT = frozenset({TurnOrigin.USER_MESSAGE.value})
_ALLOWED = frozenset(item.value for item in TurnOrigin)


def parse_rollup_llm_origins(raw: str) -> frozenset[str]:
    """Return known TurnOrigin values; empty input keeps user_message only.

    Changing this setting does not scan or wake parked jobs by itself. A later
    ledger or visual force signal increments signal_revision and sets
    next_attempt_at=now on the same job; the new whitelist then applies.
    """

    parsed = frozenset(
        item.strip() for item in raw.split(",") if item.strip() and item.strip() in _ALLOWED
    )
    return parsed or _DEFAULT
