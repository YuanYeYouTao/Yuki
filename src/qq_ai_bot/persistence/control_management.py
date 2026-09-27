"""Session-aware management mutations. Domain rules stay in existing services."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigChangeResult, ControlAuditRef
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_types import (
    CommandOperation,
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
from qq_ai_bot.memory.dream.service import PreparedDreamPlan, plan_full_core, prepare_full_core
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.rebuild.service import (
    PreparedRebuildPlan,
    cancel_rebuild_core,
    plan_rebuild_core,
    prepare_rebuild_core,
    start_rebuild_core,
)
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
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.speech.db_models import SpeechVoiceProfileModel
from qq_ai_bot.speech.repository import VoiceProfileRepository
from yuki_plugin_sdk.permissions import PluginPermission


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
    progress: float | None,
    error_category: str | None = None,
) -> OperationRef:
    return OperationRef(
        operation_id=operation_id,
        status=status,
        progress=progress,
        state_epoch=StateEpoch.V2,
        error_category=error_category,
        created_at=created_at,
        updated_at=updated_at,
    )


def _rebuild_status(value: str) -> OperationStatus:
    mapping = {
        "planned": OperationStatus.QUEUED,
        "extracting": OperationStatus.RUNNING,
        "extraction_paused": OperationStatus.BLOCKED,
        "review": OperationStatus.WAITING,
        "committing": OperationStatus.RUNNING,
        "commit_paused": OperationStatus.BLOCKED,
        "completed": OperationStatus.SUCCEEDED,
        "cancelled": OperationStatus.CANCELLED,
        "failed": OperationStatus.FAILED,
    }
    return mapping[value]


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
    return mapping[value]


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
        automation: AutomationService | None = None,
        memories: MemoryFactService | None = None,
        plugins: PluginManager | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._runtime_config = runtime_config
        self._mcp = mcp
        self._maintenance = maintenance
        self._embeddings = embeddings
        self._automation = automation
        self._memories = memories
        self._plugins = plugins

    async def validate_external(
        self,
        session: AsyncSession,
        command: ControlCommand,
        operation: str,
        parsed: ManagementActionPayload,
    ) -> None:
        if operation == CommandOperation.PLUGIN_MUTATE.value:
            if self._plugins is None:
                raise ManagementUnavailable
            if parsed.action not in {"approve", "enable", "disable", "doctor"}:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            row = await session.get(PluginInstallationModel, parsed.resource_id)
            if row is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(row.updated_at), command.expected_revision)
            if parsed.action == "approve":
                permissions = (parsed.spec or {}).get("permissions")
                if not isinstance(permissions, tuple) or not all(
                    isinstance(item, str) for item in permissions
                ):
                    raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
                try:
                    requested = json.loads(row.requested_permissions_json)
                    selected = {PluginPermission(str(item)).value for item in permissions}
                    if not selected <= set(requested):
                        raise ValueError("permission not requested")
                except (TypeError, ValueError) as exc:
                    raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
        elif operation == CommandOperation.MCP_MUTATE.value:
            if self._mcp is None:
                raise ManagementUnavailable
            if parsed.action not in {"enable", "disable", "refresh", "reconnect"}:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            if parsed.resource_id not in self._mcp.configured_server_ids:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            mcp_row = await MCPRepository(self._database).state(parsed.resource_id, session=session)
            _require_revision(
                0 if mcp_row is None else state_revision(mcp_row.updated_at),
                command.expected_revision,
            )
        elif operation == CommandOperation.MEMORY_MAINTENANCE.value:
            if self._maintenance is None:
                raise ManagementUnavailable
            if parsed.action not in {"start", "run"} or parsed.resource_id != "yuki":
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            _require_revision(0, command.expected_revision)
        else:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR)

    async def execute_external(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        if operation == CommandOperation.PLUGIN_MUTATE.value:
            manager = self._plugins
            if manager is None:
                raise ManagementUnavailable
            if parsed.action == "approve":
                raw = parsed.spec.get("permissions") if parsed.spec else None
                if raw is not None and (
                    not isinstance(raw, list | tuple) or any(type(item) is not str for item in raw)
                ):
                    raise ValueError("invalid plugin permission list")
                record = await manager.approve(
                    parsed.resource_id,
                    permissions=None if raw is None else tuple(str(item) for item in raw),
                    actor_user_id=principal.principal_id.text,
                    expected_revision=command.expected_revision,
                )
            elif parsed.action == "enable":
                record = await manager.enable(
                    parsed.resource_id,
                    actor_user_id=principal.principal_id.text,
                    expected_revision=command.expected_revision,
                )
            elif parsed.action == "disable":
                record = await manager.disable(
                    parsed.resource_id,
                    actor_user_id=principal.principal_id.text,
                    expected_revision=command.expected_revision,
                )
            else:
                report = await manager.doctor(parsed.resource_id)
                shown = await manager.show(parsed.resource_id)
                if shown is None:
                    raise RuntimeError("plugin disappeared")
                return ManagementMutation(
                    parsed.resource_id,
                    state_revision(shown.updated_at),
                    "healthy" if not report.problems else "unhealthy",
                )
            if parsed.action == "enable" and record.status == "failed":
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
            return ManagementMutation(
                parsed.resource_id, state_revision(record.updated_at), record.status
            )
        if operation == CommandOperation.MCP_MUTATE.value:
            if self._mcp is None:
                raise ManagementUnavailable
            await self._mcp.manage(
                parsed.resource_id,
                action=parsed.action,
                expected_revision=command.expected_revision,
            )
            row = await MCPRepository(self._database).state(parsed.resource_id)
            if row is None:
                raise RuntimeError("MCP state missing after management")
            return ManagementMutation(
                parsed.resource_id, state_revision(row.updated_at), str(row.status)
            )
        if self._maintenance is None:
            raise ManagementUnavailable
        await self._maintenance.process_once()
        return ManagementMutation(parsed.resource_id, 1, "completed")

    async def prepare(
        self, operation: str, parsed: ManagementActionPayload
    ) -> PreparedRebuildPlan | PreparedDreamPlan | None:
        try:
            if operation == CommandOperation.MEMORY_REBUILD.value and parsed.action == "plan":
                selection = _rebuild_selection(parsed.spec)
                return await prepare_rebuild_core(
                    settings=self._require_settings(),
                    ledger=EventLedgerRepository(self._database),
                    selection=selection,
                )
            if operation == CommandOperation.MEMORY_DREAM.value and parsed.action == "plan":
                if self._embeddings is None:
                    raise ManagementUnavailable
                return await prepare_full_core(
                    settings=self._require_settings(),
                    repository=DreamRepository(self._database),
                    embeddings=self._embeddings,
                )
        except RuntimeError as exc:
            raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
        except ValueError as exc:
            raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
        return None

    def _require_settings(self) -> Settings:
        if self._settings is None:
            raise ManagementUnavailable
        return self._settings

    def _config_service(self) -> RuntimeConfigService:
        if self._runtime_config is not None:
            return self._runtime_config
        raise ManagementUnavailable

    def _facts(self) -> MemoryFactService:
        if self._memories is None:
            raise ManagementUnavailable
        return self._memories

    def _automation_service(self) -> AutomationService:
        if self._automation is not None:
            return self._automation
        raise ManagementUnavailable

    @staticmethod
    def _config_actor(principal: ControlPrincipal, command: ControlCommand) -> ControlAuditRef:
        return ControlAuditRef(
            user_id=principal.principal_id.text,
            principal_kind="control",
            principal_id=principal.principal_id.text,
            control_request_id=command.request_id.text,
        )

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
            trigger_message_id="",
            audit_ref=self._config_actor(principal, command),
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
            trigger_message_id="",
            audit_ref=self._config_actor(principal, command),
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
            trigger_message_id="",
            audit_ref=self._config_actor(principal, command),
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
        *,
        prepared: PreparedRebuildPlan | PreparedDreamPlan | None = None,
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
                prepared=prepared,
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
        prepared: PreparedRebuildPlan | PreparedDreamPlan | None = None,
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
                prepared=prepared if isinstance(prepared, PreparedRebuildPlan) else None,
            )
        elif parsed.action == "start":
            existing = await repository.get_run(parsed.resource_id, session=session)
            if existing is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(existing.updated_at), command.expected_revision)
            run = await start_rebuild_core(
                repository, parsed.resource_id, settings=settings, session=session
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
                progress=1.0 if loaded.status.value == "completed" else None,
            ),
        )

    async def dream_memory(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
        *,
        prepared: PreparedRebuildPlan | PreparedDreamPlan | None = None,
    ) -> ManagementMutation:
        repository = DreamRepository(self._database)
        actor = principal.principal_id.text
        settings = self._require_settings()
        existing = None
        if parsed.action == "plan":
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
                    prepared=prepared if isinstance(prepared, PreparedDreamPlan) else None,
                )
            except RuntimeError as exc:
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED) from exc
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
                progress=1.0 if current.status.value == "completed" else None,
            ),
        )

    async def mutate_automation(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        service = self._automation_service()
        if parsed.action == "create":
            _require_revision(0, command.expected_revision)
            if parsed.spec is None:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            spec = dict(parsed.spec)
            owner_id: object = principal.person_id.text if principal.person_id is not None else None
            conversation_id: object = None
            max_runs: object = None
            script_payload: object = spec
            if "script" in spec:
                if set(spec) - {"script", "owner_id", "conversation_id", "max_runs"}:
                    raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
                owner_id = spec.get("owner_id", owner_id)
                conversation_id = spec.get("conversation_id")
                max_runs = spec.get("max_runs")
                script_payload = spec["script"]
            if owner_id is None:
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
            if (
                not isinstance(owner_id, str)
                or (conversation_id is not None and not isinstance(conversation_id, str))
                or (max_runs is not None and (type(max_runs) is not int or max_runs < 1))
            ):
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR)
            try:
                row = await service.administer_create(
                    script_payload,
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    creation_source_key=f"control:{principal.principal_id.text}:{command.request_id.text}",
                    max_runs=max_runs,
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

    async def retry_plugin_notification(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
    ) -> ManagementMutation:
        if parsed.action == "retry":
            try:
                item_id = int(parsed.resource_id)
            except ValueError as exc:
                raise ManagementFailure(ProblemCode.VALIDATION_ERROR) from exc
            current_outbox = await session.get(PluginNotificationOutboxModel, item_id)
            if current_outbox is None:
                raise ManagementFailure(ProblemCode.NOT_FOUND)
            _require_revision(state_revision(current_outbox.updated_at), command.expected_revision)
            if (
                current_outbox.status != "failed"
                or current_outbox.platform_message_id is not None
                or current_outbox.last_error_category
                not in {"bot_unavailable", "gateway_disconnected", "effect_gate_timeout"}
                or current_outbox.attempts >= current_outbox.max_attempts
            ):
                raise ManagementFailure(ProblemCode.PRECONDITION_FAILED)
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
        raise ManagementFailure(ProblemCode.VALIDATION_ERROR)

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
            return await self.retry_plugin_notification(
                session,
                principal,
                command,
                ManagementActionPayload(action="retry", resource_id=rest),
            )
        raise ManagementFailure(ProblemCode.OPERATION_UNAVAILABLE)


def _rebuild_selection(spec: Mapping[str, object] | None) -> MemoryRebuildSelection:
    return MemoryRebuildSelection.model_validate(
        {"all_events": True, "maximum_events": 100, **(spec or {})}
    )
