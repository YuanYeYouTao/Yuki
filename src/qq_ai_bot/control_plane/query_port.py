"""Control query port. Implementations live outside this package."""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.control_plane.json_types import JsonObject
from qq_ai_bot.control_plane.operations import OperationKind, OperationRef
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    AuditEventView,
    AutomationView,
    ChatEventView,
    ChatHistoryFilter,
    ConfigOverrideView,
    ConfigQueryScope,
    ConfigSpecView,
    ConversationView,
    DownloadView,
    EffectiveConfigView,
    EmojiAssetView,
    ExecutionTraceFilter,
    ExecutionTraceView,
    IdentityBindingView,
    ManagementHealthView,
    McpServerView,
    MemoryEvidenceView,
    MemoryFactView,
    MemoryHealthView,
    MemoryJobView,
    MemoryQueryFilter,
    PersonActiveRouteView,
    PersonView,
    PluginRuntimeView,
    PluginView,
    PresenceView,
    ReflectionQueryFilter,
    SocialReceiptView,
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SpeechProfileView,
    SystemSnapshot,
    YukiSummaryView,
)
from qq_ai_bot.domain.identity import ConversationId, PersonId, PrincipalId, RequestId


class ControlQueryPort(Protocol):
    """Read-only projections. Must not return catalog rows or session objects."""

    async def read_conversation_execution(
        self,
        conversation_id: ConversationId,
        *,
        include_content: bool = False,
        turn_id: str | None = None,
        before_step_id: int | None = None,
    ) -> ActivityView: ...
    async def list_event_turns(
        self,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        event_id: int,
        direction: str,
    ) -> Page[ActivityView]: ...

    async def list_plugin_outbox(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]: ...
    async def list_plugin_background_turns(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]: ...
    async def list_participation_feedback(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]: ...

    async def read_plugin_observation(
        self, plugin_id: str, *, cursor: str | None = None, limit: int = 10
    ) -> ActivityView: ...

    async def read_plugin_configuration(
        self, plugin_id: str, *, scope_type: str = "global", owner_id: str | None = None
    ) -> ActivityView: ...

    async def download_workspace(self, artifact_id: str) -> DownloadView: ...
    async def read_display_names(
        self, references: JsonObject, allowed: frozenset[str]
    ) -> ActivityView: ...
    async def download_avatar(self, kind: str, owner_id: str) -> DownloadView: ...
    async def download_emoji(self, asset_id: str) -> DownloadView: ...
    async def download_environment_file(self, path: str) -> DownloadView: ...
    async def download_chat_media(
        self, conversation_id: ConversationId, event_id: int, attachment_index: int
    ) -> DownloadView: ...

    async def read_model_catalog(self) -> ActivityView: ...
    async def read_config_file(self, file_id: str) -> ActivityView: ...

    async def read_persona(self) -> ActivityView: ...

    async def list_participation_runs(
        self, request: PageRequest, *, conversation_id: ConversationId | None = None
    ) -> Page[ActivityView]: ...

    async def read_participation(self) -> ActivityView: ...

    async def read_work(self, work_id: str, *, include_content: bool = False) -> ActivityView: ...

    async def list_work_history(
        self, request: PageRequest, *, work_id: str, section: str, include_content: bool = False
    ) -> Page[ActivityView]: ...

    async def list_work(
        self, request: PageRequest, *, include_content: bool = False
    ) -> Page[ActivityView]: ...
    async def read_automation(self, automation_id: int) -> ActivityView: ...
    async def read_automation_schema(self) -> ActivityView: ...
    async def read_memory_maintenance_schema(self) -> ActivityView: ...
    async def list_memory_rebuild_proposals(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]: ...

    async def read_memory_maintenance_run(self, operation_id: str) -> ActivityView: ...
    async def list_model_usage(self, request: PageRequest) -> Page[ActivityView]: ...
    async def read_model_usage_summary(self, window: str) -> ActivityView: ...
    async def list_workspace(self, request: PageRequest) -> Page[ActivityView]: ...
    async def read_terminal_submission(
        self, request_id: RequestId, principal_id: PrincipalId
    ) -> ActivityView: ...

    async def read_environment(self, section: str, arguments: JsonObject) -> ActivityView: ...

    async def read_workspace(self, artifact_id: str) -> ActivityView: ...

    async def list_automation_runs(
        self, request: PageRequest, *, automation_id: int
    ) -> Page[ActivityView]: ...
    async def list_automation_steps(
        self, request: PageRequest, *, automation_id: int, run_id: int | None = None
    ) -> Page[ActivityView]: ...

    async def list_execution_trace(
        self, request: PageRequest, *, scope: ExecutionTraceFilter, include_content: bool = False
    ) -> Page[ExecutionTraceView]: ...

    async def read_execution_trace(
        self, entry_id: int, *, conversation_id: ConversationId | None = None
    ) -> ExecutionTraceView: ...

    async def list_chat_events(
        self,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        include_content: bool = False,
        history: ChatHistoryFilter | None = None,
    ) -> Page[ChatEventView]: ...

    async def list_social_receipts(
        self, request: PageRequest, *, conversation_id: ConversationId
    ) -> Page[SocialReceiptView]: ...

    async def read_system(self) -> SystemSnapshot: ...

    async def read_yuki(self) -> YukiSummaryView: ...

    async def read_health(self) -> ManagementHealthView: ...

    async def list_persons(self, request: PageRequest) -> Page[PersonView]: ...

    async def list_identity_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[IdentityBindingView]: ...

    async def list_spaces(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceView]: ...

    async def list_space_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceBindingView]: ...

    async def list_presences(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[PresenceView]: ...

    async def list_conversations(self, request: PageRequest) -> Page[ConversationView]: ...

    async def list_person_active_routes(
        self, request: PageRequest
    ) -> Page[PersonActiveRouteView]: ...

    async def list_space_binding_ingest_routes(
        self, request: PageRequest
    ) -> Page[SpaceBindingIngestRouteView]: ...

    async def list_space_active_routes(
        self, request: PageRequest
    ) -> Page[SpaceActiveRouteView]: ...

    async def list_audit_events(self, request: PageRequest) -> Page[AuditEventView]: ...

    async def list_operations(
        self, request: PageRequest, *, kind: OperationKind = OperationKind.CONTROL
    ) -> Page[OperationRef]: ...

    async def read_operation(self, operation_id: str) -> OperationRef: ...

    async def list_config_specs(self, request: PageRequest) -> Page[ConfigSpecView]: ...

    async def list_effective_configs(
        self, request: PageRequest, *, scope: ConfigQueryScope | None = None
    ) -> Page[EffectiveConfigView]: ...

    async def list_config_overrides(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[ConfigOverrideView]: ...

    async def read_self_reflection_health(self) -> ActivityView: ...
    async def list_self_reflection_history(
        self, request: PageRequest, *, section: str, scope: ReflectionQueryFilter | None = None
    ) -> Page[ActivityView]: ...

    async def list_relationships(self, request: PageRequest) -> Page[ActivityView]: ...
    async def read_relationship(self, person_id: PersonId) -> ActivityView: ...
    async def list_relationship_history(
        self, request: PageRequest, *, person_id: PersonId, section: str
    ) -> Page[ActivityView]: ...

    async def list_memory_facts(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryFactView]: ...

    async def list_memory_evidence(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryEvidenceView]: ...

    async def read_memory_fact(self, fact_id: int, *, include_content: bool) -> ActivityView: ...

    async def list_memory_jobs(self, request: PageRequest) -> Page[MemoryJobView]: ...

    async def read_memory_health(self) -> MemoryHealthView: ...

    async def list_automations(self, request: PageRequest) -> Page[AutomationView]: ...

    async def list_plugins(self, request: PageRequest) -> Page[PluginView]: ...

    async def read_plugin_approval(self, plugin_id: str) -> ActivityView: ...

    async def read_plugin_runtime(self, plugin_id: str) -> PluginRuntimeView: ...

    async def list_mcp_servers(self, request: PageRequest) -> Page[McpServerView]: ...

    async def list_emoji_assets(
        self,
        request: PageRequest,
        *,
        reveal_first_seen_person: bool,
        reveal_first_seen_space: bool,
    ) -> Page[EmojiAssetView]: ...

    async def list_speech_profiles(self, request: PageRequest) -> Page[SpeechProfileView]: ...
