"""A failed background runner does not revoke independent plugin capabilities."""

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki import __version__
from yuki.persistence.database import write_failure_committed, write_failure_phase
from yuki.plugin_host.capability_adapter import PluginCapabilityAdapter
from yuki.plugin_host.db_models import PluginInstallationModel
from yuki.plugin_host.discovery import PluginDiscovery
from yuki.plugin_host.event_bus import PluginEventBus
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.plugin_host.loader import PluginLoader
from yuki.plugin_host.manager import PluginManager
from yuki.plugin_host.repository import PluginInstallationRepository, PluginStateRepository
from yuki.plugin_host.storage import BoundStorageFacade
from yuki.services.agent_tools import ToolRuntime
from yuki_plugin_sdk.api import PLUGIN_API_VERSION


@pytest.mark.parametrize("restart", ["NEVER", "ON_FAILURE"])
async def test_one_runner_failure_keeps_other_runner_and_tools(database, tmp_path, restart):
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
    await manager.start()
    try:
        await manager.approve(plugin, actor_user_id="9000")
        await manager.enable(plugin, actor_user_id="9000")
        instance = manager._running[plugin].loaded.instance
        failed_task = manager._running[plugin].background_tasks["bad"]
        async with asyncio.timeout(5):
            await instance.other_started.wait()
            instance.fail.set()
            with pytest.raises(RuntimeError, match="one independent runner failed"):
                await failed_task
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


@pytest.mark.parametrize("failure", ["execute", "commit", "cancel"])
async def test_stop_database_failure_still_joins_all_original_plugin_tasks(
    database, tmp_path, monkeypatch, failure
):
    root = tmp_path / "plugins"
    identities = ("test.shutdown-a", "test.shutdown-z")
    for identity in identities:
        directory = root / identity
        directory.mkdir(parents=True)
        (directory / "plugin.toml").write_text(
            f'''id = "{identity}"
name = "Shutdown ownership"
description = "Original database workers must exit"
version = "1.0.0"
entrypoint = "plugin:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3"
permissions = ["background.worker", "storage.private"]
[limits]
background_tasks = 2
''',
            encoding="utf-8",
        )
        (directory / "plugin.py").write_text(
            """import asyncio
from yuki_plugin_sdk.registrar import BackgroundServiceMetadata, BackgroundServiceRegistration

class Fixture:
    def __init__(self):
        self.started = {name: asyncio.Event() for name in ("host", "managed")}
        self.cancelled = set()
        self.stops = 0
    async def register(self, r):
        r.register_background_service(BackgroundServiceRegistration(
            BackgroundServiceMetadata(name="managed"), self.managed
        ))
    async def start(self, context):
        self.context = context
        context.scheduler.create_task("host", self.host)
    async def stop(self):
        self.stops += 1
    async def worker(self, name):
        await self.context.storage.set("shutdown", name, {"original": True})
        self.started[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.add(name)
    async def host(self):
        await self.worker("host")
    async def managed(self):
        await self.worker("managed")
""",
            encoding="utf-8",
        )
    registry = ExtensionRegistry()
    installations = PluginInstallationRepository(database)
    states = PluginStateRepository(database)
    contexts = {}

    def context(manifest, permissions):
        value = HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            services=PluginFacadeServices(
                storage=BoundStorageFacade(
                    repository=states, plugin_id=manifest.id, approved_permissions=permissions
                )
            ),
        )
        contexts[manifest.id] = value
        return value

    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(root, yuki_version=__version__),
        installations=installations,
        loader=PluginLoader(),
        extensions=registry,
        event_bus=PluginEventBus(),
        context_factory=context,
    )
    await manager.start()
    for identity in identities:
        await manager.approve(identity, actor_user_id="operator")
        await manager.enable(identity, actor_user_id="operator")
    instances = {identity: manager._running[identity].loaded.instance for identity in identities}
    tasks = [task for item in manager._running.values() for task in item.background_tasks.values()]
    tasks.extend(task for item in contexts.values() for task in item._scheduler._tasks.values())
    for instance in instances.values():
        async with asyncio.timeout(5):
            await instance.started["host"].wait()
            await instance.started["managed"].wait()
    commit = AsyncSession.commit
    lost = False

    async def lost_ack(session):
        nonlocal lost
        stopping = await session.scalar(
            select(PluginInstallationModel.plugin_id).where(
                PluginInstallationModel.plugin_id == identities[-1],
                PluginInstallationModel.status == "stopping",
            )
        )
        await commit(session)
        if stopping and not lost:
            lost = True
            if failure == "cancel":
                raise asyncio.CancelledError("original_plugin_stop_cancel")
            raise OSError("original_plugin_stop_ack_lost")

    if failure == "execute":
        async with database.immediate_session() as writer:
            await writer.execute(
                text(
                    "CREATE TRIGGER reject_plugin_stop BEFORE UPDATE OF status "
                    "ON plugin_installations "
                    "WHEN NEW.status='stopping' AND NEW.plugin_id='test.shutdown-z' "
                    "BEGIN SELECT RAISE(ABORT,'actual_stopping_failed'); END"
                )
            )
    try:
        with monkeypatch.context() as fault:
            if failure != "execute":
                fault.setattr(AsyncSession, "commit", lost_ack)
            error = (
                IntegrityError
                if failure == "execute"
                else (asyncio.CancelledError if failure == "cancel" else OSError)
            )
            with pytest.raises(error) as failed:
                await manager.stop()
        assert write_failure_phase(failed.value) == (
            "execute" if failure == "execute" else "commit"
        )
        assert write_failure_committed(failed.value) is (False if failure == "execute" else None)
        assert not manager.running_plugin_ids
        assert not manager._definitions and not registry.list()
        assert all(task.done() for task in tasks)
        for identity, instance in instances.items():
            assert instance.stops == 1 and instance.cancelled == {"host", "managed"}
            assert contexts[identity]._closed
            record = await installations.get(identity)
            assert record.failure_count == 0 and record.last_error_category is None
            assert await states.get(plugin_id=identity, namespace="shutdown", key="managed")
    finally:
        async with database.immediate_session() as writer:
            await writer.execute(text("DROP TRIGGER IF EXISTS reject_plugin_stop"))
        await manager.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("failure", ["execute", "commit"])
