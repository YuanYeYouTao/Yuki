"""One deployment manifest and global short-state snapshot for every Yuki entrypoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from qq_ai_bot.codemode.contract import CODE_API_REVISION
from qq_ai_bot.codemode.tool_visibility import DIRECT_TOOL_NAMES, LOOKUP_TOOLS, model_definitions
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.work_control import work_control_tools
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.workspace.short_state import STATE_TOOL, ShortState

if TYPE_CHECKING:
    from qq_ai_bot.codemode.api_projection import ScriptApi


class MainAgentContract:
    def __init__(self, chat: Any, state: ShortState, *, code_enabled: bool = False) -> None:
        self.chat, self.state = chat, state
        self.mode = "code" if code_enabled else "direct"
        self._tools: tuple[ChatTool, ...] | None = None
        self.revision = ""
        self.script_api: ScriptApi | None = None
        self.plugin_contracts: dict[str, str] = {}
        self._stale_plugins: set[str] = set()
        self._lock = asyncio.Lock()

    def health(self) -> dict[str, object]:
        """Inspect the deployed declaration without refreshing tools or their metadata."""
        from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
        from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS

        # Health reflects a hot upgrade immediately while keeping declarations fixed.
        for name in self.plugin_contracts:
            self.plugin_binding_current(name)
        names = {tool.name for tool in self._tools or ()}
        return {
            "frozen": self._tools is not None,
            "revision": self.revision,
            "tool_count": len(names),
            "model_tool_count": len(names & DIRECT_TOOL_NAMES)
            if self.mode == "code"
            else len(names),
            "mode": self.mode,
            "restart_required": bool(self._stale_plugins),
            "persistent_environment_tools_complete": (SANDBOX_TOOLS | WORKSPACE_TOOLS) <= names,
        }

    async def definitions(self) -> tuple[ChatTool, ...]:
        """Full execution contract; the Provider uses model_definitions instead."""
        async with self._lock:
            if self._tools is not None:
                return deepcopy(self._tools)
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
            tools = [
                entry.descriptor.as_chat_tool(description=entry.descriptor.description)
                for entry in registry.catalog(declaration).entries
            ]
            tools.append(STATE_TOOL)
            from qq_ai_bot.runtime.subagent_tools import subagent_tools

            tools.extend(work_control_tools())
            tools.extend(subagent_tools())
            from qq_ai_bot.codemode.contract import EXECUTE_CODE_TOOL

            # Code composition is calling syntax over this same frozen manifest.
            if self.mode == "code":
                tools.append(EXECUTE_CODE_TOOL)
                tools.append(LOOKUP_TOOLS)
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("duplicate Main Agent manifest tool")
            frozen = deepcopy(tuple(sorted(tools, key=lambda item: item.name)))
            adapter = self.chat._plugin_tools
            fingerprint = getattr(adapter, "contract_fingerprint", None)
            if callable(fingerprint):
                self.plugin_contracts = {
                    tool.name: value
                    for tool in frozen
                    if (value := fingerprint(tool.name)) is not None
                }
            revision = hashlib.sha256(
                json.dumps(
                    {
                        # 14: retired MCP and fixed direct view, discovery and full execution API.
                        "version": 16,
                        "mode": self.mode,
                        "code_api": CODE_API_REVISION,
                        "direct_names": sorted(DIRECT_TOOL_NAMES),
                        "plugin_contracts": self.plugin_contracts,
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
            from qq_ai_bot.codemode.api_projection import project

            if self.mode == "code":
                self.script_api = project(frozen, revision)
            logging.getLogger(__name__).info(
                "main_agent_manifest_frozen tools=%d revision=%s", len(self._tools), self.revision
            )
            return deepcopy(self._tools)

    async def model_definitions(self) -> tuple[ChatTool, ...]:
        return model_definitions(await self.definitions(), enabled=self.mode == "code")

    def plugin_binding_current(self, name: str) -> bool:
        expected = self.plugin_contracts.get(name)
        if expected is None:
            return True
        fingerprint = getattr(self.chat._plugin_tools, "contract_fingerprint", None)
        actual = fingerprint(name) if callable(fingerprint) else None
        if actual is not None and actual != expected:
            self._stale_plugins.add(name)
        return actual == expected
