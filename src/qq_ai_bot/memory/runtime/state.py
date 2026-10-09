"""Turn-local read exposure and attribution handoff; mutations own their receipts."""

from __future__ import annotations

from qq_ai_bot.memory.runtime.contract import MemoryTurnContract
from qq_ai_bot.memory.runtime.errors import MemorySessionClosedError
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


class MemorySessionState:
    def __init__(self, contract: MemoryTurnContract, scope: ResolvedMemoryScope) -> None:
        self.contract = contract
        self.scope = scope
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def require_open(self) -> None:
        """Public guard: session must still be open before I/O."""

        self._require_open()

    def _require_open(self) -> None:
        if self._closed:
            raise MemorySessionClosedError("memory session already closed")
