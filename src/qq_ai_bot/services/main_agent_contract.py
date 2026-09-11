"""One deployment manifest and global short-state snapshot for every Yuki entrypoint."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any

from qq_ai_bot.capabilities.request import request_tools_definition
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.workspace.short_state import STATE_TOOL, ShortState


class MainAgentContract:
    def __init__(self, chat: Any, automation: Any, state: ShortState) -> None:
        self.chat, self.automation, self.state = chat, automation, state
        self.automation_names: dict[str, str] = {}
        self._tools: tuple[ChatTool, ...] | None = None
        self._lock = asyncio.Lock()

    async def definitions(self) -> tuple[ChatTool, ...]:
        async with self._lock:
            if self._tools is not None:
                return self._tools
            from qq_ai_bot.services.chat import _SET_REPLY_TARGET_TOOL

            # No event/person/group can affect declaration. This runtime is NEVER used to execute.
            config = await self.chat._runtime_config.snapshot()
            declaration = ToolRuntime(
                inbound=None,
                gateway=None,
                allow_generic_onebot=False,
                declaration_only=True,
                runtime_config=config,
            )
            registry = self.chat._build_tool_registry(declaration, web_was_used=False)
            for provider in self.chat._external_tool_providers:
                await provider.refresh(force=False)
            tools = [
                entry.descriptor.as_chat_tool() for entry in registry.catalog(declaration).entries
            ]
            tools.extend((request_tools_definition(), _SET_REPLY_TARGET_TOOL, STATE_TOOL))
            if self.automation._registry is not None:
                for capability in self.automation._registry.list():
                    if not capability.name.startswith("yuki."):
                        matching = [
                            tool
                            for tool in tools
                            if tool.parameters == capability.input_schema
                            and tool.description == capability.description
                        ]
                        if len(matching) == 1:
                            self.automation_names[capability.name] = matching[0].name
                            continue
                        self.automation_names[capability.name] = (
                            self.automation._registry.agent_tool_name(capability.name)
                        )
                        tools.append(
                            ChatTool(
                                name=self.automation._registry.agent_tool_name(capability.name),
                                description=capability.description,
                                parameters=capability.input_schema,
                                result_cacheable=capability.result_cacheable,
                            )
                        )
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("duplicate Main Agent manifest tool")
            self._tools = deepcopy(tuple(sorted(tools, key=lambda item: item.name)))
            logging.getLogger(__name__).info(
                "main_agent_manifest_frozen tools=%d", len(self._tools)
            )
            return self._tools


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

    def post_commit_recovery_text(self) -> None:
        return None
