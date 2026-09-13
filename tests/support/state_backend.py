"""Minimal state executor for isolated Runner protocol tests only."""

from typing import Any

from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.workspace.short_state import STATE_TOOL, ShortState


class ShortStateOnlyBackend:
    """Text-only Main Agent entries may use global state, with no other side effects."""

    def __init__(self, state: ShortState) -> None:
        self.state = state

    def definitions(self, runtime: Any, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        return (STATE_TOOL,)

    def begin_batch(self, calls: Any, runtime: Any) -> None:
        pass

    async def execute(self, name: str, arguments_json: str, runtime: Any) -> str:
        if name == STATE_TOOL.name:
            return await self.state.execute(arguments_json)
        return '{"ok":false,"error":"capability_not_allowed"}'

    def parallel_safe(self, name: str, runtime: Any) -> bool:
        return False

    def is_side_effecting(self, name: str, arguments_json: str, runtime: Any) -> bool:
        return True

    def finalize(self, content: str, runtime: Any) -> str:
        return content

    def exhausted(self, runtime: Any) -> str:
        return "本轮处理已达到调用上限。"
