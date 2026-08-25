"""Session-aware management mutations. Domain rules stay in existing services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigChangeResult
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_types import (
    ConfigRollbackPayload,
    ConfigWritePayload,
    ManagementActionPayload,
)
from qq_ai_bot.control_plane.commands import ControlCommand
from qq_ai_bot.control_plane.operations import OperationRef, OperationStatus, StateEpoch
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import ProblemCode
from qq_ai_bot.emoji.db_models import EmojiAssetModel
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.models import EmojiLifecycleStatus
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.dream.service import plan_full_core
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.rebuild.service import (
    cancel_rebuild_core,
    plan_rebuild_core,
    start_rebuild_core,
)
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import (
    AutomationModel,
    MemoryFactModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.unit_of_work import next_updated_at
from qq_ai_bot.persistence.unit_of_work import state_revision as _state_revision
from qq_ai_bot.plugin_host.db_models import PluginInstallationModel, PluginNotificationOutboxModel
from qq_ai_bot.plugin_host.manager import diagnose_plugin
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginApprovalError, PluginInstallationRepository
from qq_ai_bot.speech.db_models import SpeechVoiceProfileModel
from qq_ai_bot.speech.repository import VoiceProfileRepository
from qq_ai_bot.time.service import TimeContextService


class ManagementUnavailable(Exception):
    """A management dependency was not injected and cannot be constructed."""


class ManagementFailure(Exception):
    def __init__(self, code: ProblemCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ManagementMutation:
    resource_id: str
    revision: int
    status: str
    operation: OperationRef | None = None


def state_revision(updated_at: datetime) -> int:
    return _state_revision(updated_at)


def _require_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ManagementFailure(ProblemCode.VERSION_CONFLICT)


class _TimestampedRow(Protocol):
    updated_at: datetime


async def _persist_revision(
    session: AsyncSession,
    row: _TimestampedRow,
    previous: datetime,
) -> int:
    current = row.updated_at
    if type(current) is not datetime:
        raise ManagementFailure(ProblemCode.STATE_MISMATCH)
    row.updated_at = next_updated_at(previous, now=current)
    await session.flush()
    return state_revision(row.updated_at)


def _map_config_error(result: ConfigChangeResult) -> ProblemCode:
    category = result.error_category or "validation_error"
    if category == "version_conflict":
        return ProblemCode.VERSION_CONFLICT
    if category in {"not_found", "unknown_key", "not_rollbackable"}:
        return ProblemCode.NOT_FOUND
    if category == "rollback_conflict":
        return ProblemCode.VERSION_CONFLICT
    if category == "permission_denied":
        if result.apply_mode is ConfigApplyMode.SECRET:
            return ProblemCode.SECRET_NOT_READABLE
        return ProblemCode.PRECONDITION_FAILED
    if category == "validation_error":
        return ProblemCode.VALIDATION_ERROR
    return ProblemCode.PRECONDITION_FAILED


def _op_ref(
    operation_id: str,
    status: OperationStatus,
    *,
    created_at: datetime,
    updated_at: datetime,
    progress: float,
    error_category: str | None = None,
) -> OperationRef:
    return OperationRef(
        operation_id=operation_id,
        status=status,
        progress=progress,
        state_epoch=StateEpoch.V1,
        error_category=error_category,
        created_at=created_at,
        updated_at=updated_at,
    )


def _rebuild_status(value: str) -> OperationStatus:
    mapping = {
        "planned": OperationStatus.QUEUED,
        "extracting": OperationStatus.RUNNING,
        "extraction_paused": OperationStatus.RUNNING,
        "review": OperationStatus.RUNNING,
        "committing": OperationStatus.RUNNING,
        "commit_paused": OperationStatus.RUNNING,
        "completed": OperationStatus.SUCCEEDED,
        "cancelled": OperationStatus.CANCELLED,
        "failed": OperationStatus.FAILED,
    }
    return mapping.get(value, OperationStatus.RUNNING)


def _dream_status(value: str) -> OperationStatus:
    mapping = {
        "planned": OperationStatus.QUEUED,
        "running": OperationStatus.RUNNING,
        "partial_failed": OperationStatus.FAILED,
        "completed": OperationStatus.SUCCEEDED,
        "cancelled": OperationStatus.CANCELLED,
        "rolling_back": OperationStatus.RUNNING,
        "rolled_back": OperationStatus.CANCELLED,
    }
    return mapping.get(value, OperationStatus.RUNNING)


class ControlManagementGateway:
    """Reuse existing domain services inside the control-plane unit of work."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        runtime_config: RuntimeConfigService | None = None,
        mcp: MCPManager | None = None,
        maintenance: MemoryMaintenanceWorker | None = None,
        embeddings: MemoryEmbeddingRuntime | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._runtime_config = runtime_config
        self._mcp = mcp
        self._maintenance = maintenance
        self._embeddings = embeddings
        self._automation: AutomationService | None = None

    def _require_settings(self) -> Settings:
        if self._settings is None:
            raise ManagementUnavailable
        return self._settings

    def _config_service(self) -> RuntimeConfigService:
        if self._runtime_config is not None:
            return self._runtime_config
        settings = self._require_settings()
        self._runtime_config = RuntimeConfigService(settings=settings, database=self._database)
        return self._runtime_config

    def _facts(self) -> MemoryFactService:
        return MemoryFactService(MemoryFactRepository(self._database))

    def _automation_service(self) -> AutomationService:
        if self._automation is not None:
            return self._automation
        settings = self._require_settings()
        self._automation = AutomationService(
            settings=settings,
            repository=AutomationRepository(self._database),
            registry=build_capability_registry(),
            time_service=TimeContextService(self._database),
        )
        return self._automation

    async def set_config(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ConfigWritePayload,
    ) -> ManagementMutation:
        runtime = self._config_service()
        result = await runtime.set_override(
            parsed.key,
            parsed.value,
            scope_type=parsed.scope_type,
            scope_id=parsed.scope_id,
            actor_user_id=principal.principal_id.text,
            trigger_message_id=command.request_id.text,
            expected_version=command.expected_revision,
            session=session,
        )
        if not result.success:
            raise ManagementFailure(_map_config_error(result))
        revision = result.version if result.version is not None and result.version >= 1 else 1
        return ManagementMutation(parsed.key, revision, "applied")

    async def unset_config(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ConfigWritePayload,
    ) -> ManagementMutation:
        runtime = self._config_service()
        result = await runtime.delete_override(
            parsed.key,
            scope_type=parsed.scope_type,
            scope_id=parsed.scope_id,
            actor_user_id=principal.principal_id.text,
            trigger_message_id=command.request_id.text,
            expected_version=command.expected_revision,
            session=session,
        )
        if not result.success:
            raise ManagementFailure(_map_config_error(result))
        revision = max(1, command.expected_revision)
        return ManagementMutation(parsed.key, revision, "removed")

    async def rollback_config(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ConfigRollbackPayload,
    ) -> ManagementMutation:
        runtime = self._config_service()
        result = await runtime.rollback(
            parsed.change_id,
            actor_user_id=principal.principal_id.text,
            trigger_message_id=command.request_id.text,
            expected_version=command.expected_revision,
            session=session,
        )
        if not result.success:
            raise ManagementFailure(_map_config_error(result))
        revision = result.version if result.version is not None and result.version >= 1 else 1
        return ManagementMutation(str(parsed.change_id), revision, "rolled_back")

    async def mutate_memory(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        try:
            fact_id = int(parsed.resource_id)
        except ValueError as exc:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
        self._require_settings()
        facts = self._facts()
        current = await facts.get_fact(fact_id, session=session)
        if current is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        _require_revision(state_revision(current.updated_at), command.expected_revision)
        if parsed.action == "confirm":
            updated = await facts.administrator_confirm(
                fact_id,
                actor_user_id=principal.principal_id.text,
                session=session,
            )
        elif parsed.action == "quarantine":
            updated = await facts.administrator_quarantine(fact_id, session=session)
        else:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        if updated is None:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
        row = await session.get(MemoryFactModel, updated.id)
        if row is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            parsed.resource_id,
            await _persist_revision(session, row, current.updated_at),
            parsed.action,
        )

    async def rebuild_memory(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        settings = self._require_settings()
        repository = MemoryRebuildRepository(self._database)
        ledger = EventLedgerRepository(self._database)
        actor = principal.principal_id.text
        try:
            return await self._rebuild_memory_inner(
                session,
                command,
                parsed,
                settings=settings,
                repository=repository,
                ledger=ledger,
                actor=actor,
            )
        except RuntimeError as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        except ValueError as exc:
            message = str(exc)
            if "not found" in message:
                raise ManagementFailure(ProblemCode.NOT_FOUND) from exc
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc

    async def _rebuild_memory_inner(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: ManagementActionPayload,
        *,
        settings: Settings,
        repository: MemoryRebuildRepository,
        ledger: EventLedgerRepository,
        actor: str,
    ) -> ManagementMutation:
        existing = None
        if parsed.action == "plan":
            _require_revision(0, command.expected_revision)
            run = await plan_rebuild_core(
                settings=settings,
                repository=repository,
                ledger=ledger,
                selection=_rebuild_selection(parsed.spec),
                actor_user_id=actor,
                session=session,
            )
        elif parsed.action == "start":
            if _looks_like_public_id(parsed.resource_id):
                existing = await repository.get_run(parsed.resource_id, session=session)
                if existing is None:
                    raise ManagementFailure(ProblemCode.NOT_FOUND)
                _require_revision(state_revision(existing.updated_at), command.expected_revision)
                run = await start_rebuild_core(
                    repository, parsed.resource_id, settings=settings, session=session
                )
            else:
                _require_revision(0, command.expected_revision)
                planned = await plan_rebuild_core(
                    settings=settings,
                    repository=repository,
                    ledger=ledger,
                    selection=_rebuild_selection(parsed.spec),
                    actor_user_id=actor,
                    session=session,
                )
                run = await start_rebuild_core(
                    repository, planned.public_id, settings=settings, session=session
                )
        elif parsed.action == "cancel":
            existing = await repository.get_run(parsed.resource_id, session=session)
            if existing is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(existing.updated_at), command.expected_revision)
            run = await cancel_rebuild_core(repository, parsed.resource_id, session=session)
        else:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        row = await session.scalar(
            select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == run.public_id)
        )
        if row is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        previous = existing.updated_at if existing is not None else run.created_at
        revision = await _persist_revision(session, row, previous)
        loaded = await repository.get_run(run.public_id, session=session)
        if loaded is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            loaded.public_id,
            revision,
            loaded.status.value,
            operation=_op_ref(
                f"rebuild:{loaded.public_id}",
                _rebuild_status(loaded.status.value),
                created_at=loaded.created_at,
                updated_at=loaded.updated_at,
                progress=1.0 if loaded.status.value == "completed" else 0.1,
            ),
        )

    async def dream_memory(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        repository = DreamRepository(self._database)
        actor = principal.principal_id.text
        settings = self._require_settings()
        existing = None
        if parsed.action == "plan" or (
            parsed.action == "start" and not _looks_like_public_id(parsed.resource_id)
        ):
            _require_revision(0, command.expected_revision)
            if self._embeddings is None:
                raise ManagementUnavailable
            try:
                run = await plan_full_core(
                    settings=settings,
                    repository=repository,
                    embeddings=self._embeddings,
                    actor_user_id=actor,
                    session=session,
                )
            except RuntimeError as exc:
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
            if parsed.action == "start":
                if not await repository.start_run(run.public_id, session=session):
                    raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
                loaded = await repository.get_run(run.public_id, session=session)
                if loaded is None:
                    raise ManagementFailure(ProblemCode.STATE_MISMATCH)
                run = loaded
        elif parsed.action == "start":
            existing = await repository.get_run(parsed.resource_id, session=session)
            if existing is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(existing.updated_at), command.expected_revision)
            if not await repository.start_run(parsed.resource_id, session=session):
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
            loaded = await repository.get_run(parsed.resource_id, session=session)
            if loaded is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            run = loaded
        elif parsed.action == "cancel":
            existing = await repository.get_run(parsed.resource_id, session=session)
            if existing is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(existing.updated_at), command.expected_revision)
            if not await repository.cancel(parsed.resource_id, session=session):
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
            loaded = await repository.get_run(parsed.resource_id, session=session)
            if loaded is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            run = loaded
        else:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        row = await session.scalar(
            select(MemoryDreamRunModel).where(MemoryDreamRunModel.public_id == run.public_id)
        )
        if row is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        previous = existing.updated_at if existing is not None else run.created_at
        revision = await _persist_revision(session, row, previous)
        current = await repository.get_run(run.public_id, session=session)
        if current is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            current.public_id,
            revision,
            current.status.value,
            operation=_op_ref(
                f"dream:{current.public_id}",
                _dream_status(current.status.value),
                created_at=current.created_at,
                updated_at=current.updated_at,
                progress=1.0 if current.status.value == "completed" else 0.1,
            ),
        )

    async def maintain_memory(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        if parsed.action not in {"plan", "start", "run"}:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        _require_revision(0, command.expected_revision)
        worker = self._maintenance
        if worker is None:
            worker = MemoryMaintenanceWorker(
                settings=self._require_settings(),
                facts=self._facts(),
            )
        changed = await worker.process_once(session=session)
        return ManagementMutation(
            parsed.resource_id,
            max(1, changed + 1),
            "completed",
        )

    async def mutate_automation(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        service = self._automation_service()
        actor = principal.principal_id.text
        if parsed.action == "create":
            _require_revision(0, command.expected_revision)
            if parsed.spec is None:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            try:
                row = await service.administer_create(
                    dict(parsed.spec),
                    actor_user_id=actor,
                    trigger_message_id=command.request_id.text,
                    session=session,
                )
            except RuntimeError as exc:
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
            except (ValueError, PermissionError) as exc:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
            stored = await session.get(AutomationModel, row.id)
            if stored is None:
                raise ManagementFailure(ProblemCode.STATE_MISMATCH)
            return ManagementMutation(
                str(row.id),
                await _persist_revision(session, stored, row.created_at),
                row.status.value,
            )
        try:
            automation_id = int(parsed.resource_id)
        except ValueError as exc:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
        existing = await AutomationRepository(self._database).get(automation_id, session=session)
        if existing is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        _require_revision(state_revision(existing.updated_at), command.expected_revision)
        try:
            if parsed.action == "update":
                if parsed.spec is None:
                    raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
                row = await service.administer_update(
                    automation_id,
                    dict(parsed.spec),
                    actor_user_id=actor,
                    trigger_message_id=command.request_id.text,
                    session=session,
                )
            elif parsed.action in {"pause", "resume", "cancel", "run_now"}:
                row = await service.administer_transition(
                    automation_id, action=parsed.action, session=session
                )
            else:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        except LookupError as exc:
            raise ManagementFailure(ProblemCode.NOT_FOUND) from exc
        except RuntimeError as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        except (ValueError, PermissionError) as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        stored = await session.get(AutomationModel, row.id)
        if stored is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            str(row.id),
            await _persist_revision(session, stored, existing.updated_at),
            row.status.value,
        )

    async def mutate_plugin(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        self._require_settings()
        installations = PluginInstallationRepository(self._database)
        if parsed.action == "retry":
            try:
                item_id = int(parsed.resource_id)
            except ValueError as exc:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
            current_outbox = await session.get(PluginNotificationOutboxModel, item_id)
            if current_outbox is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(current_outbox.updated_at), command.expected_revision)
            await PluginNotificationRepository(self._database).retry_outbox(
                item_id, error_category="manual_retry", session=session
            )
            updated_outbox = await session.get(PluginNotificationOutboxModel, item_id)
            if updated_outbox is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            return ManagementMutation(
                parsed.resource_id,
                await _persist_revision(session, updated_outbox, current_outbox.updated_at),
                str(updated_outbox.status),
            )
        current = await installations.get(parsed.resource_id, session=session)
        if current is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        _require_revision(state_revision(current.updated_at), command.expected_revision)
        try:
            if parsed.action == "approve":
                permissions = None
                if parsed.spec is not None and "permissions" in parsed.spec:
                    raw = parsed.spec["permissions"]
                    if not isinstance(raw, list | tuple):
                        raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
                    permissions = tuple(str(item) for item in raw)
                updated = await installations.approve(
                    parsed.resource_id, permissions=permissions, session=session
                )
            elif parsed.action == "enable":
                updated = await installations.set_enabled(
                    parsed.resource_id, enabled=True, session=session
                )
            elif parsed.action == "disable":
                updated = await installations.set_enabled(
                    parsed.resource_id, enabled=False, session=session
                )
            elif parsed.action == "doctor":
                report = diagnose_plugin(
                    parsed.resource_id,
                    system_enabled=self._require_settings().plugin_system_enabled,
                    record=current,
                )
                return ManagementMutation(
                    parsed.resource_id,
                    state_revision(current.updated_at),
                    "healthy" if not report.problems else "unhealthy",
                )
            else:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        except PluginApprovalError as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        if updated is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        stored = await session.get(PluginInstallationModel, parsed.resource_id)
        if stored is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            parsed.resource_id,
            await _persist_revision(session, stored, current.updated_at),
            parsed.action if parsed.action == "doctor" else updated.status,
        )

    async def mutate_mcp(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        if parsed.action in {"call", "run", "invoke", "execute"}:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
        if parsed.action not in {"enable", "disable", "refresh", "reconnect"}:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        if self._mcp is None:
            raise ManagementUnavailable
        repo = MCPRepository(self._database)
        state = await repo.state(parsed.resource_id, session=session)
        actual = 0 if state is None else state_revision(state.updated_at)
        _require_revision(actual, command.expected_revision)
        try:
            if parsed.action == "enable":
                await self._mcp.set_enabled(parsed.resource_id, True, session=session)
            elif parsed.action == "disable":
                await self._mcp.set_enabled(parsed.resource_id, False, session=session)
            elif parsed.action == "refresh":
                await self._mcp.refresh(parsed.resource_id, session=session)
            else:
                await self._mcp.reconnect(parsed.resource_id, session=session)
            status = await self._mcp.status(parsed.resource_id, session=session)
        except KeyError as exc:
            raise ManagementFailure(ProblemCode.NOT_FOUND) from exc
        except (RuntimeError, ValueError) as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        refreshed = await repo.state(parsed.resource_id, session=session)
        if refreshed is None:
            revision = 1
        else:
            previous = state.updated_at if state is not None else refreshed.updated_at
            revision = await _persist_revision(session, refreshed, previous)
        return ManagementMutation(
            parsed.resource_id,
            revision,
            "enabled" if status.enabled else "disabled",
        )

    async def mutate_emoji(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        self._require_settings()
        repository = EmojiRepository(self._database)
        lifecycle = EmojiLifecycleService(repository)
        current = await repository.get(parsed.resource_id, session=session)
        if current is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        _require_revision(state_revision(current.updated_at), command.expected_revision)
        try:
            if parsed.action == "pin":
                updated = await repository.set_pinned(parsed.resource_id, True, session=session)
            elif parsed.action == "unpin":
                updated = await repository.set_pinned(parsed.resource_id, False, session=session)
            elif parsed.action == "reject":
                updated = await lifecycle.transition(
                    parsed.resource_id, EmojiLifecycleStatus.REJECTED, session=session
                )
            elif parsed.action == "ban":
                updated = await lifecycle.transition(
                    parsed.resource_id, EmojiLifecycleStatus.BANNED, session=session
                )
            else:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        except ValueError as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        except LookupError as exc:
            raise ManagementFailure(ProblemCode.NOT_FOUND) from exc
        stored = await session.get(EmojiAssetModel, parsed.resource_id)
        if stored is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            parsed.resource_id,
            await _persist_revision(session, stored, current.updated_at),
            updated.status.value,
        )

    async def mutate_speech(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        self._require_settings()
        repository = VoiceProfileRepository(self._database)
        current = await repository.get_profile(parsed.resource_id, session=session)
        if current is None:
            raise ManagementFailure(ProblemCode.NOT_FOUND)
        _require_revision(state_revision(current.updated_at), command.expected_revision)
        try:
            if parsed.action == "enable":
                updated = await repository.set_enabled(
                    parsed.resource_id, enabled=True, session=session
                )
            elif parsed.action == "disable":
                updated = await repository.set_enabled(
                    parsed.resource_id, enabled=False, session=session
                )
            else:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
        except LookupError as exc:
            raise ManagementFailure(ProblemCode.NOT_FOUND) from exc
        stored = await session.get(SpeechVoiceProfileModel, parsed.resource_id)
        if stored is None:
            raise ManagementFailure(ProblemCode.STATE_MISMATCH)
        return ManagementMutation(
            parsed.resource_id,
            await _persist_revision(session, stored, current.updated_at),
            "enabled" if updated.enabled else "disabled",
        )

    async def cancel_operation(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        kind, _, rest = parsed.resource_id.partition(":")
        if kind == "rebuild":
            return await self.rebuild_memory(
                session,
                principal,
                command,
                ManagementActionPayload(action="cancel", resource_id=rest),
            )
        if kind == "dream":
            return await self.dream_memory(
                session,
                principal,
                command,
                ManagementActionPayload(action="cancel", resource_id=rest),
            )
        if kind == "automation":
            return await self.mutate_automation(
                session,
                principal,
                command,
                ManagementActionPayload(action="cancel", resource_id=rest),
            )
        raise ManagementFailure(ProblemCode.OPERATION_UNAVAILABLE)

    async def retry_operation(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        kind, _, rest = parsed.resource_id.partition(":")
        if kind == "rebuild":
            return await self.rebuild_memory(
                session,
                principal,
                command,
                ManagementActionPayload(action="start", resource_id=rest),
            )
        if kind == "dream":
            return await self.dream_memory(
                session,
                principal,
                command,
                ManagementActionPayload(action="start", resource_id=rest),
            )
        if kind == "plugin-outbox":
            return await self.mutate_plugin(
                session,
                command,
                ManagementActionPayload(action="retry", resource_id=rest),
            )
        if kind == "automation":
            return await self.mutate_automation(
                session,
                principal,
                command,
                ManagementActionPayload(action="run_now", resource_id=rest),
            )
        raise ManagementFailure(ProblemCode.OPERATION_UNAVAILABLE)


def _looks_like_public_id(value: str) -> bool:
    return len(value) >= 32 and "-" in value


def _rebuild_selection(spec: Mapping[str, object] | None) -> MemoryRebuildSelection:
    raw = spec.get("maximum_events") if spec is not None else None
    maximum = raw if type(raw) is int else 100
    return MemoryRebuildSelection(all_events=True, maximum_events=maximum)
