"""Async SQLAlchemy persistence layer.

The package root deliberately resolves its compatibility exports lazily. ORM
model modules import :mod:`qq_ai_bot.persistence.models` while the application
graph is still being assembled; eagerly importing repositories here would
re-enter identity and conversation models through that package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.repositories import (
        EmojiDescriptionRecord,
        EmojiDescriptionRepository,
        GroupSettingsRepository,
        MediaAnalysisRecord,
        MediaAnalysisRepository,
        ProcessedEventRepository,
    )

__all__ = [
    "Database",
    "EmojiDescriptionRecord",
    "EmojiDescriptionRepository",
    "GroupSettingsRepository",
    "MediaAnalysisRecord",
    "MediaAnalysisRepository",
    "ProcessedEventRepository",
]


def __getattr__(name: str) -> Any:
    """Resolve legacy package-root exports without eager application imports."""

    if name == "Database":
        from qq_ai_bot.persistence.database import Database

        return Database
    if name in {
        "EmojiDescriptionRecord",
        "EmojiDescriptionRepository",
        "GroupSettingsRepository",
        "MediaAnalysisRecord",
        "MediaAnalysisRepository",
        "ProcessedEventRepository",
    }:
        from qq_ai_bot.persistence import repositories

        return getattr(repositories, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose compatibility exports to introspection without resolving them."""

    return sorted({*globals(), *__all__})
