"""Transport-independent application services.

Public service symbols are loaded lazily so importing a nested service does not
eagerly import ``MessageProcessor`` and create an administrator-service cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuki.services.chat import ChatService, OutboundSender
    from yuki.services.processor import MessageProcessor, ProcessResult

__all__ = ["ChatService", "MessageProcessor", "OutboundSender", "ProcessResult"]


def __getattr__(name: str) -> Any:
    """Preserve the package-level API without eager cross-service imports."""

    if name in {"ChatService", "OutboundSender"}:
        from yuki.services.chat import ChatService, OutboundSender

        return {"ChatService": ChatService, "OutboundSender": OutboundSender}[name]
    if name in {"MessageProcessor", "ProcessResult"}:
        from yuki.services.processor import MessageProcessor, ProcessResult

        return {"MessageProcessor": MessageProcessor, "ProcessResult": ProcessResult}[name]
    raise AttributeError(name)
