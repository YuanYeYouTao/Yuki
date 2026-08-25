"""Persistence-neutral SQL constraint helpers for canonical UUID identifiers."""

from __future__ import annotations

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)


def uuid4_text36_sql(column: str) -> str:
    """SQLite CHECK for canonical lowercase UUID4 TEXT(36)."""

    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


def optional_uuid4_text36_sql(column: str) -> str:
    """SQLite CHECK for a nullable canonical UUID4 TEXT(36)."""

    return f"{column} IS NULL OR ({uuid4_text36_sql(column)})"
