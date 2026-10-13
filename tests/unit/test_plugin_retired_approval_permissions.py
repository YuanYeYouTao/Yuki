"""Retired inert grants cannot break startup or hide an unknown active grant."""

import json

import pytest
from sqlalchemy import select
from tests.integration.test_plugin_frozen_binding import write_plugin

from qq_ai_bot import __version__
from qq_ai_bot.plugin_host.db_models import PluginInstallationModel
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.parametrize("unknown", [False, True])
async def test_historical_retired_permissions_keep_actual_approval_boundary(
    database, tmp_path, unknown
):
    write_plugin(tmp_path / "plugins" / "test.contract", 1, "READ", "value")
    installations = PluginInstallationRepository(database)
    contexts = []

    def context_factory(manifest, permissions):
        contexts.append(permissions)
        return HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            services=PluginFacadeServices(approval_revision=manifest.manifest_hash),
        )

    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(tmp_path / "plugins", yuki_version=__version__),
        installations=installations,
        loader=PluginLoader(),
        extensions=ExtensionRegistry(),
        event_bus=PluginEventBus(),
        context_factory=context_factory,
    )
    try:
        await manager.start()
        await manager.approve("test.contract", actor_user_id="9000")
        original = await installations.get("test.contract")
        historical = ["tool.register", "runtime.config.read", "runtime.config.write"]
        if unknown:
            historical.append("unimplemented.future.permission")
        async with database.immediate_session() as writer:
            row = await writer.scalar(select(PluginInstallationModel))
            row.approved_permissions_json = json.dumps(historical)
        await manager.enable("test.contract", actor_user_id="9000")
        current = await installations.get("test.contract")
        assert current.manifest_hash == original.manifest_hash
        assert set(current.approved_permissions) == set(historical)
        if unknown:
            assert manager.running_plugin_ids == ()
            assert current.last_error_category == "invalid_approved_permission"
            assert contexts == []
        else:
            assert manager.running_plugin_ids == ("test.contract",)
            assert contexts == [frozenset({PluginPermission.TOOL_REGISTER})]
    finally:
        await manager.stop()


@pytest.mark.parametrize("change", ["old_api", "retired_read", "retired_write"])
def test_old_api_or_retired_permission_is_rejected_during_discovery(tmp_path, change):
    root = tmp_path / "plugins" / "test.contract"
    write_plugin(root, 1, "READ", "value")
    manifest = root / "plugin.toml"
    text = manifest.read_text()
    if change == "old_api":
        text = text.replace('plugin_api = "3.5"', 'plugin_api = "3.4"')
    else:
        permission = "runtime.config.read" if change == "retired_read" else "runtime.config.write"
        text = text.replace('permissions = ["tool.register"]', f'permissions = ["{permission}"]')
    manifest.write_text(text)
    (found,) = PluginDiscovery(tmp_path / "plugins", yuki_version=__version__).discover()
    assert found.manifest is None
    assert found.record.error_category == "manifest_invalid"
