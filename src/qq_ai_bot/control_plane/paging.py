"""Opaque cursor and explicit numbered browsing over live authorized records."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, final

from qq_ai_bot.control_plane.tokens import (
    MAX_CURSOR_LENGTH,
    require_aware_datetime,
    require_opaque_token,
)

DEFAULT_PAGE_LIMIT: Final[int] = 20
MAX_PAGE_LIMIT: Final[int] = 100


@final
@dataclass(frozen=True, slots=True)
class Cursor:
    """Opaque pagination cursor. Callers must not parse it as an offset."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            require_opaque_token(self.value, name="cursor", max_length=MAX_CURSOR_LENGTH),
        )


@final
@dataclass(frozen=True, slots=True)
class PageRequest:
    """Cursor continuation or numbered browsing; the two are mutually exclusive."""

    limit: int = DEFAULT_PAGE_LIMIT
    cursor: Cursor | None = None
    number: int | None = None

    def __post_init__(self) -> None:
        if type(self.limit) is not int or type(self.limit) is bool:
            raise TypeError("limit must be an int")
        if self.limit < 1 or self.limit > MAX_PAGE_LIMIT:
            raise ValueError("limit must be between 1 and 100")
        if self.cursor is not None and type(self.cursor) is not Cursor:
            raise TypeError("cursor must be Cursor or None")
        if self.number is not None:
            if type(self.number) is not int or not 1 <= self.number <= 1000000:
                raise ValueError("page number must be between 1 and 1000000")
            if self.cursor is not None:
                raise ValueError("numbered pages cannot use a cursor")


@final
@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of already-ordered items plus an optional opaque next cursor."""

    items: tuple[T, ...]
    next_cursor: Cursor | None = None
    snapshot_at: datetime | None = None
    total: int | None = None
    number: int | None = None

    def __init__(
        self,
        items: Sequence[T],
        next_cursor: Cursor | None = None,
        snapshot_at: datetime | None = None,
        *,
        total: int | None = None,
        number: int | None = None,
    ) -> None:
        if isinstance(items, (str, bytes)):
            raise TypeError("page items must be a sequence")
        if next_cursor is not None and type(next_cursor) is not Cursor:
            raise TypeError("next_cursor must be Cursor or None")
        object.__setattr__(self, "items", tuple(items))
        object.__setattr__(self, "next_cursor", next_cursor)
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("invalid page total")
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "number", number)
        object.__setattr__(
            self,
            "snapshot_at",
            None
            if snapshot_at is None
            else require_aware_datetime(snapshot_at, name="snapshot_at"),
        )


def paginate[T](
    items: Sequence[T],
    request: PageRequest,
    *,
    sort_key: Callable[[T], str],
) -> Page[T]:
    """Return a stably ordered page. ``sort_key`` values must be unique."""

    if type(request) is not PageRequest:
        raise TypeError("request must be PageRequest")
    if not callable(sort_key):
        raise TypeError("sort_key must be callable")
    ordered = tuple(sorted(items, key=sort_key))
    keys = [
        require_opaque_token(sort_key(item), name="sort_key", max_length=MAX_CURSOR_LENGTH)
        for item in ordered
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("sort_key must be unique")
    start = 0
    if request.number is not None:
        start = (request.number - 1) * request.limit
    if request.cursor is not None:
        try:
            start = keys.index(request.cursor.value) + 1
        except ValueError as exc:
            raise ValueError("cursor does not match this page snapshot") from exc
    window = ordered[start : start + request.limit]
    next_cursor = None
    if start + request.limit < len(ordered) and window:
        next_cursor = Cursor(sort_key(window[-1]))
    return Page(window, next_cursor=next_cursor, total=len(ordered), number=request.number)
