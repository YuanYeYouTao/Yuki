"""Two frozen entry contracts and one global short-state snapshot per deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from yuki.codemode.contract import CODE_API_REVISION
from yuki.codemode.tool_visibility import DIRECT_TOOL_NAMES, LOOKUP_TOOLS, model_definitions
from yuki.domain.messages import ChatTool
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.runtime.work_control import work_control_tools
from yuki.services.agent_tools import ToolRuntime
from yuki.workspace.short_state import STATE_TOOL, ShortState

if TYPE_CHECKING:
    from yuki.codemode.api_projection import ScriptApi


class MainAgentContract:
    def __init__(self, chat: Any, state: ShortState, *, code_enabled: bool = False) -> None:
        self.chat, self.state = chat, state
        self.mode = "code" if code_enabled else "direct"
        self._tools: tuple[ChatTool, ...] | None = None
        self.revision = ""
        self.app_revision = ""
        self._app_tools: tuple[ChatTool, ...] | None = None
        self.app_script_api: ScriptApi | None = None
        self.script_api: ScriptApi | None = None
        self.plugin_contracts: dict[str, str] = {}
        self._stale_plugins: set[str] = set()
        self._lock = asyncio.Lock()

    def health(self) -> dict[str, object]:
        """Inspect the deployed declaration without refreshing tools or their metadata."""
        from yuki.sandbox.environment_tools import SANDBOX_TOOLS
        from yuki.workspace.tools import WORKSPACE_TOOLS

        # Health reflects a hot upgrade immediately while keeping declarations fixed.
        for name in self.plugin_contracts:
            self.plugin_binding_current(name)
        names = {tool.name for tool in self._tools or ()}
        return {
            "frozen": self._tools is not None,
            "revision": self.revision,
            "entry_revisions": {"platform": self.revision, "app": self.app_revision},
            "tool_count": len(names),
            "model_tool_count": len(names & DIRECT_TOOL_NAMES)
            if self.mode == "code"
            else len(names),
            "mode": self.mode,
            "restart_required": bool(self._stale_plugins),
            "persistent_environment_tools_complete": (SANDBOX_TOOLS | WORKSPACE_TOOLS) <= names,
        }

    async def definitions(self, entry: str = "platform") -> tuple[ChatTool, ...]:
        """Full execution contract; the Provider uses model_definitions instead."""
        async with self._lock:
            if self._tools is not None:
                assert self._app_tools is not None
                return deepcopy(self._app_tools if entry == "app" else self._tools)
            # No event/person/group can affect declaration. This runtime is NEVER used to execute.
            config = await self.chat._runtime_config.snapshot()
            declaration = ToolRuntime(
                inbound=None,
                declaration_only=True,
                runtime_config=config,
            )
            registry = self.chat._build_tool_registry(declaration)
            tools = [
                entry.descriptor.as_chat_tool(description=entry.descriptor.description)
                for entry in registry.catalog(declaration).entries
            ]
            tools.append(STATE_TOOL)
            from yuki.runtime.subagent_tools import subagent_tools

            tools.extend(work_control_tools())
            tools.extend(subagent_tools())
            from yuki.codemode.contract import EXECUTE_CODE_TOOL

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
                        # 19: lifecycle decisions settle after the original tool round.
                        # 24: optional illustrations are accepted separately from text delivery.
                        # 25: same-entry historical references retain their exact event identity.
                        # 26: persistent computer control and original-run assistance guidance.
                        "version": 26,
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
            from yuki.services.personal_entry_tool import ENTRY_TOOL

            self._app_tools = frozen
            self._tools = tuple(tool for tool in frozen if tool.name != ENTRY_TOOL.name)
            self.app_revision = hashlib.sha256((revision + ":app").encode()).hexdigest()
            self.revision = hashlib.sha256((revision + ":platform").encode()).hexdigest()
            from yuki.codemode.api_projection import project

            if self.mode == "code":
                self.script_api = project(self._tools, self.revision)
                self.app_script_api = project(self._app_tools, self.app_revision)
            logging.getLogger(__name__).info(
                "main_agent_manifest_frozen tools=%d revision=%s", len(self._tools), self.revision
            )
            return deepcopy(self._app_tools if entry == "app" else self._tools)

    async def model_definitions(self, entry: str = "platform") -> tuple[ChatTool, ...]:
        return model_definitions(await self.definitions(entry), enabled=self.mode == "code")

    async def entry_for(self, conversation_id: str | None) -> str:
        from sqlalchemy import select

        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.identity.db_models import IdentityBindingModel

        if conversation_id is None:
            return "platform"
        async with self.chat._ledger._database.sessions() as session:
            platform = await session.scalar(
                select(IdentityBindingModel.platform)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.identity_binding_id == IdentityBindingModel.id,
                )
                .where(CanonicalConversationModel.id == conversation_id)
            )
        return "app" if platform in LOCAL_CLIENT_PLATFORMS else "platform"

    def plugin_binding_current(self, name: str) -> bool:
        expected = self.plugin_contracts.get(name)
        if expected is None:
            return True
        fingerprint = getattr(self.chat._plugin_tools, "contract_fingerprint", None)
        actual = fingerprint(name) if callable(fingerprint) else None
        if actual is not None and actual != expected:
            self._stale_plugins.add(name)
        return actual == expected
