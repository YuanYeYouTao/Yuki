"""Persistent, failure-isolated lifecycle manager for trusted local plugins."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol

from qq_ai_bot.plugin_host.discovery import DiscoveredPlugin
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.loader import LoadedPlugin, PluginLoader
from qq_ai_bot.plugin_host.manifest import PluginManifest
from qq_ai_bot.plugin_host.repository import (
    PluginApprovalError,
    PluginAuditRepository,
    PluginInstallationRecord,
    PluginInstallationRepository,
)
from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.errors import PluginLifecycleError
from yuki_plugin_sdk.models import RestartPolicy, StrictModel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import (
    BackgroundServiceRegistration,
    EventHookRegistration,
)

logger = logging.getLogger(__name__)


class PluginDiscoverySource(Protocol):
    def discover(self) -> tuple[DiscoveredPlugin, ...]: ...


PluginContextFactory = Callable[
    [PluginManifest, frozenset[PluginPermission]],
    PluginContext | Awaitable[PluginContext],
]
PluginActivated = Callable[[PluginManifest], None]
PluginDeactivated = Callable[[str], None]


class PluginDoctorReport(StrictModel):
    plugin_id: str
    system_enabled: bool
    installed: bool
    manifest_available: bool
    manifest_hash_matches: bool
    approval_valid: bool
    enabled: bool
    running: bool
    status: str | None
    requested_permissions: tuple[str, ...] = ()
    approved_permissions: tuple[str, ...] = ()
    extension_count: int = 0
    background_task_count: int = 0
    problems: tuple[str, ...] = ()


def diagnose_plugin(
    plugin_id: str,
    *,
    system_enabled: bool,
    record: PluginInstallationRecord | None,
    available_hash: str | None = None,
    available_permissions: frozenset[str] | None = None,
    running: bool = False,
    extension_count: int = 0,
    background_task_count: int = 0,
) -> PluginDoctorReport:
    """Project installation, manifest, and runtime facts into a doctor report."""

    available = available_hash is not None
    manifest_matches = bool(
        record is not None and available and record.manifest_hash == available_hash
    )
    approval_valid = bool(
        record is not None
        and available
        and manifest_matches
        and record.approved_at is not None
        and available_permissions is not None
        and set(record.approved_permissions) <= set(available_permissions)
    )
    problems: list[str] = []
    if not system_enabled:
        problems.append("plugin_system_disabled")
    if record is None:
        problems.append("not_installed")
    if not available:
        problems.append("manifest_unavailable")
    elif record is not None and not manifest_matches:
        problems.append("manifest_hash_changed")
    if record is not None and not approval_valid:
        problems.append("approval_missing_or_stale")
    if record is not None and not record.enabled:
        problems.append("disabled")
    if record is not None and record.last_error_category:
        problems.append(f"last_error:{record.last_error_category}")
    return PluginDoctorReport(
        plugin_id=plugin_id,
        system_enabled=system_enabled,
        installed=record is not None,
        manifest_available=available,
        manifest_hash_matches=manifest_matches,
        approval_valid=approval_valid,
        enabled=record.enabled if record is not None else False,
        running=running,
        status=record.status if record is not None else None,
        requested_permissions=record.requested_permissions if record else (),
        approved_permissions=record.approved_permissions if record else (),
        extension_count=extension_count,
        background_task_count=background_task_count,
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class _AvailablePlugin:
    manifest: PluginManifest
    root: Path


@dataclass(slots=True)
class _ManagedPlugin:
    loaded: LoadedPlugin
    manifest: PluginManifest
    approved_permissions: frozenset[PluginPermission]
    context: PluginContext
    background_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)


class PluginManager:
    """Coordinate discovery, approval, loading, hooks, and managed tasks.

    The manager governs APIs for trusted in-process code; it is not an OS sandbox.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        discovery: PluginDiscoverySource,
        installations: PluginInstallationRepository,
        loader: PluginLoader,
        extensions: ExtensionRegistry,
        event_bus: PluginEventBus,
        context_factory: PluginContextFactory,
        on_activated: PluginActivated | None = None,
        on_deactivated: PluginDeactivated | None = None,
        audit: PluginAuditRepository | None = None,
        start_timeout_seconds: float = 10.0,
        stop_timeout_seconds: float = 10.0,
        background_task_limit: int = 4,
        failure_disable_threshold: int = 3,
    ) -> None:
        if start_timeout_seconds <= 0 or stop_timeout_seconds <= 0:
            raise ValueError("plugin lifecycle timeouts must be positive")
        if background_task_limit <= 0 or failure_disable_threshold <= 0:
            raise ValueError("plugin lifecycle limits must be positive")
        self._enabled = enabled
        self._discovery = discovery
        self._installations = installations
        self._loader = loader
        self._extensions = extensions
        self._event_bus = event_bus
        self._context_factory = context_factory
        self._on_activated = on_activated
        self._on_deactivated = on_deactivated
        self._audit = audit
        self._start_timeout = start_timeout_seconds
        self._stop_timeout = stop_timeout_seconds
        self._background_task_limit = background_task_limit
        self._failure_disable_threshold = failure_disable_threshold
        self._available: dict[str, _AvailablePlugin] = {}
        self._running: dict[str, _ManagedPlugin] = {}
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._active = False
        self._lock = asyncio.Lock()

    @property
    def system_enabled(self) -> bool:
        return self._enabled

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def running_plugin_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._running))

    async def discover(self) -> tuple[PluginInstallationRecord, ...]:
        async with self._lock:
            return await self._discover_unlocked()

    async def start(self) -> tuple[str, ...]:
        async with self._lock:
            if not self._enabled:
                self._active = False
                return ()
            self._active = True
            try:
                await self._discover_unlocked()
                for record in await self._installations.list_all():
                    if record.enabled:
                        await self._start_one_unlocked(record.plugin_id)
                return self.running_plugin_ids
            except asyncio.CancelledError:
                self._active = False
                for plugin_id in tuple(sorted(self._running, reverse=True)):
                    await self._stop_one_unlocked(plugin_id, final_status="approved")
                raise

    async def stop(self) -> None:
        async with self._lock:
            self._active = False
            for plugin_id in tuple(sorted(self._running, reverse=True)):
                record = await self._installations.get(plugin_id)
                final_status = "approved" if record is not None and record.enabled else "disabled"
                await self._stop_one_unlocked(plugin_id, final_status=final_status)
            maintenance = tuple(self._maintenance_tasks)
            self._maintenance_tasks.clear()
            for task in maintenance:
                task.cancel()
            if maintenance:
                await asyncio.gather(*maintenance, return_exceptions=True)

    async def approve(
        self,
        plugin_id: str,
        *,
        actor_user_id: str,
        permissions: Iterable[PluginPermission | str] | None = None,
    ) -> PluginInstallationRecord:
        async with self._lock:
            available = self._available.get(plugin_id)
            record = await self._installations.get(plugin_id)
            if available is None or record is None:
                await self._record_audit(
                    plugin_id,
                    actor_user_id=actor_user_id,
                    operation="approve",
                    success=False,
                    error_category="plugin_not_discovered",
                )
                raise PluginApprovalError("plugin is not currently discovered")
            if record.manifest_hash != available.manifest.manifest_hash:
                raise PluginApprovalError("plugin manifest changed; discover it again")
            selected = self._normalize_permissions(permissions)
            if selected is None:
                selected = tuple(permission.value for permission in available.manifest.permissions)
            requested = {permission.value for permission in available.manifest.permissions}
            if not set(selected) <= requested:
                await self._record_audit(
                    plugin_id,
                    actor_user_id=actor_user_id,
                    operation="approve",
                    success=False,
                    error_category="permission_not_requested",
                )
                raise PluginApprovalError("approved permissions must be requested by the plugin")
            if plugin_id in self._running and set(selected) != set(record.approved_permissions):
                await self._stop_one_unlocked(plugin_id, final_status=None)
                await self._installations.set_enabled(plugin_id, enabled=False)
            approved = await self._installations.approve(plugin_id, permissions=selected)
            if approved is None:
                raise PluginApprovalError("plugin is not installed")
            await self._record_audit(
                plugin_id,
                actor_user_id=actor_user_id,
                operation="approve",
                success=True,
                detail={"permission_count": len(approved.approved_permissions)},
            )
            return approved

    async def enable(self, plugin_id: str, *, actor_user_id: str) -> PluginInstallationRecord:
        async with self._lock:
            available = self._available.get(plugin_id)
            record = await self._installations.get(plugin_id)
            if available is None or record is None:
                raise PluginApprovalError("plugin is not currently discovered")
            if record.manifest_hash != available.manifest.manifest_hash:
                raise PluginApprovalError("plugin approval is stale")
            enabled = await self._installations.set_enabled(plugin_id, enabled=True)
            if enabled is None:
                raise PluginApprovalError("plugin is not installed")
            started = True
            if self._active and self._enabled:
                started = await self._start_one_unlocked(plugin_id)
            current = await self._installations.get(plugin_id)
            assert current is not None
            await self._record_audit(
                plugin_id,
                actor_user_id=actor_user_id,
                operation="enable",
                success=started,
                error_category=None if started else current.last_error_category,
            )
            return current

    async def disable(self, plugin_id: str, *, actor_user_id: str) -> PluginInstallationRecord:
        async with self._lock:
            record = await self._installations.get(plugin_id)
            if record is None:
                raise PluginApprovalError("plugin is not installed")
            await self._stop_one_unlocked(plugin_id, final_status=None)
            self._event_bus.unsubscribe_plugin(plugin_id)
            self._extensions.remove_plugin(plugin_id)
            disabled = await self._installations.set_enabled(plugin_id, enabled=False)
            assert disabled is not None
            await self._record_audit(
                plugin_id,
                actor_user_id=actor_user_id,
                operation="disable",
                success=True,
            )
            return disabled

    async def list(self) -> tuple[PluginInstallationRecord, ...]:
        return await self._installations.list_all()

    async def show(self, plugin_id: str) -> PluginInstallationRecord | None:
        return await self._installations.get(plugin_id)

    async def doctor(self, plugin_id: str) -> PluginDoctorReport:
        async with self._lock:
            record = await self._installations.get(plugin_id)
            available = self._available.get(plugin_id)
            managed = self._running.get(plugin_id)
            return diagnose_plugin(
                plugin_id,
                system_enabled=self._enabled,
                record=record,
                available_hash=(None if available is None else available.manifest.manifest_hash),
                available_permissions=(
                    None
                    if available is None
                    else frozenset(
                        permission.value for permission in available.manifest.permissions
                    )
                ),
                running=managed is not None,
                extension_count=len(self._extensions.list(plugin_id=plugin_id)),
                background_task_count=(len(managed.background_tasks) if managed is not None else 0),
            )

    async def _discover_unlocked(self) -> tuple[PluginInstallationRecord, ...]:
        if not self._enabled:
            return ()
        discovered = self._discovery.discover()
        old_records = {record.plugin_id: record for record in await self._installations.list_all()}
        available: dict[str, _AvailablePlugin] = {}
        persisted: list[PluginInstallationRecord] = []
        scanned_directories = {item.record.directory.name for item in discovered}
        for item in discovered:
            manifest = item.manifest
            if manifest is None:
                continue
            old = old_records.get(manifest.id)
            if (
                old is not None
                and old.manifest_hash != manifest.manifest_hash
                and manifest.id in self._running
            ):
                await self._stop_one_unlocked(manifest.id, final_status=None)
            record = await self._installations.upsert_discovered(
                plugin_id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                plugin_api=manifest.plugin_api,
                yuki_requires=manifest.yuki_requires,
                manifest_hash=manifest.manifest_hash,
                entrypoint=manifest.entrypoint,
                requested_permissions=(permission.value for permission in manifest.permissions),
            )
            available[manifest.id] = _AvailablePlugin(manifest, item.record.directory)
            persisted.append(record)
        for plugin_id in old_records.keys() - available.keys():
            if plugin_id in self._running:
                await self._stop_one_unlocked(plugin_id, final_status=None)
            await self._installations.set_enabled(plugin_id, enabled=False)
            error_category = (
                "manifest_invalid" if plugin_id in scanned_directories else "manifest_missing"
            )
            await self._installations.set_status(
                plugin_id,
                status="invalid",
                error_category=error_category,
            )
        self._available = available
        return tuple(persisted)

    async def _start_one_unlocked(self, plugin_id: str) -> bool:
        if plugin_id in self._running:
            return True
        available = self._available.get(plugin_id)
        record = await self._installations.get(plugin_id)
        if available is None or record is None:
            return False
        if not record.enabled or record.approved_at is None:
            return False
        if record.manifest_hash != available.manifest.manifest_hash:
            await self._installations.set_enabled(plugin_id, enabled=False)
            await self._installations.set_status(
                plugin_id,
                status="pending_approval",
                error_category="manifest_hash_changed",
            )
            return False
        try:
            approved = frozenset(
                PluginPermission(permission) for permission in record.approved_permissions
            )
        except ValueError:
            await self._record_start_failure(plugin_id, "invalid_approved_permission")
            return False
        if not approved <= set(available.manifest.permissions):
            await self._record_start_failure(plugin_id, "approval_scope_invalid")
            return False

        loaded: LoadedPlugin | None = None
        context: PluginContext | None = None
        start_called = False
        try:
            loaded = self._loader.load(available.root, available.manifest)
            registrar = self._extensions.registrar(plugin_id, approved)
            async with asyncio.timeout(self._start_timeout):
                await loaded.instance.register(registrar)
            event_hooks = self._event_registrations(plugin_id)
            background_services = self._background_registrations(plugin_id)
            self._validate_background_limits(available.manifest, background_services)
            for hook in event_hooks:
                self._event_bus.subscribe(
                    plugin_id=plugin_id,
                    hook_id=hook.metadata.id,
                    event=hook.metadata.event,
                    handler=hook.handler,
                    priority=hook.metadata.priority,
                    timeout_seconds=hook.metadata.timeout_seconds,
                )
            await self._installations.set_status(plugin_id, status="registered")
            context = await self._make_context(available.manifest, approved)
            await self._installations.set_status(plugin_id, status="starting")
            start_called = True
            async with asyncio.timeout(self._start_timeout):
                await loaded.instance.start(context)
            managed = _ManagedPlugin(loaded, available.manifest, approved, context)
            self._running[plugin_id] = managed
            if self._on_activated is not None:
                self._on_activated(available.manifest)
            await self._installations.set_status(plugin_id, status="running")
            for registration in background_services:
                self._start_background_task(plugin_id, registration)
            return True
        except asyncio.CancelledError:
            await self._rollback_start_unlocked(
                plugin_id,
                loaded,
                context=context,
                start_called=start_called,
            )
            await self._installations.set_status(plugin_id, status="approved")
            raise
        except Exception as exc:
            error_category = (
                "start_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            )
            logger.warning(
                "plugin_start_failed plugin_id=%s error_category=%s",
                plugin_id,
                error_category,
            )
            await self._rollback_start_unlocked(
                plugin_id,
                loaded,
                context=context,
                start_called=start_called,
            )
            await self._record_start_failure(plugin_id, error_category)
            return False

    async def _rollback_start_unlocked(
        self,
        plugin_id: str,
        loaded: LoadedPlugin | None,
        *,
        context: PluginContext | None,
        start_called: bool,
    ) -> None:
        managed = self._running.pop(plugin_id, None)
        if loaded is not None and start_called:
            try:
                async with asyncio.timeout(self._stop_timeout):
                    await loaded.instance.stop()
            except (Exception, asyncio.CancelledError):
                pass
        managed_context = managed.context if managed is not None else context
        if managed is not None:
            tasks = tuple(managed.background_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_context(managed_context)
        self._deactivate_extensions(plugin_id)
        self._event_bus.unsubscribe_plugin(plugin_id)
        self._extensions.remove_plugin(plugin_id)
        if loaded is not None:
            self._loader.unload(loaded)

    async def _stop_one_unlocked(
        self,
        plugin_id: str,
        *,
        final_status: str | None,
        record_stop_failure: bool = True,
    ) -> None:
        managed = self._running.pop(plugin_id, None)
        if managed is None:
            self._deactivate_extensions(plugin_id)
            self._event_bus.unsubscribe_plugin(plugin_id)
            self._extensions.remove_plugin(plugin_id)
            if final_status is not None:
                await self._installations.set_status(plugin_id, status=final_status)
            return
        await self._installations.set_status(plugin_id, status="stopping")
        error_category: str | None = None
        try:
            async with asyncio.timeout(self._stop_timeout):
                await managed.loaded.instance.stop()
        except TimeoutError:
            error_category = "stop_timeout"
        except Exception as exc:
            error_category = type(exc).__name__
        await self._close_context(managed.context)
        tasks = tuple(managed.background_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                async with asyncio.timeout(self._stop_timeout):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                error_category = error_category or "background_stop_timeout"
        self._deactivate_extensions(plugin_id)
        self._event_bus.unsubscribe_plugin(plugin_id)
        self._extensions.remove_plugin(plugin_id)
        self._loader.unload(managed.loaded)
        if error_category is not None and record_stop_failure:
            await self._installations.record_failure(
                plugin_id,
                error_category=error_category,
                disable_threshold=self._failure_disable_threshold,
            )
        elif final_status is not None:
            await self._installations.set_status(plugin_id, status=final_status)

    def _deactivate_extensions(self, plugin_id: str) -> None:
        callback = self._on_deactivated
        if callback is None:
            return
        try:
            callback(plugin_id)
        except Exception as exc:
            logger.warning(
                "plugin_extension_deactivate_failed plugin_id=%s error_category=%s",
                plugin_id,
                type(exc).__name__,
            )

    async def _close_context(self, context: PluginContext | None) -> None:
        if context is None:
            return
        close = getattr(context, "close_host_resources", None)
        if not callable(close):
            return
        try:
            async with asyncio.timeout(self._stop_timeout):
                await close()
        except Exception as exc:
            logger.warning(
                "plugin_context_stop_failed plugin_id=%s error_category=%s",
                context.plugin_id,
                type(exc).__name__,
            )

    def _start_background_task(
        self,
        plugin_id: str,
        registration: BackgroundServiceRegistration,
    ) -> None:
        managed = self._running.get(plugin_id)
        if managed is None:
            return
        name = registration.metadata.name
        task: asyncio.Task[None] = asyncio.create_task(
            self._invoke_background(registration),
            name=f"yuki-plugin:{plugin_id}:{name}",
        )
        managed.background_tasks[name] = task
        task.add_done_callback(partial(self._background_done, plugin_id, registration))

    @staticmethod
    async def _invoke_background(registration: BackgroundServiceRegistration) -> None:
        await registration.runner()

    def _background_done(
        self,
        plugin_id: str,
        registration: BackgroundServiceRegistration,
        task: asyncio.Task[None],
    ) -> None:
        managed = self._running.get(plugin_id)
        if managed is not None:
            current = managed.background_tasks.get(registration.metadata.name)
            if current is task:
                managed.background_tasks.pop(registration.metadata.name, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        maintenance = asyncio.create_task(
            self._handle_background_failure(plugin_id, registration, type(error).__name__),
            name=f"yuki-plugin-maintenance:{plugin_id}:{registration.metadata.name}",
        )
        self._maintenance_tasks.add(maintenance)
        maintenance.add_done_callback(self._maintenance_tasks.discard)

    async def _handle_background_failure(
        self,
        plugin_id: str,
        registration: BackgroundServiceRegistration,
        error_category: str,
    ) -> None:
        logger.warning(
            "plugin_background_failed plugin_id=%s service=%s error_category=%s",
            plugin_id,
            registration.metadata.name,
            error_category,
        )
        record = await self._installations.record_failure(
            plugin_id,
            error_category=error_category,
            disable_threshold=self._failure_disable_threshold,
        )
        async with self._lock:
            if plugin_id not in self._running:
                return
            if (
                record is not None
                and record.enabled
                and registration.metadata.restart_policy is RestartPolicy.ON_FAILURE
                and self._active
            ):
                self._start_background_task(plugin_id, registration)
                return
            await self._stop_one_unlocked(
                plugin_id,
                final_status=None,
                record_stop_failure=False,
            )
            await self._installations.set_status(
                plugin_id,
                status="failed",
                error_category=error_category,
            )

    def _event_registrations(self, plugin_id: str) -> tuple[EventHookRegistration, ...]:
        result: list[EventHookRegistration] = []
        for extension in self._extensions.list(plugin_id=plugin_id, kind=ExtensionKind.EVENT_HOOK):
            if not isinstance(extension.registration, EventHookRegistration):
                raise PluginLifecycleError("invalid event hook registration")
            result.append(extension.registration)
        return tuple(result)

    def _background_registrations(
        self, plugin_id: str
    ) -> tuple[BackgroundServiceRegistration, ...]:
        result: list[BackgroundServiceRegistration] = []
        for extension in self._extensions.list(
            plugin_id=plugin_id, kind=ExtensionKind.BACKGROUND_SERVICE
        ):
            if not isinstance(extension.registration, BackgroundServiceRegistration):
                raise PluginLifecycleError("invalid background service registration")
            result.append(extension.registration)
        return tuple(result)

    def _validate_background_limits(
        self,
        manifest: PluginManifest,
        registrations: tuple[BackgroundServiceRegistration, ...],
    ) -> None:
        allowed = min(self._background_task_limit, manifest.limits.background_tasks)
        required = sum(item.metadata.max_concurrency for item in registrations)
        if required > allowed:
            raise PluginLifecycleError(
                f"plugin declares {required} background slots but only {allowed} are allowed"
            )

    async def _make_context(
        self,
        manifest: PluginManifest,
        permissions: frozenset[PluginPermission],
    ) -> PluginContext:
        context = self._context_factory(manifest, permissions)
        if inspect.isawaitable(context):
            return await context
        return context

    async def _record_start_failure(self, plugin_id: str, error_category: str) -> None:
        await self._installations.record_failure(
            plugin_id,
            error_category=error_category,
            disable_threshold=self._failure_disable_threshold,
        )

    async def _record_audit(
        self,
        plugin_id: str,
        *,
        actor_user_id: str | None,
        operation: str,
        success: bool,
        error_category: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.record(
                plugin_id=plugin_id,
                actor_user_id=actor_user_id,
                operation=operation,
                permission=None,
                success=success,
                error_category=error_category,
                detail=detail,
            )
        except Exception as exc:
            logger.warning(
                "plugin_audit_failed plugin_id=%s operation=%s error_category=%s",
                plugin_id,
                operation,
                type(exc).__name__,
            )

    @staticmethod
    def _normalize_permissions(
        permissions: Iterable[PluginPermission | str] | None,
    ) -> tuple[str, ...] | None:
        if permissions is None:
            return None
        return tuple(
            sorted(
                {
                    item.value
                    if isinstance(item, PluginPermission)
                    else PluginPermission(item).value
                    for item in permissions
                }
            )
        )
