"""Shared deterministic quality limits for Dream decisions and mutations."""

from __future__ import annotations

MAX_DREAM_OUTPUTS = 4
MAX_DREAM_OUTPUT_CHARACTERS = 800
MAX_DREAM_TOTAL_CHARACTERS = 1600


def validate_output_lengths(contents: tuple[str, ...], *, per_output: int = 800) -> None:
    """Apply the same absolute budgets before generation acceptance and mutation."""
    if len(contents) > MAX_DREAM_OUTPUTS:
        raise ValueError("dream_output_count_exceeded")
    if any(len(content) > min(per_output, MAX_DREAM_OUTPUT_CHARACTERS) for content in contents):
        raise ValueError("dream_output_too_long")
    if sum(map(len, contents)) > MAX_DREAM_TOTAL_CHARACTERS:
        raise ValueError("dream_total_output_too_long")


def episode_compression_limit(
    source_characters: int,
    *,
    ratio: float,
    maximum: int,
) -> int:
    """Return one bounded total-output budget for an Episode recompose."""

    return max(1, min(maximum, int(source_characters * ratio)))