async def test_start_metadata_database_failure_is_not_a_plugin_business_failure(
    database, tmp_path, monkeypatch, failure
):
    from tests.unit.test_plugin_offline_configuration import manager_for, write_configured_plugin

    root = tmp_path / "plugins"
    identity = "test.configured"
    write_configured_plugin(root / identity)
    manager, _registry, _service = manager_for(database, root)
    await manager.start()
    await manager.approve(identity, actor_user_id="operator")
    contexts = []
    factory = manager._context_factory

    def capture(manifest, permissions):
        context = factory(manifest, permissions)
        contexts.append(context)
        return context

    monkeypatch.setattr(manager, "_context_factory", capture)
    commit = AsyncSession.commit

    async def lost_ack(session):
        starting = await session.scalar(
            select(PluginInstallationModel.plugin_id).where(
                PluginInstallationModel.plugin_id == identity,
                PluginInstallationModel.status == "starting",
            )
        )
        await commit(session)
        if starting:
            raise OSError("original_starting_commit_ack_lost")

    if failure == "execute":
        async with database.immediate_session() as writer:
            await writer.execute(
                text(
                    "CREATE TRIGGER reject_plugin_start BEFORE UPDATE OF status "
                    "ON plugin_installations WHEN NEW.status='starting' "
                    "BEGIN SELECT RAISE(ABORT,'actual_starting_failed'); END"
                )
            )
    try:
        with monkeypatch.context() as fault:
            if failure == "commit":
                fault.setattr(AsyncSession, "commit", lost_ack)
            with pytest.raises(IntegrityError if failure == "execute" else OSError) as failed:
                await manager.enable(identity, actor_user_id="operator")
        assert write_failure_phase(failed.value) == (
            "execute" if failure == "execute" else "commit"
        )
        assert contexts and all(context._closed for context in contexts)
        assert not manager.running_plugin_ids
        record = await manager.show(identity)
        assert record.enabled and record.failure_count == 0 and record.last_error_category is None
    finally:
        async with database.immediate_session() as writer:
            await writer.execute(text("DROP TRIGGER IF EXISTS reject_plugin_start"))
        await manager.stop()


