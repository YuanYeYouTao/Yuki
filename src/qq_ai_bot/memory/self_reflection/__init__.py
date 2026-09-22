"""Independent, bounded Yuki self-reflection pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
    from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker

__all__ = ["SelfReflectionService", "SelfReflectionWorker"]


def __getattr__(name: str) -> type[SelfReflectionService] | type[SelfReflectionWorker]:
    # Importing schema must not construct the service/Database dependency graph.
    if name == "SelfReflectionService":
        from qq_ai_bot.memory.self_reflection.service import SelfReflectionService

        return SelfReflectionService
    if name == "SelfReflectionWorker":
        from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker

        return SelfReflectionWorker
    raise AttributeError(name)
