"""Management reaches the live plugin manager, including lifecycle failure evidence."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot import __version__
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryService,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_start", [False, True])
async def test_control_lifecycle_uses_running_manager_and_never_replays_start(
    database, tmp_path, fail_start
):
    root = tmp_path / "plugins" / "test.lifecycle"
    root.mkdir(parents=True)
    root.joinpath("plugin.toml").write_text(
        f'''
id = "test.lifecycle"
name = "Lifecycle fixture"
version = "1.0.0"
description = "Offline lifecycle test"
entrypoint = "fixture:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3.8"
permissions = []
''',
        encoding="utf-8",
    )
    root.joinpath("fixture.py").write_text(
        """
class Fixture:
    async def register(self, registrar):
        pass
    async def start(self, context):
        context.starts += 1
        if context.fail_start:
            raise RuntimeError("fixture start failed")
    async def stop(self):
        pass
""",
        encoding="utf-8",
    )
    host_context = SimpleNamespace(starts=0, fail_start=fail_start)
    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(tmp_path / "plugins", yuki_version=__version__),
        installations=PluginInstallationRepository(database),
        loader=PluginLoader(),
        extensions=ExtensionRegistry(),
        event_bus=PluginEventBus(),
        context_factory=lambda *_: host_context,
    )
    commands = ControlCommandService(ControlCommandAdapter(database, plugins=manager))
    queries = ControlQueryService(ControlQueryAdapter(database, plugins=manager))
    ctx = context("control.plugin.read", "control.plugin.mutate")
    await manager.start()
    try:
        initial = (await queries.list_plugins(ctx, PageRequest())).items[0]
        discovered = dict(manager._available)
        manager._available.clear()
        rejected = ControlCommand(
            request_id=ctx.request_id,
            expected_revision=initial.revision,
            payload={"action": "enable", "resource_id": initial.plugin_id},
        )
        for _ in range(2):
            with pytest.raises(ControlCommandError) as exc:
                await commands.mutate_plugin(ctx, rejected)
            assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
        assert host_context.starts == 0
        manager._available.update(discovered)
        ctx = replace(ctx, request_id=RequestId.new())
        approve = await commands.mutate_plugin(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=initial.revision,
                payload={
                    "action": "approve",
                    "resource_id": initial.plugin_id,
                    "spec": {"permissions": []},
                },
            ),
        )
        ctx = replace(ctx, request_id=RequestId.new())
        command = ControlCommand(
            request_id=ctx.request_id,
            expected_revision=approve.revision,
            payload={"action": "enable", "resource_id": initial.plugin_id},
        )
        if fail_start:
            for _ in range(2):
                with pytest.raises(ControlCommandError):
                    await commands.mutate_plugin(ctx, command)
            assert host_context.starts == 1
            runtime = await queries.read_plugin_runtime(ctx, initial.plugin_id)
            assert not runtime.running and runtime.status == "failed"
        else:
            enabled = await commands.mutate_plugin(ctx, command)
            assert enabled.success and enabled.effective_state["status"] == "running"
            assert await commands.mutate_plugin(ctx, command) == enabled
            runtime = await queries.read_plugin_runtime(ctx, initial.plugin_id)
            assert runtime.running and runtime.approval_valid
            assert host_context.starts == 1
            ctx = replace(ctx, request_id=RequestId.new())
            await commands.mutate_plugin(
                ctx,
                ControlCommand(
                    request_id=ctx.request_id,
                    expected_revision=enabled.revision,
                    payload={"action": "disable", "resource_id": initial.plugin_id},
                ),
            )
            assert not (await queries.read_plugin_runtime(ctx, initial.plugin_id)).running
    finally:
        await manager.stop()
