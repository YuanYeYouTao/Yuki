"""Control query port. Implementations live outside this package."""

from __future__ import annotations

from typing import Protocol

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
    PersonActiveRouteView,
    PersonView,
    PluginRuntimeView,
    PluginView,
    PresenceView,
    SocialReceiptView,
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SpeechProfileView,
    SystemSnapshot,
    YukiSummaryView,
)
from qq_ai_bot.domain.identity import ConversationId


class ControlQueryPort(Protocol):
    """Read-only projections. Must not return catalog rows or session objects."""

    async def download_workspace(self, artifact_id: str) -> DownloadView: ...
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

    async def list_work(
        self, request: PageRequest, *, include_content: bool = False
    ) -> Page[ActivityView]: ...
    async def read_automation(self, automation_id: int) -> ActivityView: ...
    async def list_model_usage(self, request: PageRequest) -> Page[ActivityView]: ...
    async def list_workspace(self, request: PageRequest) -> Page[ActivityView]: ...
    async def read_workspace(self, artifact_id: str) -> ActivityView: ...

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

    async def list_memory_facts(
        self,
        request: PageRequest,
        *,
        include_content: bool,
    ) -> Page[MemoryFactView]: ...

    async def list_memory_evidence(
        self,
        request: PageRequest,
        *,
        include_content: bool,
    ) -> Page[MemoryEvidenceView]: ...

    async def list_memory_jobs(self, request: PageRequest) -> Page[MemoryJobView]: ...

    async def read_memory_health(self) -> MemoryHealthView: ...

    async def list_automations(self, request: PageRequest) -> Page[AutomationView]: ...

    async def list_plugins(self, request: PageRequest) -> Page[PluginView]: ...

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
