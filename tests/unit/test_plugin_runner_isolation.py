"""A failed background runner does not revoke independent plugin capabilities."""

import asyncio

import pytest

from yuki import __version__
from yuki.plugin_host.capability_adapter import PluginCapabilityAdapter
from yuki.plugin_host.discovery import PluginDiscovery
from yuki.plugin_host.event_bus import PluginEventBus
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext
from yuki.plugin_host.loader import PluginLoader
from yuki.plugin_host.manager import PluginManager
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.services.agent_tools import ToolRuntime
from yuki_plugin_sdk.api import PLUGIN_API_VERSION


@pytest.mark.parametrize("restart", ["NEVER", "ON_FAILURE"])
async def test_one_runner_failure_keeps_other_runner_and_tools(
    database, tmp_path, monkeypatch, restart
):
    plugin = "test.runner-isolation"
    root = tmp_path / "plugins"
    directory = root / plugin
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(
        f'''id = "{plugin}"
name = "Runner isolation"
description = "Independent runner lifecycle fixture"
version = "1.0.0"
entrypoint = "plugin:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3"
permissions = ["tool.register", "background.worker"]
[limits]
background_tasks = 2
''',
        encoding="utf-8",
    )
    (directory / "plugin.py").write_text(
        """import asyncio
from yuki_plugin_sdk.models import RestartPolicy, StrictModel
from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata, BackgroundServiceRegistration, ToolMetadata, ToolRegistration
)
from yuki_plugin_sdk.results import ToolResult

class Arguments(StrictModel):
    pass

class Fixture:
    def __init__(self):
        self.fail = asyncio.Event()
        self.other_started = asyncio.Event()
        self.restarted = asyncio.Event()
        self.other_cancelled = False
        self.stops = self.bad_runs = 0
    async def register(self, r):
        r.register_tool(ToolRegistration(
            ToolMetadata(name="okay", description="Independent read"),
            Arguments, ToolResult, self.okay
        ))
        r.register_background_service(BackgroundServiceRegistration(
            BackgroundServiceMetadata(name="bad", restart_policy=RestartPolicy.POLICY), self.bad
        ))
        r.register_background_service(BackgroundServiceRegistration(
            BackgroundServiceMetadata(name="other"), self.other
        ))
    async def start(self, context):
        pass
    async def stop(self):
        self.stops += 1
    async def okay(self, arguments):
        return ToolResult(data={"original": "result"})
    async def bad(self):
        self.bad_runs += 1
        if self.bad_runs == 1:
            await self.fail.wait()
            raise RuntimeError("one independent runner failed")
        self.restarted.set()
        await asyncio.Event().wait()
    async def other(self):
        self.other_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.other_cancelled = True
""".replace("RestartPolicy.POLICY", f"RestartPolicy.{restart}"),
        encoding="utf-8",
    )
    registry = ExtensionRegistry()
    installations = PluginInstallationRepository(database)
    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(root, yuki_version=__version__),
        installations=installations,
        loader=PluginLoader(),
        extensions=registry,
        event_bus=PluginEventBus(),
        context_factory=lambda manifest, permissions: HostPluginContext(
            plugin_id=manifest.id, approved_permissions=permissions
        ),
        failure_disable_threshold=1,
    )
    settled = asyncio.Event()
    handle_failure = manager._handle_background_failure

    async def handled(*args):
        try:
            await handle_failure(*args)
        finally:
            settled.set()

    monkeypatch.setattr(manager, "_handle_background_failure", handled)
    await manager.start()
    try:
        await manager.approve(plugin, actor_user_id="9000")
        await manager.enable(plugin, actor_user_id="9000")
        instance = manager._running[plugin].loaded.instance
        async with asyncio.timeout(5):
            await instance.other_started.wait()
            instance.fail.set()
            await settled.wait()
            if restart == "ON_FAILURE":
                await instance.restarted.wait()
        assert manager.running_plugin_ids == (plugin,)
        assert not instance.other_cancelled and instance.stops == 0
        assert instance.bad_runs == (2 if restart == "ON_FAILURE" else 1)
        record = await installations.get(plugin)
        assert record.enabled and record.status == "running" and record.failure_count == 0
        (tool,) = registry.list(kind=ExtensionKind.TOOL)
        adapter = PluginCapabilityAdapter(
            registry=registry,
            installations=installations,
            is_running=lambda identity: identity in manager.running_plugin_ids,
        )
        result = await adapter.execute(tool.model_name, "{}", ToolRuntime(inbound=None))
        assert result.ok and result.data == {"original": "result"}
    finally:
        await manager.stop()
    assert instance.other_cancelled and instance.stops == 1
