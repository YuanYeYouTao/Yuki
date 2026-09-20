"""One deployment manifest and global short-state snapshot for every Yuki entrypoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from typing import Any

from qq_ai_bot.capabilities.request import request_tools_definition
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.work_control import work_control_tools
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.workspace.short_state import STATE_TOOL, ShortState


class MainAgentContract:
    def __init__(self, chat: Any, state: ShortState) -> None:
        self.chat, self.state = chat, state
        self._tools: tuple[ChatTool, ...] | None = None
        self.revision = ""
        self._lock = asyncio.Lock()

    def health(self) -> dict[str, object]:
        """Inspect the deployed declaration without refreshing tools or their metadata."""
        from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
        from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS

        names = {tool.name for tool in self._tools or ()}
        return {
            "frozen": self._tools is not None,
            "revision": self.revision,
            "tool_count": len(names),
            "persistent_environment_tools_complete": (SANDBOX_TOOLS | WORKSPACE_TOOLS) <= names,
            "netease_tools_present": any("netease" in name.casefold() for name in names),
        }

    async def definitions(self) -> tuple[ChatTool, ...]:
        async with self._lock:
            if self._tools is not None:
                return deepcopy(self._tools)
            from qq_ai_bot.services.main_agent_backend import _SET_REPLY_TARGET_TOOL

            # No event/person/group can affect declaration. This runtime is NEVER used to execute.
            config = await self.chat._runtime_config.snapshot()
            declaration = ToolRuntime(
                inbound=None,
                gateway=None,
                allow_generic_onebot=False,
                declaration_only=True,
                runtime_config=config,
            )
            for provider in self.chat._external_tool_providers:
                prepare = getattr(provider, "prepare_manifest", None)
                if callable(prepare):
                    await prepare(declaration)
                else:
                    await provider.refresh(force=False)
            registry = self.chat._build_tool_registry(declaration, web_was_used=False)
            tools = [
                entry.descriptor.as_chat_tool(description=entry.descriptor.description)
                for entry in registry.catalog(declaration).entries
            ]
            tools.extend((request_tools_definition(), _SET_REPLY_TARGET_TOOL, STATE_TOOL))
            from qq_ai_bot.runtime.subagent_tools import subagent_tools

            tools.extend(work_control_tools())
            tools.extend(subagent_tools())
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("duplicate Main Agent manifest tool")
            frozen = deepcopy(tuple(sorted(tools, key=lambda item: item.name)))
            revision = hashlib.sha256(
                json.dumps(
                    {
                        "version": 4,
                        "tools": [
                            {
                                "name": t.name,
                                "description": t.description,
                                "parameters": t.parameters,
                                "result_cacheable": t.result_cacheable,
                            }
                            for t in frozen
                        ],
                    },
                    ensure_ascii=False,
                    # Schema mapping order is part of the Provider token prefix.
                    sort_keys=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self._tools, self.revision = frozen, revision
            logging.getLogger(__name__).info(
                "main_agent_manifest_frozen tools=%d revision=%s", len(self._tools), self.revision
            )
            return deepcopy(self._tools)