async def test_start_cancel_keeps_original_error_and_closes_other_plugins_after_database_failure(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_plugin_offline_configuration import manager_for, write_configured_plugin

    root = tmp_path / "plugins"
    identities = ("test.configured-a", "test.configured-z")
    for identity in identities:
        directory = root / identity
        write_configured_plugin(directory)
        manifest = directory / "plugin.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace("test.configured", identity),
            encoding="utf-8",
        )
    manager, registry, service = manager_for(database, root)
    await manager.start()
    for identity in identities:
        await manager.approve(identity, actor_user_id="operator")
        configured = await service.read(identity)
        await service.save(
            identity,
            configured["revision"],
            {"scope_type": "global", "owner_id": None, "values": {"endpoint": "configured"}},
        )
        await manager._installations.set_enabled(identity, enabled=True)
    contexts = []
    factory = manager._context_factory

    def capture(manifest, permissions):
        context = factory(manifest, permissions)
        contexts.append(context)
        return context

    original = asyncio.CancelledError("original_plugin_start_cancel")
    start = manager._start_one_unlocked

    async def cancelled_start(identity):
        result = await start(identity)
        assert result is True
        if identity == identities[-1]:
            raise original
        return result

    monkeypatch.setattr(manager, "_context_factory", capture)
    monkeypatch.setattr(manager, "_start_one_unlocked", cancelled_start)
    async with database.immediate_session() as writer:
        await writer.execute(
            text(
                "CREATE TRIGGER reject_plugin_stop BEFORE UPDATE OF status ON plugin_installations "
                "WHEN NEW.status='stopping' AND NEW.plugin_id='test.configured-z' "
                "BEGIN SELECT RAISE(ABORT,'actual_cancel_cleanup_failed'); END"
            )
        )
    try:
        with pytest.raises(asyncio.CancelledError) as failed:
            await manager.start()
        assert failed.value is original
        assert any(note.startswith("plugin_start_cleanup_failed:") for note in original.__notes__)
        assert not manager.running_plugin_ids and not manager._definitions
        assert not registry.list()
        assert len(contexts) == 2 and all(context._closed for context in contexts)
    finally:
        async with database.immediate_session() as writer:
            await writer.execute(text("DROP TRIGGER IF EXISTS reject_plugin_stop"))
        await manager.stop()


@pytest.mark.parametrize("failure", ["approved", "repeat_cancel"])
async def test_cancel_during_real_plugin_start_finishes_original_cleanup(
    database, tmp_path, monkeypatch, failure
):
    from tests.unit.test_plugin_offline_configuration import (
        manager_for,
        save,
        write_configured_plugin,
    )

    root = tmp_path / "plugins"
    identity = "test.configured"
    write_configured_plugin(root / identity)
    manifest = root / identity / "plugin.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '"tool.register"]', '"tool.register", "background.worker"]'
        ),
        encoding="utf-8",
    )
    manager, registry, service = manager_for(database, root)
    await manager.start()
    await manager.approve(identity, actor_user_id="operator")
    await save(service, {"endpoint": "configured"})
    await manager._installations.set_enabled(identity, enabled=True)
    definition = manager._definitions[identity].loaded
    implementation = type(definition.instance)
    real_start = implementation.start
    real_stop = implementation.stop
    started = asyncio.Event()
    stopping = asyncio.Event()
    finish_stop = asyncio.Event()
    stopped = asyncio.Event()
    contexts = []
    host_tasks = []

    async def start(instance, context):
        await real_start(instance, context)
        contexts.append(context)

        async def host():
            started.set()
            await asyncio.Event().wait()

        handle = context.scheduler.create_task("original-host", host)
        host_tasks.append(context._scheduler._tasks[handle])
        await asyncio.Event().wait()

    async def stop(instance):
        stopping.set()
        await finish_stop.wait()
        await real_stop(instance)
        stopped.set()

    monkeypatch.setattr(implementation, "start", start)
    monkeypatch.setattr(implementation, "stop", stop)
    if failure == "approved":
        async with database.immediate_session() as writer:
            await writer.execute(
                text(
                    "CREATE TRIGGER reject_plugin_cancel_cleanup BEFORE UPDATE OF status "
                    "ON plugin_installations WHEN NEW.status='approved' "
                    "BEGIN SELECT RAISE(ABORT,'actual_approved_cleanup_failed'); END"
                )
            )
    pending = asyncio.create_task(manager.start())
    try:
        async with asyncio.timeout(5):
            await started.wait()
            pending.cancel("original_start_cancel")
            await stopping.wait()
            if failure == "repeat_cancel":
                pending.cancel("later_cancel_one")
                await asyncio.sleep(0)
                pending.cancel("later_cancel_two")
                await asyncio.sleep(0)
                assert not pending.done()
            finish_stop.set()
            with pytest.raises(asyncio.CancelledError) as failed:
                await pending
        assert failed.value.args == ("original_start_cancel",)
        assert stopped.is_set()
        assert contexts and all(context._closed for context in contexts)
        assert all(task.done() for task in host_tasks)
        assert not manager.running_plugin_ids and not manager._definitions and not registry.list()
        if failure == "approved":
            assert "plugin_start_cleanup_failed:IntegrityError" in failed.value.__notes__
    finally:
        finish_stop.set()
        async with database.immediate_session() as writer:
            await writer.execute(text("DROP TRIGGER IF EXISTS reject_plugin_cancel_cleanup"))
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()
