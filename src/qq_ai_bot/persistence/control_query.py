"""Read-only persistence adapter for control-plane query projections."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, TypedDict, TypeVar

from sqlalchemy import Select, event, func, select, tuple_
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from qq_ai_bot import __version__
from qq_ai_bot.admin.config_files import ConfigFileError, ConfigFileService
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.json_types import JsonObject
from qq_ai_bot.control_plane.operations import (
    OperationKind,
    OperationRef,
    OperationStatus,
    StateEpoch,
)
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_cursors import (
    decode_integer_cursor_key,
    decode_operation_cursor_key,
    decode_resource_cursor,
    decode_time_id_key,
    encode_query_cursor,
    encode_time_id_key,
)
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    AuditEventView,
    AutomationView,
    ChatEventView,
    ChatHistoryFilter,
    ComponentHealthView,
    ConfigOverrideView,
    ConfigOwnerKind,
    ConfigQueryScope,
    ConfigSpecView,
    ControlQueryError,
    ConversationView,
    CountSnapshot,
    DownloadView,
    EffectiveConfigView,
    EmojiAssetView,
    EmojiSpaceEnablementView,
    ExecutionTraceFilter,
    ExecutionTraceView,
    IdentityBindingView,
    IdentityResolution,
    ManagementHealthView,
    MemoryEvidenceView,
    MemoryFactView,
    MemoryHealthView,
    MemoryJobView,
    MemoryQueryFilter,
    PendingRestartView,
    PersonActiveRouteView,
    PersonView,
    PluginRuntimeView,
    PluginView,
    PresenceConnectionState,
    PresenceView,
    QueryCursorPhase,
    QueryResourceKind,
    QueueSummary,
    ReflectionQueryFilter,
    RouteKind,
    SocialReceiptView,
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SystemSnapshot,
    YukiSummaryView,
    classify_route_reference,
    mask_external_id,
    sanitize_projected_display,
)
from qq_ai_bot.control_plane.tokens import require_opaque_token
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.conversation.media_service import ConversationMediaService
from qq_ai_bot.domain.identity import (
    ConversationGeneration,
    ConversationId,
    IdentityBindingId,
    PersonId,
    PresenceId,
    PrincipalId,
    RequestId,
    RouteGeneration,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.emoji.db_models import EmojiAssetModel, EmojiScopeStateModel
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.memory.embedding.health import MemoryEmbeddingHealthService
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.persistence.control_external import control_operation
from qq_ai_bot.persistence.control_operations import read_operation
from qq_ai_bot.persistence.control_paging import numbered_statement
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    AutomationModel,
    MemoryJobModel,
    MemoryRebuildRunModel,
    RuntimeConfigOverrideModel,
)
from qq_ai_bot.persistence.unit_of_work import state_revision
from qq_ai_bot.plugin_host.configuration_service import (
    PluginConfigurationError,
    PluginConfigurationService,
)
from qq_ai_bot.plugin_host.db_models import PluginInstallationModel
from qq_ai_bot.plugin_host.manager import PluginManagementRejected, PluginManager
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceStore
from yuki_plugin_sdk.observation import PluginObservationRequest


class _HasId(Protocol):
    id: str


class _AuditRow(Protocol):
    id: int
    actor_principal_id: str | None
    control_request_id: str | None
    capability: object
    operation: object
    target_type: object
    success: object
    error_category: object
    duration_seconds: int | float
    created_at: datetime | None


_T = TypeVar("_T")
_IdT = TypeVar("_IdT", bound=_HasId)


def _automation_target_kind(row: AutomationModel) -> str:
    if row.canonical_target_person_id:
        return "person"
    if row.canonical_target_space_id:
        return "space"
    return "none"


def _automation_target_id(row: AutomationModel) -> str:
    if row.canonical_target_person_id:
        return str(row.canonical_target_person_id)
    if row.canonical_target_space_id:
        return str(row.canonical_target_space_id)
    return "none"


def _automation_route_state(
    row: AutomationModel,
    *,
    person_route: PersonActiveRouteModel | None,
    space_route: SpaceActiveRouteModel | None,
) -> str:
    if not row.canonical_target_person_id and not row.canonical_target_space_id:
        return "missing"
    route = person_route if row.canonical_target_person_id else space_route
    if route is None:
        return "missing"
    if route.paused:
        return "paused"
    return "configured"


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _spec_is_configured(
    spec: object,
    overrides: set[tuple[str, str, str]] | dict[tuple[str, str, str], object],
    settings: object | None,
) -> bool:
    key = getattr(spec, "key", "")
    if any(item[0] == key for item in overrides):
        return True
    apply_mode = getattr(getattr(spec, "apply_mode", None), "value", "")
    if apply_mode != "secret" and not bool(getattr(spec, "sensitive", False)):
        return False
    getter = getattr(spec, "default_getter", None)
    if settings is None or getter is None:
        return False
    try:
        return bool(getter(settings))
    except (TypeError, ValueError, AttributeError):
        return False


def _safe_config_value(value_json: str) -> str | int | float | bool | None:
    try:
        decoded = json.loads(value_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if type(decoded) is str:
        return None if len(decoded) > 256 else decoded
    if type(decoded) is bool or type(decoded) is int or type(decoded) is float:
        return decoded
    return None


_CONFIG_OVERRIDE_CURSOR_PREFIX = "ovr:"


def _try_person_id(value: object) -> PersonId | None:
    if type(value) is not str or not value:
        return None
    try:
        return PersonId.parse(value)
    except (TypeError, ValueError):
        return None


def _try_space_id(value: object) -> SpaceId | None:
    if type(value) is not str or not value:
        return None
    try:
        return SpaceId.parse(value)
    except (TypeError, ValueError):
        return None


def _unavailable_config_owner() -> tuple[
    ConfigOwnerKind, PersonId | None, SpaceId | None, IdentityResolution
]:
    return (
        ConfigOwnerKind.UNAVAILABLE,
        None,
        None,
        IdentityResolution.UNRESOLVED,
    )


def _config_owner_projection(
    row: RuntimeConfigOverrideModel,
) -> tuple[ConfigOwnerKind, PersonId | None, SpaceId | None, IdentityResolution]:
    scope = str(row.scope_type)
    person = _try_person_id(row.canonical_person_id)
    space = _try_space_id(row.canonical_space_id)
    if person is not None and space is not None:
        return _unavailable_config_owner()
    if scope == "global":
        if person is not None or space is not None:
            return _unavailable_config_owner()
        return (
            ConfigOwnerKind.GLOBAL,
            None,
            None,
            IdentityResolution.CANONICAL,
        )
    if scope == "user":
        if space is not None:
            return _unavailable_config_owner()
        if person is not None:
            return (
                ConfigOwnerKind.PERSON,
                person,
                None,
                IdentityResolution.CANONICAL,
            )
        return _unavailable_config_owner()
    if scope == "group":
        if person is not None:
            return _unavailable_config_owner()
        if space is not None:
            return (
                ConfigOwnerKind.SPACE,
                None,
                space,
                IdentityResolution.CANONICAL,
            )
        return _unavailable_config_owner()
    return _unavailable_config_owner()


def _config_override_is_secret(spec: object | None) -> bool:
    if spec is None:
        return True
    apply_mode = getattr(getattr(spec, "apply_mode", None), "value", "")
    return apply_mode == "secret" or bool(getattr(spec, "sensitive", False))


def _project_config_override(
    row: RuntimeConfigOverrideModel,
    *,
    spec: object | None,
) -> ConfigOverrideView:
    owner_kind, person_id, space_id, resolution = _config_owner_projection(row)
    secret = _config_override_is_secret(spec)
    spec_mode = getattr(getattr(spec, "apply_mode", None), "value", "")
    apply_mode = "secret" if secret else str(spec_mode or row.apply_mode)
    return ConfigOverrideView(
        override_id=int(row.id),
        key=str(row.config_key),
        scope_type=str(row.scope_type),
        owner_kind=owner_kind,
        person_id=person_id,
        space_id=space_id,
        resolution=resolution,
        apply_mode=apply_mode,
        configured=True,
        version=int(row.version),
        value=None if secret else _safe_config_value(row.value_json),
    )


def _decode_config_override_cursor(key: str | None) -> int:
    if key is None:
        return 0
    prefix = _CONFIG_OVERRIDE_CURSOR_PREFIX
    if not key.startswith(prefix):
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return decode_integer_cursor_key(key[len(prefix) :], minimum=1)


def _project_emoji_space_enablement(
    row: EmojiScopeStateModel,
) -> EmojiSpaceEnablementView | None:
    if str(row.scope_type) == "global":
        return None
    space = _try_space_id(row.canonical_space_id)
    if space is not None:
        return EmojiSpaceEnablementView(
            space_id=space,
            resolution=IdentityResolution.CANONICAL,
            enabled=bool(row.enabled),
        )
    return EmojiSpaceEnablementView(
        space_id=None,
        resolution=IdentityResolution.UNRESOLVED,
        enabled=bool(row.enabled),
    )


def _project_emoji_asset(
    row: EmojiAssetModel,
    *,
    scope_rows: Sequence[EmojiScopeStateModel],
    reveal_first_seen_person: bool,
    reveal_first_seen_space: bool,
) -> EmojiAssetView:
    global_enabled: bool | None = None
    enablements: list[EmojiSpaceEnablementView] = []
    for scope in scope_rows:
        if str(scope.scope_type) == "global":
            global_enabled = bool(scope.enabled)
            continue
        projected = _project_emoji_space_enablement(scope)
        if projected is not None:
            enablements.append(projected)
    enablements.sort(
        key=lambda item: (
            0 if item.resolution is IdentityResolution.CANONICAL else 1,
            str(item.space_id) if item.space_id is not None else "",
            item.enabled,
        )
    )
    first_seen_person = (
        _try_person_id(row.canonical_first_seen_person_id) if reveal_first_seen_person else None
    )
    first_seen_space = (
        _try_space_id(row.canonical_first_seen_space_id) if reveal_first_seen_space else None
    )
    return EmojiAssetView(
        revision=state_revision(row.updated_at),
        asset_id=str(row.id),
        status=str(row.status),
        enabled=bool(row.pinned) or str(row.status) == "adopted",
        global_enabled=global_enabled,
        space_enablements=tuple(enablements),
        first_seen_person_id=first_seen_person,
        first_seen_space_id=first_seen_space,
    )


def _memory_job_view(row: MemoryJobModel) -> MemoryJobView:
    mapping = {
        "pending": OperationStatus.QUEUED,
        "processing": OperationStatus.RUNNING,
        "done": OperationStatus.SUCCEEDED,
        "failed": OperationStatus.FAILED,
    }
    status = mapping.get(str(row.status))
    if status is None:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
    created = _as_aware(row.created_at)
    updated = _as_aware(row.updated_at)
    if created is None or updated is None:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
    error_category = None
    if status is OperationStatus.FAILED:
        error_category = _safe_token(
            row.error_category, fallback="memory_job_failed", max_length=64
        )
    return MemoryJobView(
        job_id=str(row.id),
        kind=str(row.processing_source),
        status=str(row.status),
        operation=OperationRef(
            operation_id=f"memory-job-{row.id}",
            status=status,
            progress=1.0 if status is OperationStatus.SUCCEEDED else 0.0,
            state_epoch=StateEpoch.V2,
            error_category=error_category,
            created_at=created,
            updated_at=updated,
        ),
    )


def _safe_token(value: object, *, fallback: str, max_length: int) -> str:
    if type(value) is not str or not value:
        return fallback
    try:
        return require_opaque_token(value, name="token", max_length=max_length)
    except (TypeError, ValueError):
        return fallback


def project_audit_event(row: _AuditRow, *, snapshot_at: datetime) -> AuditEventView:
    """Project stored audit data. Corrupt rows fail closed as state_mismatch."""

    if type(snapshot_at) is not datetime:
        raise TypeError("snapshot_at must be datetime")
    try:
        actor = None
        request = None
        if getattr(row, "actor_principal_kind", None) == "control":
            if row.actor_principal_id is None:
                raise ValueError("control audit has no principal")
            actor = PrincipalId.parse(row.actor_principal_id)
            if row.control_request_id is None:
                raise ValueError("control audit has no request")
            request = RequestId.parse(row.control_request_id)
        return AuditEventView(
            principal_id=actor,
            request_id=request,
            target_id=getattr(row, "target_id", None) if actor is not None else None,
            audit_id=int(row.id),
            capability=_safe_token(row.capability, fallback="unspecified", max_length=64),
            operation=_safe_token(row.operation, fallback="unspecified", max_length=128),
            target_type=_safe_token(row.target_type, fallback="unspecified", max_length=64),
            success=bool(row.success),
            error_category=(
                None
                if row.error_category is None
                else _safe_token(row.error_category, fallback="unclassified", max_length=64)
            ),
            duration_seconds=float(row.duration_seconds),
            created_at=_as_aware(row.created_at) or snapshot_at,
        )
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc


class _PresenceConnectionFields(TypedDict):
    connection_state: PresenceConnectionState
    connection_provider: str | None
    connection_generation: int | None
    connection_capabilities: tuple[str, ...]
    connection_problem: Problem


def _presence_connection_fields(
    registry: object | None, row: PresenceModel
) -> _PresenceConnectionFields:
    if registry is None:
        return {
            "connection_state": PresenceConnectionState.UNAVAILABLE,
            "connection_provider": None,
            "connection_generation": None,
            "connection_capabilities": (),
            "connection_problem": Problem(ProblemCode.OPERATION_UNAVAILABLE),
        }
    snapshot = getattr(registry, "snapshot_presence", None)
    if not callable(snapshot):
        return {
            "connection_state": PresenceConnectionState.UNAVAILABLE,
            "connection_provider": None,
            "connection_generation": None,
            "connection_capabilities": (),
            "connection_problem": Problem(ProblemCode.OPERATION_UNAVAILABLE),
        }
    view = snapshot(
        presence_id=row.id,
        platform=row.platform,
        external_account_id=row.external_account_id,
    )
    health = str(getattr(view, "health", "disconnected"))
    if health == "connected":
        generation = int(getattr(view, "generation", 0) or 0)
        return {
            "connection_state": PresenceConnectionState.CONNECTED,
            "connection_provider": str(getattr(view, "provider", "")) or None,
            "connection_generation": generation or None,
            "connection_capabilities": tuple(
                sorted(str(item) for item in getattr(view, "capabilities", ()))
            ),
            "connection_problem": Problem(
                ProblemCode.OPERATION_UNAVAILABLE,
                {"live": True},
            ),
        }
    if health == "ambiguous":
        return {
            "connection_state": PresenceConnectionState.AMBIGUOUS,
            "connection_provider": None,
            "connection_generation": None,
            "connection_capabilities": (),
            "connection_problem": Problem(ProblemCode.BINDING_AMBIGUOUS),
        }
    return {
        "connection_state": PresenceConnectionState.DISCONNECTED,
        "connection_provider": None,
        "connection_generation": getattr(view, "generation", None),
        "connection_capabilities": (),
        "connection_problem": Problem(ProblemCode.NOT_FOUND),
    }


class ControlQueryAdapter:
    """Keyset reader over existing identity and conversation tables."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        workspace_service: WorkspaceService | None = None,
        runtime_config: RuntimeConfigService | None = None,
        embeddings: MemoryEmbeddingRuntime | None = None,
        connection_registry: object | None = None,
        plugins: PluginManager | None = None,
        workspace: WorkspaceStore | None = None,
        automation: AutomationService | None = None,
        conversation_media: ConversationMediaService | None = None,
        model_catalog: ModelProfileCatalog | None = None,
        config_files: ConfigFileService | None = None,
        participation_snapshot: Callable[[], Awaitable[dict[str, object]]] | None = None,
        runtime_health: Callable[[], Awaitable[tuple[ComponentHealthView, ...]]] | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        if type(database) is not Database:
            raise TypeError("database must be Database")
        self._database = database
        from qq_ai_bot.persistence.control_workspace import ControlWorkspace

        self._workspace_control = ControlWorkspace(
            workspace_service,
            readonly_root=settings.webui_workspace_directory if settings else None,
        )
        self._automation = automation
        from qq_ai_bot.persistence.control_execution_query import ControlExecutionQueryAdapter

        self._execution = ControlExecutionQueryAdapter(self._reader)
        from qq_ai_bot.persistence.control_activity_query import ControlActivityQueryAdapter

        self._activity = ControlActivityQueryAdapter(
            self._reader, workspace=workspace, conversation_media=conversation_media
        )
        self._config_files = config_files or (
            ConfigFileService(settings, model_catalog) if settings else None
        )
        from qq_ai_bot.persistence.control_work_query import ControlWorkQueryAdapter

        self._work_details = ControlWorkQueryAdapter(self._reader)
        from qq_ai_bot.persistence.control_automation_query import ControlAutomationQueryAdapter

        self._automation_details = ControlAutomationQueryAdapter(self._reader)
        from qq_ai_bot.persistence.control_memory_query import ControlMemoryQueryAdapter

        self._memory_details = ControlMemoryQueryAdapter(self._reader)
        from qq_ai_bot.persistence.control_reflection_query import ControlReflectionQueryAdapter

        self._reflection_details = ControlReflectionQueryAdapter(database, self._reader, settings)
        self._settings = settings
        self._model_catalog = model_catalog
        self._config = runtime_config
        self._embeddings = embeddings
        self._registry = runtime_config.registry if runtime_config is not None else ConfigRegistry()
        self._connections = connection_registry
        self._plugins = plugins
        self._runtime_health = runtime_health
        self._trace_recorder = trace_recorder
        if trace_recorder is not None and not isinstance(trace_recorder, TraceRecorder):
            raise TypeError("trace_recorder must be TraceRecorder or None")
        self._participation_snapshot = participation_snapshot

    async def download_workspace(self, artifact_id: str) -> DownloadView:
        return await self._activity.download_workspace(artifact_id)

    async def read_display_names(
        self, references: JsonObject, allowed: frozenset[str]
    ) -> ActivityView:
        from qq_ai_bot.persistence.control_names import display_names

        return await display_names(self._reader, references, allowed)

    async def download_avatar(self, kind: str, owner_id: str) -> DownloadView:
        from qq_ai_bot.persistence.control_names import download_avatar

        return await download_avatar(self._reader, kind, owner_id)

    async def download_environment_file(self, path: str) -> DownloadView:
        import asyncio

        from qq_ai_bot.persistence.control_media_query import workspace_bytes
        from qq_ai_bot.workspace.store import WorkspaceError

        root = self._settings.webui_workspace_directory if self._settings else None
        if root is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        try:
            return await asyncio.to_thread(workspace_bytes, root, path)
        except (WorkspaceError, OSError, ValueError) as exc:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND)) from exc

    async def download_emoji(self, asset_id: str) -> DownloadView:
        from qq_ai_bot.persistence.control_media_query import emoji_download

        return await emoji_download(
            self._reader, self._settings.emoji_storage_root if self._settings else None, asset_id
        )

    async def list_plugin_outbox(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        return await self._activity.list_plugin_outbox(request, plugin_id=plugin_id)

    async def download_chat_media(
        self, conversation_id: ConversationId, event_id: int, attachment_index: int
    ) -> DownloadView:
        return await self._activity.download_chat_media(conversation_id, event_id, attachment_index)

    async def read_model_catalog(self) -> ActivityView:
        catalog = (
            self._config_files.loaded_catalog if self._config_files is not None else None
        ) or self._model_catalog
        if catalog is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        return ActivityView(
            "models",
            {
                "profiles": [
                    {
                        "id": item.id,
                        "provider": item.provider,
                        "protocol": item.protocol.value,
                        "model": item.model,
                        "timeout_seconds": item.timeout_seconds,
                        "max_retries": item.max_retries,
                        "temperature": item.default_temperature,
                        "max_output_tokens": item.default_max_output_tokens,
                        "capabilities": sorted(cap.value for cap in item.capabilities),
                    }
                    for item in catalog.profiles.values()
                ],
                "routes": [
                    {"task": task.value, "profile_id": route.profile_id}
                    for task, route in catalog.routes.items()
                ],
                "apply_mode": (
                    "hot_reload"
                    if self._config_files and self._config_files.model_hot_reload_enabled
                    else "restart"
                ),
            },
        )

    async def read_config_file(self, file_id: str) -> ActivityView:
        if self._config_files is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        try:
            return ActivityView(file_id, await self._config_files.read(file_id))
        except ConfigFileError as exc:
            raise ControlQueryError(Problem(ProblemCode(exc.category))) from None

    async def read_persona(self) -> ActivityView:
        if self._settings is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        return ActivityView(
            "persona", {"system_prompt": self._settings.system_prompt, "apply_mode": "restart"}
        )

    async def list_participation_runs(
        self, request: PageRequest, *, conversation_id: ConversationId | None = None
    ) -> Page[ActivityView]:
        return await self._activity.list_participation_runs(
            request, conversation_id=conversation_id
        )

    async def list_participation_feedback(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        return await self._activity.list_participation_feedback(
            request, run_id=run_id, include_content=include_content
        )

    async def list_plugin_background_turns(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        return await self._activity.list_plugin_background_turns(request, plugin_id=plugin_id)

    async def read_participation(self) -> ActivityView:
        if self._participation_snapshot is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        return ActivityView("participation", await self._participation_snapshot())

    async def read_work(self, work_id: str, *, include_content: bool = False) -> ActivityView:
        return await self._work_details.read_work(work_id, include_content=include_content)

    async def list_work_history(
        self, request: PageRequest, *, work_id: str, section: str, include_content: bool = False
    ) -> Page[ActivityView]:
        return await self._work_details.list_work_history(
            request, work_id=work_id, section=section, include_content=include_content
        )

    async def list_work(
        self, request: PageRequest, *, include_content: bool = False
    ) -> Page[ActivityView]:
        return await self._activity.list_work(request, include_content=include_content)

    async def read_automation_schema(self) -> ActivityView:
        if self._automation is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        fields = self._automation.management_schema()
        if len(json.dumps(fields).encode()) > 256 * 1024:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        return ActivityView("automation_schema", fields)

    async def read_memory_maintenance_schema(self) -> ActivityView:
        from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection

        return ActivityView(
            "memory_maintenance_schema", {"rebuild": MemoryRebuildSelection.model_json_schema()}
        )

    async def list_memory_rebuild_proposals(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        return await self._memory_details.list_rebuild_proposals(
            request, run_id=run_id, include_content=include_content
        )

    async def read_memory_maintenance_run(self, operation_id: str) -> ActivityView:
        return await self._memory_details.read_memory_maintenance_run(operation_id)

    async def read_automation(self, automation_id: int) -> ActivityView:
        return await self._automation_details.read_automation(automation_id)

    async def list_automation_runs(
        self, request: PageRequest, *, automation_id: int
    ) -> Page[ActivityView]:
        return await self._automation_details.list_automation_runs(
            request, automation_id=automation_id
        )

    async def list_automation_steps(
        self, request: PageRequest, *, automation_id: int, run_id: int | None = None
    ) -> Page[ActivityView]:
        return await self._automation_details.list_automation_steps(
            request, automation_id=automation_id, run_id=run_id
        )

    async def list_model_usage(self, request: PageRequest) -> Page[ActivityView]:
        return await self._activity.list_model_usage(request)

    async def read_model_usage_summary(self, window: str) -> ActivityView:
        return await self._activity.read_model_usage_summary(window)

    async def list_workspace(self, request: PageRequest) -> Page[ActivityView]:
        return await self._activity.list_workspace(request)

    async def read_terminal_submission(
        self, request_id: RequestId, principal_id: PrincipalId
    ) -> ActivityView:
        return await self._workspace_control.read_submission(request_id, principal_id)

    async def read_environment(self, section: str, arguments: JsonObject) -> ActivityView:
        return await self._workspace_control.read(section, arguments)

    async def read_workspace(self, artifact_id: str) -> ActivityView:
        return await self._activity.read_workspace(artifact_id)

    async def list_execution_trace(
        self, request: PageRequest, *, scope: ExecutionTraceFilter, include_content: bool = False
    ) -> Page[ExecutionTraceView]:
        return await self._execution.list_execution_trace(
            request, scope=scope, include_content=include_content
        )

    async def read_execution_trace(
        self, entry_id: int, *, conversation_id: ConversationId | None = None
    ) -> ExecutionTraceView:
        return await self._execution.read_execution_trace(entry_id, conversation_id=conversation_id)

    async def list_chat_events(
        self,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        include_content: bool = False,
        history: ChatHistoryFilter | None = None,
    ) -> Page[ChatEventView]:
        return await self._execution.list_chat_events(
            request,
            conversation_id=conversation_id,
            include_content=include_content,
            history=history,
        )

    async def read_conversation_execution(
        self,
        conversation_id: ConversationId,
        *,
        include_content: bool = False,
        turn_id: str | None = None,
        before_step_id: int | None = None,
    ) -> ActivityView:
        from qq_ai_bot.persistence.control_live_execution import read_conversation_execution

        return await read_conversation_execution(
            self._reader,
            conversation_id,
            self._trace_recorder,
            include_content=include_content,
            turn_id=turn_id,
            before_step_id=before_step_id,
        )

    async def list_event_turns(
        self,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        event_id: int,
        direction: str,
    ) -> Page[ActivityView]:
        from qq_ai_bot.persistence.control_live_execution import list_event_turns

        return await list_event_turns(
            self._reader,
            request,
            conversation_id=conversation_id,
            event_id=event_id,
            direction=direction,
        )

    async def list_social_receipts(
        self, request: PageRequest, *, conversation_id: ConversationId
    ) -> Page[SocialReceiptView]:
        return await self._execution.list_social_receipts(request, conversation_id=conversation_id)

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session:
            session.autoflush = False

            def _reject_flush(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("control query adapter must not flush")

            sync_session = session.sync_session
            event.listen(sync_session, "before_flush", _reject_flush)
            try:
                yield session
            finally:
                event.remove(sync_session, "before_flush", _reject_flush)
                await session.rollback()

    async def _runtime(self, session: AsyncSession) -> tuple[StateEpoch, int]:
        del session
        return StateEpoch.V2, 1

    def _cursor_state(
        self,
        request: PageRequest,
        kind: QueryResourceKind,
        *,
        epoch: StateEpoch,
    ) -> tuple[QueryCursorPhase, str | None]:
        if type(request) is not PageRequest:
            raise TypeError("request must be PageRequest")
        if request.cursor is None:
            start = (
                QueryCursorPhase.TIME_ID
                if kind is QueryResourceKind.AUDIT
                else QueryCursorPhase.CANONICAL
            )
            return start, None
        return decode_resource_cursor(request.cursor, expected_kind=kind, epoch=epoch)

    async def _keyset(
        self,
        session: AsyncSession,
        model: type[_T],
        column: InstrumentedAttribute[str],
        time_column: InstrumentedAttribute[datetime],
        after: str | None,
        limit: int,
        *,
        request: PageRequest,
    ) -> tuple[list[_T], bool, int | None]:
        stmt = select(model)
        if after is not None:
            stmt = stmt.where(column > after)
        stmt = stmt.order_by(column.asc()).limit(limit)
        window = await numbered_statement(
            session,
            stmt,
            request,
            order=(time_column.desc(), column.desc()),
        )
        rows = list(await session.scalars(window.statement))
        has_more = len(rows) == limit
        return (rows[:-1] if has_more else rows), has_more, window.total

    async def _load_by_ids(
        self,
        session: AsyncSession,
        model: type[_IdT],
        column: InstrumentedAttribute[str],
        ids: Sequence[str],
    ) -> dict[str, _IdT]:
        if not ids:
            return {}
        rows = await session.scalars(select(model).where(column.in_(tuple(ids))))
        return {row.id: row for row in rows}

    async def _count(self, session: AsyncSession, stmt: Select[tuple[int]]) -> int:
        value = await session.scalar(stmt)
        return int(value or 0)

    async def _queue(self, session: AsyncSession) -> QueueSummary:
        return QueueSummary(
            memory_jobs_pending=await self._count(
                session,
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.status == "pending"),
            ),
            memory_jobs_processing=await self._count(
                session,
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.status == "processing"),
            ),
        )

    async def _pending_restart(self, session: AsyncSession) -> PendingRestartView:
        if self._config is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        pending = tuple(sorted(set(await self._config.pending_restart_entries(session=session))))
        return PendingRestartView(pending, len(pending))

    async def _counts(self, session: AsyncSession) -> dict[str, CountSnapshot]:
        return {
            "persons": CountSnapshot(
                count=await self._count(
                    session, select(func.count()).select_from(CanonicalPersonModel)
                ),
            ),
            "identity_bindings": CountSnapshot(
                count=await self._count(
                    session, select(func.count()).select_from(IdentityBindingModel)
                ),
            ),
            "spaces": CountSnapshot(
                count=await self._count(
                    session, select(func.count()).select_from(CanonicalSpaceModel)
                ),
            ),
            "space_bindings": CountSnapshot(
                count=await self._count(
                    session, select(func.count()).select_from(SpaceBindingModel)
                ),
            ),
            "presences": CountSnapshot(
                count=await self._count(session, select(func.count()).select_from(PresenceModel)),
            ),
            "conversations": CountSnapshot(
                count=await self._count(
                    session, select(func.count()).select_from(CanonicalConversationModel)
                ),
            ),
        }

    async def read_system(self) -> SystemSnapshot:
        async with self._reader() as session:
            epoch, revision = await self._runtime(session)
            counts = await self._counts(session)
            return SystemSnapshot(
                version=__version__,
                identity_state=epoch,
                identity_revision=revision,
                persons=counts["persons"],
                identity_bindings=counts["identity_bindings"],
                spaces=counts["spaces"],
                space_bindings=counts["space_bindings"],
                presences=counts["presences"],
                conversations=counts["conversations"],
                queue=await self._queue(session),
                pending_restart=await self._pending_restart(session),
            )

    async def read_yuki(self) -> YukiSummaryView:
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            presence_count = await self._count(
                session, select(func.count()).select_from(PresenceModel)
            )
            return YukiSummaryView(
                yuki_count=1, presence_count=presence_count, identity_state=epoch
            )

    async def read_health(self) -> ManagementHealthView:
        reachable = await self._database.ping()
        components = await self._runtime_health() if self._runtime_health is not None else ()
        if not reachable:
            return ManagementHealthView(
                identity_state=StateEpoch.V2,
                identity_revision=None,
                database="unavailable",
                queue=None,
                components=components,
            )
        async with self._reader() as session:
            epoch, revision = await self._runtime(session)
            return ManagementHealthView(
                identity_state=epoch,
                identity_revision=revision,
                database="ok",
                queue=await self._queue(session),
                components=components,
            )

    async def _binding_counts(
        self, session: AsyncSession, person_ids: Sequence[str]
    ) -> dict[str, int]:
        if not person_ids:
            return {}
        rows = await session.execute(
            select(IdentityBindingModel.person_id, func.count())
            .where(IdentityBindingModel.person_id.in_(tuple(person_ids)))
            .group_by(IdentityBindingModel.person_id)
        )
        return {str(person_id): int(count) for person_id, count in rows}

    async def _space_binding_counts(
        self, session: AsyncSession, space_ids: Sequence[str]
    ) -> dict[str, int]:
        if not space_ids:
            return {}
        rows = await session.execute(
            select(SpaceBindingModel.space_id, func.count())
            .where(SpaceBindingModel.space_id.in_(tuple(space_ids)))
            .group_by(SpaceBindingModel.space_id)
        )
        return {str(space_id): int(count) for space_id, count in rows}

    async def _space_external_ids(
        self, session: AsyncSession, space_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        if not space_ids:
            return {}
        rows = await session.execute(
            select(SpaceBindingModel.space_id, SpaceBindingModel.external_space_id).where(
                SpaceBindingModel.space_id.in_(tuple(space_ids))
            )
        )
        grouped: dict[str, list[str]] = {}
        for space_id, external_id in rows:
            grouped.setdefault(str(space_id), []).append(str(external_id))
        return {space_id: tuple(tokens) for space_id, tokens in grouped.items()}

    def _page(
        self,
        items: list[_T],
        *,
        kind: QueryResourceKind,
        phase: QueryCursorPhase,
        next_key: str | None,
        snapshot_at: datetime,
        total: int | None = None,
        number: int | None = None,
    ) -> Page[_T]:
        cursor = None if next_key is None else encode_query_cursor(kind, phase, next_key)
        return Page(
            items,
            next_cursor=cursor if number is None else None,
            snapshot_at=snapshot_at,
            total=total,
            number=number,
        )

    async def list_persons(self, request: PageRequest) -> Page[PersonView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.PERSON, epoch=epoch)
            items: list[PersonView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more, total = await self._keyset(
                    session,
                    CanonicalPersonModel,
                    CanonicalPersonModel.id,
                    CanonicalPersonModel.updated_at,
                    key,
                    request.limit + 1,
                    request=request,
                )
                counts = await self._binding_counts(session, [row.id for row in rows])
                items.extend(
                    PersonView(
                        person_id=PersonId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        enabled=bool(row.enabled),
                        revision=int(row.revision),
                        created_at=_as_aware(row.created_at),
                        updated_at=_as_aware(row.updated_at),
                        binding_count=counts.get(row.id, 0),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.PERSON,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                        total=total,
                        number=request.number,
                    )
            return self._page(
                items,
                kind=QueryResourceKind.PERSON,
                phase=QueryCursorPhase.CANONICAL,
                next_key=None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_identity_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[IdentityBindingView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.BINDING, epoch=epoch)
            items: list[IdentityBindingView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more, total = await self._keyset(
                    session,
                    IdentityBindingModel,
                    IdentityBindingModel.id,
                    IdentityBindingModel.updated_at,
                    key,
                    request.limit + 1,
                    request=request,
                )
                items.extend(
                    IdentityBindingView(
                        binding_id=IdentityBindingId.parse(row.id),
                        person_id=PersonId.parse(row.person_id),
                        resolution=IdentityResolution.CANONICAL,
                        platform=row.platform,
                        external=mask_external_id(row.external_account_id, reveal=reveal_external),
                        display_name=sanitize_projected_display(
                            row.display_name,
                            external_ids=(row.external_account_id,),
                            reveal=reveal_external,
                        ),
                        status=row.status,
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.BINDING,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                        total=total,
                        number=request.number,
                    )
            return self._page(
                items,
                kind=QueryResourceKind.BINDING,
                phase=QueryCursorPhase.CANONICAL,
                next_key=None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_spaces(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.SPACE, epoch=epoch)
            items: list[SpaceView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more, total = await self._keyset(
                    session,
                    CanonicalSpaceModel,
                    CanonicalSpaceModel.id,
                    CanonicalSpaceModel.updated_at,
                    key,
                    request.limit + 1,
                    request=request,
                )
                space_ids = [row.id for row in rows]
                counts = await self._space_binding_counts(session, space_ids)
                externals = (
                    {} if reveal_external else await self._space_external_ids(session, space_ids)
                )
                items.extend(
                    SpaceView(
                        space_id=SpaceId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        name=sanitize_projected_display(
                            row.name,
                            external_ids=externals.get(row.id, ()),
                            reveal=reveal_external,
                        ),
                        enabled=bool(row.enabled),
                        autonomous_enabled=bool(row.autonomous_enabled),
                        require_mention=bool(row.require_mention),
                        revision=int(row.revision),
                        binding_count=counts.get(row.id, 0),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.SPACE,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                        total=total,
                        number=request.number,
                    )
            return self._page(
                items,
                kind=QueryResourceKind.SPACE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_space_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceBindingView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.SPACE_BINDING, epoch=epoch)
            items: list[SpaceBindingView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more, total = await self._keyset(
                    session,
                    SpaceBindingModel,
                    SpaceBindingModel.id,
                    SpaceBindingModel.updated_at,
                    key,
                    request.limit + 1,
                    request=request,
                )
                items.extend(
                    SpaceBindingView(
                        binding_id=SpaceBindingId.parse(row.id),
                        space_id=SpaceId.parse(row.space_id),
                        resolution=IdentityResolution.CANONICAL,
                        platform=row.platform,
                        external=mask_external_id(row.external_space_id, reveal=reveal_external),
                        display_name=sanitize_projected_display(
                            row.display_name,
                            external_ids=(row.external_space_id,),
                            reveal=reveal_external,
                        ),
                        status=row.status,
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.SPACE_BINDING,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                        total=total,
                        number=request.number,
                    )
            return self._page(
                items,
                kind=QueryResourceKind.SPACE_BINDING,
                phase=QueryCursorPhase.CANONICAL,
                next_key=None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_presences(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[PresenceView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.PRESENCE, epoch=epoch)
            if phase is not QueryCursorPhase.CANONICAL:
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
            rows, more, total = await self._keyset(
                session,
                PresenceModel,
                PresenceModel.id,
                PresenceModel.updated_at,
                key,
                request.limit + 1,
                request=request,
            )
            items = [
                PresenceView(
                    presence_id=PresenceId.parse(row.id),
                    platform=row.platform,
                    external=mask_external_id(row.external_account_id, reveal=reveal_external),
                    enabled=bool(row.enabled),
                    ingest_eligible=bool(row.ingest_eligible),
                    revision=int(row.revision),
                    **_presence_connection_fields(self._connections, row),
                )
                for row in rows
            ]
            return self._page(
                items,
                kind=QueryResourceKind.PRESENCE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].id if more else None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_conversations(self, request: PageRequest) -> Page[ConversationView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.CONVERSATION, epoch=epoch)
            items: list[ConversationView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more, total = await self._keyset(
                    session,
                    CanonicalConversationModel,
                    CanonicalConversationModel.id,
                    CanonicalConversationModel.updated_at,
                    key,
                    request.limit + 1,
                    request=request,
                )
                items.extend(
                    ConversationView(
                        conversation_id=ConversationId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        kind=row.kind,
                        person_id=None if row.person_id is None else PersonId.parse(row.person_id),
                        space_id=None if row.space_id is None else SpaceId.parse(row.space_id),
                        generation=ConversationGeneration(int(row.generation)),
                        last_event_id=int(row.last_event_id),
                        starts_after_event_id=int(row.starts_after_event_id),
                        covered_through_event_id=int(row.covered_through_event_id),
                        last_generation_change_event_id=int(row.last_generation_change_event_id),
                        uncovered_event_count=int(row.uncovered_event_count),
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.CONVERSATION,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                        total=total,
                        number=request.number,
                    )
            return self._page(
                items,
                kind=QueryResourceKind.CONVERSATION,
                phase=QueryCursorPhase.CANONICAL,
                next_key=None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_person_active_routes(self, request: PageRequest) -> Page[PersonActiveRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.PERSON_ROUTE, epoch=epoch)
            rows, more, total = await self._keyset(
                session,
                PersonActiveRouteModel,
                PersonActiveRouteModel.person_id,
                PersonActiveRouteModel.updated_at,
                key,
                request.limit + 1,
                request=request,
            )
            binding_ids = [row.identity_binding_id for row in rows]
            presence_ids = [row.presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, IdentityBindingModel, IdentityBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.identity_binding_id)
                presence = presences.get(row.presence_id)
                items.append(
                    PersonActiveRouteView(
                        kind=RouteKind.PERSON_ACTIVE,
                        person_id=PersonId.parse(row.person_id),
                        identity_binding_id=IdentityBindingId.parse(row.identity_binding_id),
                        presence_id=PresenceId.parse(row.presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=row.person_id,
                            actual_owner_id=None if binding is None else binding.person_id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.PERSON_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].person_id if more else None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_space_binding_ingest_routes(
        self, request: PageRequest
    ) -> Page[SpaceBindingIngestRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.INGEST_ROUTE, epoch=epoch)
            rows, more, total = await self._keyset(
                session,
                SpaceBindingIngestRouteModel,
                SpaceBindingIngestRouteModel.space_binding_id,
                SpaceBindingIngestRouteModel.updated_at,
                key,
                request.limit + 1,
                request=request,
            )
            binding_ids = [row.space_binding_id for row in rows]
            presence_ids = [row.ingest_presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, SpaceBindingModel, SpaceBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.space_binding_id)
                presence = presences.get(row.ingest_presence_id)
                items.append(
                    SpaceBindingIngestRouteView(
                        kind=RouteKind.SPACE_BINDING_INGEST,
                        space_binding_id=SpaceBindingId.parse(row.space_binding_id),
                        ingest_presence_id=PresenceId.parse(row.ingest_presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=None,
                            actual_owner_id=None if binding is None else binding.id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.INGEST_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].space_binding_id if more else None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_space_active_routes(self, request: PageRequest) -> Page[SpaceActiveRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.SPACE_ROUTE, epoch=epoch)
            rows, more, total = await self._keyset(
                session,
                SpaceActiveRouteModel,
                SpaceActiveRouteModel.space_id,
                SpaceActiveRouteModel.updated_at,
                key,
                request.limit + 1,
                request=request,
            )
            binding_ids = [row.space_binding_id for row in rows]
            presence_ids = [row.presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, SpaceBindingModel, SpaceBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.space_binding_id)
                presence = presences.get(row.presence_id)
                items.append(
                    SpaceActiveRouteView(
                        kind=RouteKind.SPACE_ACTIVE,
                        space_id=SpaceId.parse(row.space_id),
                        space_binding_id=SpaceBindingId.parse(row.space_binding_id),
                        presence_id=PresenceId.parse(row.presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=row.space_id,
                            actual_owner_id=None if binding is None else binding.space_id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.SPACE_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].space_id if more else None,
                snapshot_at=snapshot_at,
                total=total,
                number=request.number,
            )

    async def list_audit_events(self, request: PageRequest) -> Page[AuditEventView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.AUDIT, epoch=epoch)
            stmt = select(
                AdminOperationEventModel.id,
                AdminOperationEventModel.actor_principal_kind,
                AdminOperationEventModel.actor_principal_id,
                AdminOperationEventModel.control_request_id,
                AdminOperationEventModel.target_id,
                AdminOperationEventModel.capability,
                AdminOperationEventModel.operation,
                AdminOperationEventModel.target_type,
                AdminOperationEventModel.success,
                AdminOperationEventModel.error_category,
                AdminOperationEventModel.duration_seconds,
                AdminOperationEventModel.created_at,
            )
            if key is not None:
                created_at, row_id = decode_time_id_key(key)
                stmt = stmt.where(
                    tuple_(AdminOperationEventModel.created_at, AdminOperationEventModel.id)
                    > (created_at, row_id)
                )
            stmt = stmt.order_by(
                AdminOperationEventModel.created_at.asc(),
                AdminOperationEventModel.id.asc(),
            ).limit(request.limit + 1)
            rows = list(
                await session.execute(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(
                                AdminOperationEventModel.created_at.desc(),
                                AdminOperationEventModel.id.desc(),
                            ),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [project_audit_event(row, snapshot_at=snapshot_at) for row in rows]
            next_key = None
            if more and rows:
                last = rows[-1]
                try:
                    next_key = encode_time_id_key(
                        _as_aware(last.created_at) or snapshot_at, int(last.id)
                    )
                except (TypeError, ValueError) as exc:
                    raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
            return self._page(
                items,
                kind=QueryResourceKind.AUDIT,
                phase=QueryCursorPhase.TIME_ID,
                next_key=next_key,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def read_operation(self, operation_id: str) -> OperationRef:
        async with self._reader() as session:
            return await read_operation(self._database, session, operation_id)

    async def list_operations(
        self,
        request: PageRequest,
        *,
        kind: OperationKind = OperationKind.CONTROL,
    ) -> Page[OperationRef]:
        if type(kind) is not OperationKind:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _ = await self._runtime(session)
            _, key = self._cursor_state(request, QueryResourceKind.OPERATION, epoch=epoch)
            after = 0
            if key is not None:
                cursor_kind, after = decode_operation_cursor_key(key)
                if cursor_kind is not kind:
                    raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
            if kind is OperationKind.CONTROL:
                rows = list(
                    await session.scalars(
                        (
                            sql_window := await numbered_statement(
                                session,
                                select(ControlCommandReceiptModel)
                                .where(ControlCommandReceiptModel.id > after)
                                .order_by(ControlCommandReceiptModel.id)
                                .limit(request.limit + 1),
                                request,
                                order=(
                                    ControlCommandReceiptModel.created_at.desc(),
                                    ControlCommandReceiptModel.id.desc(),
                                ),
                            )
                        ).statement
                    )
                )
                more = len(rows) > request.limit
                window = rows[: request.limit]
                items = [control_operation(row) for row in window]
                last_id = window[-1].id if window else 0
            else:
                model = (
                    MemoryRebuildRunModel if kind is OperationKind.REBUILD else MemoryDreamRunModel
                )
                keys = (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                select(model.id, model.public_id)
                                .where(model.id > after)
                                .order_by(model.id)
                                .limit(request.limit + 1),
                                request,
                                order=(model.created_at.desc(), model.id.desc()),
                            )
                        ).statement
                    )
                ).all()
                more = len(keys) > request.limit
                selected = keys[: request.limit]
                items = [
                    await read_operation(self._database, session, f"{kind.value}:{public_id}")
                    for _, public_id in selected
                ]
                last_id = selected[-1][0] if selected else 0
            return self._page(
                items,
                kind=QueryResourceKind.OPERATION,
                phase=QueryCursorPhase.CANONICAL,
                next_key=f"{kind.value}:{last_id}" if more else None,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def list_config_specs(self, request: PageRequest) -> Page[ConfigSpecView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.CONFIG_SPEC, epoch=epoch)
            overrides = {
                (
                    row.config_key,
                    row.scope_type,
                    row.canonical_person_id or row.canonical_space_id or "",
                )
                for row in (await session.scalars(select(RuntimeConfigOverrideModel))).all()
            }
        specs = sorted(self._registry.list(), key=lambda item: item.key)
        if key is not None:
            specs = [item for item in specs if item.key > key]
        total = len(specs)
        start = (request.number - 1) * request.limit if request.number else 0
        window = specs[start : start + request.limit + 1]
        more = len(window) == request.limit + 1
        if more:
            window = window[:-1]
        items = [
            ConfigSpecView(
                key=item.key,
                category=item.category or "general",
                apply_mode=item.apply_mode.value,
                value_type=item.value_type,
                mutable=item.mutable,
                sensitive=item.sensitive or item.apply_mode.value == "secret",
                configured=_spec_is_configured(item, overrides, self._settings),
                display_name=item.display_name,
                description=item.description,
                minimum=item.minimum,
                maximum=item.maximum,
                choices=item.choices,
                allowed_scopes=tuple(scope.value for scope in item.allowed_scopes),
            )
            for item in window
        ]
        return self._page(
            items,
            kind=QueryResourceKind.CONFIG_SPEC,
            phase=QueryCursorPhase.CANONICAL,
            next_key=window[-1].key if more else None,
            snapshot_at=snapshot_at,
            total=total,
            number=request.number,
        )

    async def list_effective_configs(
        self, request: PageRequest, *, scope: ConfigQueryScope | None = None
    ) -> Page[EffectiveConfigView]:
        if scope is None:
            scope = ConfigQueryScope()
        if type(scope) is not ConfigQueryScope:
            raise TypeError("scope must be ConfigQueryScope")
        if self._config is None:
            raise ControlQueryError(Problem(ProblemCode.PRECONDITION_FAILED))
        snapshot_at = _now()
        scope_key = (
            sha256(
                (
                    f"{scope.person_id.text if scope.person_id else ''}/"
                    f"{scope.space_id.text if scope.space_id else ''}"
                ).encode()
            ).hexdigest()
            + ":"
        )
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(
                request, QueryResourceKind.CONFIG_EFFECTIVE, epoch=epoch
            )
            if key is not None:
                if not key.startswith(scope_key):
                    raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
                key = key[len(scope_key) :]
            specs = sorted(self._config.registry.list(), key=lambda item: item.key)
            if key is not None:
                specs = [item for item in specs if item.key > key]
            total = len(specs)
            start = (request.number - 1) * request.limit if request.number else 0
            window = specs[start : start + request.limit + 1]
            more = len(window) == request.limit + 1
            if more:
                window = window[:-1]
            try:
                inspected = await self._config.inspect_configs(
                    tuple(item.key for item in window),
                    scope=MemoryConfigScope(
                        person_id=scope.person_id.text if scope.person_id else None,
                        space_id=scope.space_id.text if scope.space_id else None,
                    ),
                    session=session,
                )
            except CanonicalIdentityError as exc:
                raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
            items = []
            for spec, result in zip(window, inspected, strict=True):
                effective, saved = result.effective, result.saved
                scope_type = saved.scope_type.value if saved.scope_type else "global"
                owner_kind = {"user": ConfigOwnerKind.PERSON, "group": ConfigOwnerKind.SPACE}.get(
                    scope_type, ConfigOwnerKind.GLOBAL
                )
                items.append(
                    EffectiveConfigView(
                        key=spec.key,
                        source=effective.source,
                        scope_type=scope_type,
                        apply_mode=spec.apply_mode.value,
                        configured=effective.configured
                        if effective.configured is not None
                        else effective.value is not None,
                        pending_restart=result.pending_restart,
                        version=result.version,
                        value=effective.value,
                        saved_value=saved.value,
                        saved_source=saved.source,
                        owner_kind=owner_kind,
                        person_id=scope.person_id if owner_kind is ConfigOwnerKind.PERSON else None,
                        space_id=scope.space_id if owner_kind is ConfigOwnerKind.SPACE else None,
                        owner_resolution=IdentityResolution.CANONICAL,
                    )
                )
        return self._page(
            items,
            kind=QueryResourceKind.CONFIG_EFFECTIVE,
            phase=QueryCursorPhase.CANONICAL,
            next_key=scope_key + window[-1].key if more else None,
            snapshot_at=snapshot_at,
            total=total,
            number=request.number,
        )

    async def list_config_overrides(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[ConfigOverrideView]:
        del reveal_external
        snapshot_at = _now()
        specs = {item.key: item for item in self._registry.list()}
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.CONFIG, epoch=epoch)
            after = _decode_config_override_cursor(key)
            stmt = select(RuntimeConfigOverrideModel)
            if after:
                stmt = stmt.where(RuntimeConfigOverrideModel.id > after)
            stmt = stmt.order_by(RuntimeConfigOverrideModel.id.asc()).limit(request.limit + 1)
            rows = list(
                await session.scalars(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(
                                RuntimeConfigOverrideModel.updated_at.desc(),
                                RuntimeConfigOverrideModel.id.desc(),
                            ),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [
                _project_config_override(
                    row,
                    spec=specs.get(str(row.config_key)),
                )
                for row in rows
            ]
            next_key = None
            if more and rows:
                next_key = f"{_CONFIG_OVERRIDE_CURSOR_PREFIX}{rows[-1].id}"
            return self._page(
                items,
                kind=QueryResourceKind.CONFIG,
                phase=QueryCursorPhase.CANONICAL,
                next_key=next_key,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def read_self_reflection_health(self) -> ActivityView:
        return await self._reflection_details.read_self_reflection_health()

    async def list_self_reflection_history(
        self, request: PageRequest, *, section: str, scope: ReflectionQueryFilter | None = None
    ) -> Page[ActivityView]:
        return await self._reflection_details.list_self_reflection_history(
            request, section=section, scope=scope
        )

    async def list_memory_facts(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryFactView]:
        return await self._memory_details.list_memory_facts(
            request, include_content=include_content, scope=scope
        )

    async def list_memory_evidence(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryEvidenceView]:
        return await self._memory_details.list_memory_evidence(
            request, include_content=include_content, scope=scope
        )

    async def read_memory_fact(self, fact_id: int, *, include_content: bool) -> ActivityView:
        return await self._memory_details.read_memory_fact(fact_id, include_content=include_content)

    async def list_memory_jobs(self, request: PageRequest) -> Page[MemoryJobView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.MEMORY_JOB, epoch=epoch)
            after = decode_integer_cursor_key(key, minimum=1) if key is not None else 0
            stmt = select(MemoryJobModel)
            if after:
                stmt = stmt.where(MemoryJobModel.id > after)
            stmt = stmt.order_by(MemoryJobModel.id.asc()).limit(request.limit + 1)
            rows = list(
                await session.scalars(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(MemoryJobModel.created_at.desc(), MemoryJobModel.id.desc()),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [_memory_job_view(row) for row in rows]
            return self._page(
                items,
                kind=QueryResourceKind.MEMORY_JOB,
                phase=QueryCursorPhase.CANONICAL,
                next_key=str(rows[-1].id) if more else None,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def read_memory_health(self) -> MemoryHealthView:
        async with self._reader() as session:
            await self._runtime(session)
        index = "unavailable"
        try:
            health = await SQLiteMemoryFTSIndex(self._database).health()
            index = "ok" if health.healthy else "degraded"
        except (MemoryRetrievalError, DatabaseError, RuntimeError, ValueError):
            index = "unavailable"
        embedding = "unavailable"
        embedding_report = None
        try:
            enabled = bool(getattr(self._settings, "memory_embedding_enabled", False))
            embedding_report = (
                await self._embeddings.health()
                if self._embeddings is not None
                else await MemoryEmbeddingHealthService(
                    enabled=enabled,
                    provider=None,
                    repository=MemoryEmbeddingRepository(self._database),
                    profile_id=None,
                    documents=EmbeddingDocumentBuilder(template_version=1, max_characters=2000),
                ).health()
            )
            if not enabled:
                embedding = "disabled"
            elif not embedding_report.provider_configured:
                embedding = "not_configured"
            elif embedding_report.coverage_ratio < 1 or embedding_report.failed_job_count:
                embedding = "degraded"
            else:
                embedding = "ok"
        except (RuntimeError, ValueError, DatabaseError):
            embedding = "unavailable"
        embedding_saved_enabled = None
        embedding_config_version = None
        embedding_pending_restart = False
        if self._config is not None:
            inspected = await self._config.inspect_configs(("memory.embedding_enabled",))
            setting = inspected[0]
            embedding_saved_enabled = bool(setting.saved.value)
            embedding_config_version = setting.version
            embedding_pending_restart = setting.pending_restart
        consistency = "unavailable"
        try:
            consistency_health = await MemoryAuditService(
                MemoryFactRepository(self._database)
            ).health()
            consistency = "ok" if consistency_health.healthy else "degraded"
        except (RuntimeError, ValueError, DatabaseError):
            consistency = "unavailable"
        return MemoryHealthView(
            index=index,
            embedding=embedding,
            consistency=consistency,
            embedding_requested=(embedding_report.enabled if embedding_report else None),
            embedding_configured=(
                embedding_report.provider_configured if embedding_report else None
            ),
            embedding_ready_count=(
                embedding_report.ready_embedding_count if embedding_report else None
            ),
            embedding_fact_count=(embedding_report.active_fact_count if embedding_report else None),
            embedding_failed_jobs=(embedding_report.failed_job_count if embedding_report else None),
            embedding_last_error=(
                embedding_report.last_error_category if embedding_report else None
            ),
            embedding_saved_enabled=embedding_saved_enabled,
            embedding_config_version=embedding_config_version,
            embedding_pending_restart=embedding_pending_restart,
        )

    async def list_automations(self, request: PageRequest) -> Page[AutomationView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.AUTOMATION, epoch=epoch)
            after = decode_integer_cursor_key(key, minimum=1) if key is not None else 0
            stmt = select(AutomationModel)
            if after:
                stmt = stmt.where(AutomationModel.id > after)
            stmt = stmt.order_by(AutomationModel.id.asc()).limit(request.limit + 1)
            rows = list(
                await session.scalars(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(AutomationModel.updated_at.desc(), AutomationModel.id.desc()),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            person_ids = [
                str(row.canonical_target_person_id)
                for row in rows
                if row.canonical_target_person_id
            ]
            space_ids = [
                str(row.canonical_target_space_id) for row in rows if row.canonical_target_space_id
            ]
            person_routes = {
                item.person_id: item
                for item in (
                    list(
                        await session.scalars(
                            select(PersonActiveRouteModel).where(
                                PersonActiveRouteModel.person_id.in_(tuple(person_ids))
                            )
                        )
                    )
                    if person_ids
                    else []
                )
            }
            space_routes = {
                item.space_id: item
                for item in (
                    list(
                        await session.scalars(
                            select(SpaceActiveRouteModel).where(
                                SpaceActiveRouteModel.space_id.in_(tuple(space_ids))
                            )
                        )
                    )
                    if space_ids
                    else []
                )
            }
            items = [
                AutomationView(
                    automation_id=int(row.id),
                    revision=state_revision(row.updated_at),
                    name=str(row.name),
                    status=str(row.status),
                    run_count=int(row.run_count),
                    script_hash=str(row.script_hash),
                    target_kind=_automation_target_kind(row),
                    target_id=_automation_target_id(row),
                    route_state=_automation_route_state(
                        row,
                        person_route=person_routes.get(str(row.canonical_target_person_id or "")),
                        space_route=space_routes.get(str(row.canonical_target_space_id or "")),
                    ),
                )
                for row in rows
            ]
            return self._page(
                items,
                kind=QueryResourceKind.AUTOMATION,
                phase=QueryCursorPhase.CANONICAL,
                next_key=str(rows[-1].id) if more else None,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def list_plugins(self, request: PageRequest) -> Page[PluginView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.PLUGIN, epoch=epoch)
            stmt = select(PluginInstallationModel)
            if key is not None:
                stmt = stmt.where(PluginInstallationModel.plugin_id > key)
            stmt = stmt.order_by(PluginInstallationModel.plugin_id.asc()).limit(request.limit + 1)
            rows = list(
                await session.scalars(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(
                                PluginInstallationModel.updated_at.desc(),
                                PluginInstallationModel.plugin_id.desc(),
                            ),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [
                PluginView(
                    plugin_id=str(row.plugin_id),
                    revision=state_revision(row.updated_at),
                    name=str(row.name),
                    version=str(row.version),
                    status=str(row.status),
                    enabled=bool(row.enabled),
                )
                for row in rows
            ]
            return self._page(
                items,
                kind=QueryResourceKind.PLUGIN,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].plugin_id if more else None,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )

    async def read_plugin_approval(self, plugin_id: str) -> ActivityView:
        if self._plugins is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        before = await self._plugins.show(plugin_id)
        report = await self._plugins.doctor(plugin_id)
        record = await self._plugins.show(plugin_id)
        if record is None or before is None:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        if (before.updated_at, before.manifest_hash) != (record.updated_at, record.manifest_hash):
            raise ControlQueryError(Problem(ProblemCode.VERSION_CONFLICT))
        return ActivityView(
            plugin_id,
            {
                "revision": state_revision(record.updated_at),
                "manifest_hash": record.manifest_hash,
                "requested_permissions": list(report.requested_permissions),
                "approved_permissions": list(report.approved_permissions),
                "manifest_hash_matches": report.manifest_hash_matches,
                "manifest_available": report.manifest_available,
                "status": record.status,
            },
        )

    async def read_plugin_runtime(self, plugin_id: str) -> PluginRuntimeView:
        if self._plugins is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        report = await self._plugins.doctor(plugin_id)
        return PluginRuntimeView(**report.model_dump())

    async def read_plugin_configuration(
        self, plugin_id: str, *, scope_type: str = "global", owner_id: str | None = None
    ) -> ActivityView:
        if self._plugins is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        try:
            fields = await PluginConfigurationService(
                self._database, self._plugins.configuration_schema
            ).read(plugin_id, scope_type=scope_type, owner_id=owner_id)
            return ActivityView(plugin_id, fields)
        except PluginConfigurationError as exc:
            raise ControlQueryError(Problem(ProblemCode(exc.category))) from None

    async def read_plugin_observation(
        self, plugin_id: str, *, cursor: str | None = None, limit: int = 10
    ) -> ActivityView:
        if self._plugins is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        if (
            type(plugin_id) is not str
            or not plugin_id
            or len(plugin_id) > 128
            or type(limit) is not int
        ):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        try:
            request = PluginObservationRequest(cursor=cursor, limit=limit)
        except ValueError:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from None
        try:
            return ActivityView(plugin_id, await self._plugins.observe(plugin_id, request))
        except PluginManagementRejected:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from None
        except Exception:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from None

    async def list_emoji_assets(
        self,
        request: PageRequest,
        *,
        reveal_first_seen_person: bool,
        reveal_first_seen_space: bool,
    ) -> Page[EmojiAssetView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.EMOJI, epoch=epoch)
            stmt = select(EmojiAssetModel)
            if key is not None:
                stmt = stmt.where(EmojiAssetModel.id > key)
            stmt = stmt.order_by(EmojiAssetModel.id.asc()).limit(request.limit + 1)
            rows = list(
                await session.scalars(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt,
                            request,
                            order=(EmojiAssetModel.updated_at.desc(), EmojiAssetModel.id.desc()),
                        )
                    ).statement
                )
            )
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            scope_rows = (
                list(
                    await session.scalars(
                        select(EmojiScopeStateModel).where(
                            EmojiScopeStateModel.emoji_id.in_(tuple(row.id for row in rows))
                        )
                    )
                )
                if rows
                else []
            )
            scopes_by_asset: dict[str, list[EmojiScopeStateModel]] = {}
            for scope in scope_rows:
                scopes_by_asset.setdefault(str(scope.emoji_id), []).append(scope)
            items = [
                _project_emoji_asset(
                    row,
                    scope_rows=scopes_by_asset.get(str(row.id), []),
                    reveal_first_seen_person=reveal_first_seen_person,
                    reveal_first_seen_space=reveal_first_seen_space,
                )
                for row in rows
            ]
            return self._page(
                items,
                kind=QueryResourceKind.EMOJI,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].id if more else None,
                snapshot_at=snapshot_at,
                total=sql_window.total,
                number=request.number,
            )
