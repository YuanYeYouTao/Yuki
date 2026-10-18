"""Approved plugin definitions remain configurable independently of runtime instances."""

from pathlib import Path

import pytest

from yuki import __version__
from yuki.plugin_host.config import BoundConfigFacade
from yuki.plugin_host.configuration_service import (
    PluginConfigurationError,
    PluginConfigurationService,
)
from yuki.plugin_host.discovery import PluginDiscovery
from yuki.plugin_host.event_bus import PluginEventBus
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.plugin_host.loader import PluginLoader
from yuki.plugin_host.manager import PluginManagementRejected, PluginManager
from yuki.plugin_host.repository import PluginConfigRepository, PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.observation import PluginObservationRequest


def write_configured_plugin(root: Path, *, version: int = 1) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.toml").write_text(f'''id = "test.configured"
name = "Offline configurable fixture"
description = "Configuration lifecycle fixture"
version = "{version}.0.0"
entrypoint = "definition:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3.8"
permissions = ["plugin.config.read", "plugin.config.write", "tool.register"]
''')
    (root / "definition.py").write_text(f"""
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration
from yuki_plugin_sdk.models import RiskClass
from yuki_plugin_sdk.results import ToolResult

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    label: str = "version-{version}"

class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Fixture:
    async def register(self, registrar):
        registrar.register_config_schema(Settings)
        registrar.register_tool(ToolRegistration(
            ToolMetadata(name="inspect", description="Inspect this fixture", risk=RiskClass.READ),
            Arguments, ToolResult, self.inspect))

    async def start(self, context):
        self.context = context
        marker = Path(__file__).with_name("start-attempts.txt")
        with marker.open("a") as stream:
            stream.write("start\\n")
        if await context.config.get("endpoint") != "configured":
            raise ValueError("configuration needs repair")

    async def stop(self):
        self.context = None

    async def inspect(self, arguments):
        return ToolResult(data={{"endpoint": await self.context.config.get("endpoint")}})

    async def observe(self, context, request):
        return {{"endpoint": await context.get_config("endpoint")}}
""")


def manager_for(database, root):
    registry = ExtensionRegistry()
    repository = PluginInstallationRepository(database)
    configs = PluginConfigRepository(database)

    def context(manifest, permissions):
        return HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            services=PluginFacadeServices(
                approval_revision=manifest.manifest_hash,
                config_factory=lambda person, space: BoundConfigFacade(
                    repository=configs,
                    plugin_id=manifest.id,
                    approved_permissions=permissions,
                    schema=manager.configuration_schema(manifest.id),
                    current_user_id=person,
                    current_group_id=space,
                ),
            ),
        )

    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(root, yuki_version=__version__),
        installations=repository,
        loader=PluginLoader(),
        extensions=registry,
        event_bus=PluginEventBus(),
        context_factory=context,
    )
    return manager, registry, PluginConfigurationService(database, manager.configuration_schema)


async def save(service, values):
    original = await service.read("test.configured")
    await service.save(
        "test.configured",
        original["revision"],
        {"scope_type": "global", "owner_id": None, "values": values},
    )


async def test_configure_before_enable_after_disable_and_after_start_failure(database, tmp_path):
    root = tmp_path / "plugins"
    plugin = root / "test.configured"
    write_configured_plugin(plugin)
    manager, registry, service = manager_for(database, root)
    marker = plugin / "start-attempts.txt"
    await manager.start()
    try:
        assert manager.configuration_schema("test.configured") is None
        await manager.approve("test.configured", actor_user_id="operator")
        schema = manager.configuration_schema("test.configured")
        initial = await service.read("test.configured")
        assert not initial["valid"] and not marker.exists()
        assert not registry.list(kind=ExtensionKind.TOOL)
        with pytest.raises(PluginManagementRejected, match="observation_unavailable"):
            await manager.observe("test.configured", PluginObservationRequest())

        await save(service, {"endpoint": "configured"})
        assert not marker.exists() and manager.running_plugin_ids == ()
        await manager.enable("test.configured", actor_user_id="operator")
        assert manager.running_plugin_ids == ("test.configured",)
        assert await manager.observe("test.configured", PluginObservationRequest()) == {
            "endpoint": "configured"
        }
        assert registry.list(kind=ExtensionKind.TOOL)
        await manager.disable("test.configured", actor_user_id="operator")
        assert not registry.list() and manager.configuration_schema("test.configured") is schema
        attempts = marker.read_bytes()
        await save(service, {"endpoint": "invalid-at-start"})
        assert marker.read_bytes() == attempts
        failed = await manager.enable("test.configured", actor_user_id="operator")
        assert failed.last_error_category == "ValueError" and not manager.running_plugin_ids
        assert manager.configuration_schema("test.configured") is schema and not registry.list()
        with pytest.raises(PluginManagementRejected, match="observation_unavailable"):
            await manager.observe("test.configured", PluginObservationRequest())
        attempts = marker.read_bytes()
        await save(service, {"endpoint": "configured"})
        assert marker.read_bytes() == attempts
        await manager.enable("test.configured", actor_user_id="operator")
        assert manager.running_plugin_ids == ("test.configured",)
        await manager.disable("test.configured", actor_user_id="operator")
    finally:
        await manager.stop()

    # A cold Manager imports approved definitions without re-enabling the plugin.
    restored, registry, service = manager_for(database, root)
    attempts = marker.read_bytes()
    await restored.start()
    try:
        assert (await service.read("test.configured"))["values"]["endpoint"] == "configured"
        assert not restored.running_plugin_ids and not registry.list()
        assert marker.read_bytes() == attempts
    finally:
        await restored.stop()


async def test_changed_manifest_and_approval_invalidate_original_definition(database, tmp_path):
    root = tmp_path / "plugins"
    plugin = root / "test.configured"
    write_configured_plugin(plugin)
    manager, _, service = manager_for(database, root)
    await manager.start()
    try:
        await manager.approve("test.configured", actor_user_id="operator")
        await save(service, {"endpoint": "configured"})
        original = await service.read("test.configured")
        old_schema = manager.configuration_schema("test.configured")
        write_configured_plugin(plugin, version=2)
        await manager.discover()
        assert manager.configuration_schema("test.configured") is None
        with pytest.raises(PluginConfigurationError, match="operation_unavailable"):
            await service.read("test.configured")
        await manager.approve("test.configured", actor_user_id="operator")
        assert manager.configuration_schema("test.configured") is not old_schema
        assert (await service.read("test.configured"))["schema"]["properties"]["label"][
            "default"
        ] == "version-2"
        with pytest.raises(PluginConfigurationError, match="version_conflict"):
            await service.save(
                "test.configured",
                original["revision"],
                {"scope_type": "global", "owner_id": None, "values": {"endpoint": "configured"}},
            )
        await manager.approve(
            "test.configured", actor_user_id="operator", permissions=["tool.register"]
        )
        assert manager.configuration_schema("test.configured") is None
        assert not manager.running_plugin_ids
    finally:
        await manager.stop()
