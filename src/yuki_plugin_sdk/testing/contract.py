"""Minimal lifecycle and registration contract runner for local plugins."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.testing.fake_context import FakePluginContext


class PluginContractReport(StrictModel):
    plugin_id: str
    passed: bool
    checks: tuple[str, ...] = Field(default=())
    error_category: str | None = None


async def run_plugin_contract_tests(
    plugin_path: Path,
    *,
    yuki_version: str = "3.8.3",
) -> PluginContractReport:
    """Load, register, start, and stop one trusted local plugin without core services."""

    # Imports stay local so yuki_plugin_sdk itself remains independent of qq_ai_bot.
    from qq_ai_bot.plugin_host.approval import InMemoryApprovalStore, PluginApprovalService
    from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
    from qq_ai_bot.plugin_host.loader import PluginLoader
    from qq_ai_bot.plugin_host.manifest import load_manifest

    checks: list[str] = []
    loaded = None
    manifest = None
    loader = PluginLoader()
    try:
        manifest = load_manifest(plugin_path, yuki_version=yuki_version)
        checks.append("manifest")
        approvals = PluginApprovalService(InMemoryApprovalStore())
        approval = await approvals.approve(
            manifest,
            approved_by="plugin-contract-test",
        )
        checks.append("permissions")
        registry = ExtensionRegistry()
        registrar = registry.registrar(manifest.id, approval.approved_permissions)
        loaded = loader.load(plugin_path, manifest)
        checks.append("entrypoint")
        await loaded.instance.register(registrar)
        checks.append("register")
        context = FakePluginContext(manifest.id)
        await loaded.instance.start(context)
        checks.append("start")
        await loaded.instance.stop()
        checks.append("stop")
        loader.unload(loaded)
        return PluginContractReport(plugin_id=manifest.id, passed=True, checks=tuple(checks))
    except Exception as exc:
        if loaded is not None:
            try:
                await loaded.instance.stop()
            except Exception:
                pass
            loader.unload(loaded)
        return PluginContractReport(
            plugin_id=manifest.id if manifest is not None else plugin_path.name,
            passed=False,
            checks=tuple(checks),
            error_category=type(exc).__name__,
        )
