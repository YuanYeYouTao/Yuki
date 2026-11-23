"""One frozen tool contract and one global short-state snapshot per deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from yuki.capabilities.validation import CapabilitySchemaView, JsonSchemaCapabilityValidator
from yuki.domain.messages import ChatMessage, ChatTool
from yuki.runtime.work_control import work_control_tools
from yuki.services.agent_tools import ToolRuntime
from yuki.workspace.short_state import STATE_TOOL, ShortState

if TYPE_CHECKING:
    from yuki.services.prompt_composer import PromptComposer


class MainAgentContract:
    def __init__(self, chat: Any, state: ShortState) -> None:
        self.chat, self.state = chat, state
        self._tools: tuple[ChatTool, ...] | None = None
        self.schema_view: CapabilitySchemaView
        self.revision = ""
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
            "tool_count": len(names),
            "restart_required": bool(self._stale_plugins),
            "persistent_environment_tools_complete": (SANDBOX_TOOLS | WORKSPACE_TOOLS) <= names,
        }

    def invariant_messages(self) -> tuple[ChatMessage, ...]:
        composer: PromptComposer = self.chat._prompt_composer
        return composer.invariant_messages()

    async def definitions(self) -> tuple[ChatTool, ...]:
        """The frozen contract the Provider declares and every executor checks."""
        async with self._lock:
            if self._tools is not None:
                return deepcopy(self._tools)
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
                        # 25: Account mentions and prepared parts replace retired dispatch.
                        # 26: one history tool and one entry-independent manifest.
                        # 27: role-independent descriptions and the single SOUL contract.
                        # 28: permanent cross-entry history with explicit source labels.
                        # 29: unified send tool; publication stays an internal delivery step.
                        # 30: source filters and paged conversation entry discovery.
                        # 31: device-owned native sessions and the unified external Agent contract.
                        # 32: local-device file/system operations with original domain receipts.
                        # 33: retire local_device; native Agent connections use external_agent.
                        # 34: original terminal/file tools explicitly select a personal computer.
                        # 35: one-shot tool surface (27 tools, single target, plain results).
                        # 36: computer and browser tools (29 tools; schema frozen with device).
                        # 37: computer wake/sleep/snapshot/restore on Yuki's own computer.
                        # 38: computer scale/zoom/elements/element, modifiers, triple/middle
                        #     clicks, key repeat and paste on Yuki's own computer.
                        # 39: coding-agent actions return on registration; Claude queue
                        #     appends without steer; deny-only answers for unowned waits.
                        # 40: one subtask tool set; native agents are executors of
                        #     start_subtask (delegate/observe/takeover, fixed scope) and the
                        #     two coding-agent tools are retired without successor.
                        # 41: Code Mode retired; one direct declaration (#573).
                        "version": 41,
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
            validator = JsonSchemaCapabilityValidator()
            validator.admit_tools(frozen)
            self.schema_view = validator.freeze()
            self._tools = frozen
            self.revision = revision
            logging.getLogger(__name__).info(
                "main_agent_manifest_frozen tools=%d revision=%s", len(self._tools), self.revision
            )
            return deepcopy(self._tools)

    def plugin_binding_current(self, name: str) -> bool:
        expected = self.plugin_contracts.get(name)
        if expected is None:
            return True
        fingerprint = getattr(self.chat._plugin_tools, "contract_fingerprint", None)
        actual = fingerprint(name) if callable(fingerprint) else None
        if actual is not None and actual != expected:
            self._stale_plugins.add(name)
        return actual == expected
