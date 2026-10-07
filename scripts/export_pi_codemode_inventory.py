"""Export a synthetic deployment's actual frozen contract, without production configuration."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from qq_ai_bot.admin.capabilities import CapabilityRegistry
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog
from qq_ai_bot.codemode.tool_visibility import TOOL_LOOKUP_NAME
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.subagent_tools import subagent_tools
from qq_ai_bot.runtime.work_control import work_control_tools
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import STATE_TOOL, ShortState
from qq_ai_bot.workspace.store import WorkspaceStore

_CONTROL_BINDINGS = {
    tool.name: "qq_ai_bot.runtime.work_control.WorkControl.execute"
    for tool in (*work_control_tools(), *subagent_tools())
}
_CONTROL_BINDINGS[STATE_TOOL.name] = "qq_ai_bot.workspace.short_state.ShortState.execute"
_CONTROL_BINDINGS["execute_code"] = "qq_ai_bot.codemode.driver.CodeModeDriver.run"
_CONTROL_BINDINGS[TOOL_LOOKUP_NAME] = "qq_ai_bot.codemode.tool_visibility.lookup_tools"


def inventory_rows(
    tools: tuple[ChatTool, ...], catalog: UnifiedToolCatalog
) -> list[dict[str, Any]]:
    """Match every declaration; missing bindings fail instead of silently dropping tools."""
    rows = []
    for tool in tools:
        entry = catalog.by_model_name(tool.name)
        descriptor = entry.descriptor if entry is not None else None
        control_binding = _CONTROL_BINDINGS.get(tool.name)
        if descriptor is None and control_binding is None:
            raise ValueError(f"unmapped_manifest_tool:{tool.name}")
        if descriptor is not None and descriptor.binding is None:
            raise ValueError(f"missing_manifest_binding:{tool.name}")
        binding = descriptor.binding if descriptor is not None else None
        binding_name = (
            f"{type(binding).__module__}.{type(binding).__qualname__}.invoke"
            if binding is not None
            else control_binding
        )
        rows.append(
            {
                "model_name": tool.name,
                "canonical_name": descriptor.canonical_name if descriptor else tool.name,
                "provider_id": entry.provider_id if entry else "host_control",
                "binding": binding_name,
                "description": tool.description,
                "input_schema": tool.parameters,
                "output_schema": descriptor.output_schema or None if descriptor else None,
                "effect": descriptor.effect.value if descriptor else "host_control",
                "parallel_safe": descriptor.parallel_safe if descriptor else False,
                "result_cacheable": tool.result_cacheable,
                "allowed_origins": sorted(o.value for o in descriptor.allowed_origins)
                if descriptor
                else [],
                "required_permissions": sorted(descriptor.required_permissions)
                if descriptor
                else [],
                "schema_version": tool.schema_version,
                "idempotency": descriptor.idempotency.value if descriptor else "host_control",
                "parameter_classification": (
                    "resolve target descriptor at execution"
                    if descriptor and callable(getattr(binding, "target_descriptor", None))
                    else "descriptor plus domain execution checks"
                ),
                "receipt_owner": "WorkSession and original domain service",
                "acceptance_status": "not_run",
            }
        )
    return rows


class _DeclarationService:
    def __init__(self, tools: tuple[ChatTool, ...]) -> None:
        self.tools = tools

    def definitions(self) -> tuple[ChatTool, ...]:
        return self.tools

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("inventory must not execute a business tool")


async def export_inventory() -> dict[str, Any]:
    # Import test assembly explicitly. No application bootstrap, .env or deployment file is read.
    from tests.conftest import build_harness, make_settings

    with tempfile.TemporaryDirectory(prefix="yuki-manifest-fixture-") as directory:
        root = Path(directory)
        database = Database(f"sqlite+aiosqlite:///{root / 'fixture.db'}")
        try:
            await database.create_schema()
            settings = make_settings(
                database.url, web_enabled=True, tavily_api_key="synthetic-unused-fixture-key"
            )
            # Only the presence of a provider affects declaration. It is never invoked.
            harness = build_harness(database, settings, web_provider=SimpleNamespace())
            chat = harness.processor._chat
            chat.set_admin_tools(_DeclarationService(CapabilityRegistry().definitions()))
            automation = AutomationToolService(SimpleNamespace(enabled=True))
            chat.set_automation_tools(_DeclarationService(automation.definitions()))
            contract = MainAgentContract(chat, ShortState(WorkspaceStore(root / "state")))
            tools = await contract.definitions()
            config = await chat._runtime_config.snapshot()
            runtime = ToolRuntime(
                inbound=None,
                gateway=None,
                allow_generic_onebot=False,
                declaration_only=True,
                runtime_config=config,
            )
            catalog = chat._build_tool_registry(runtime, web_was_used=False).catalog(runtime)
            return {
                "fixture": "synthetic core/admin/automation declaration; no external service",
                "manifest_revision": contract.revision,
                "catalog_revision": catalog.revision,
                "tools": inventory_rows(tools, catalog),
                "frozen_definitions": [asdict(tool) for tool in tools],
                "model_definitions": [asdict(tool) for tool in await contract.model_definitions()],
                "external_inventory": {
                    "mcp": "deployment-dependent; synthetic binding tests required",
                    "plugin": "deployment-dependent; synthetic binding tests required",
                    "production_manifest": "not_collected; production access not authorized",
                },
            }
        finally:
            await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    payload = asyncio.run(export_inventory())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"exported {len(payload['tools'])} tools to {output}")


if __name__ == "__main__":
    main()
