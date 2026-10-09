"""Application query services. Default-deny over DecisionContext."""

from __future__ import annotations

from qq_ai_bot.control_plane.decision import decide
from qq_ai_bot.control_plane.json_types import JsonObject
from qq_ai_bot.control_plane.operations import OperationKind, OperationRef
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_port import ControlQueryPort
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    AuditEventView,
    AutomationView,
    ChatEventView,
    ChatHistoryFilter,
    ConfigOverrideView,
    ConfigQueryScope,
    ConfigSpecView,
    ControlQueryError,
    ConversationView,
    DownloadView,
    EffectiveConfigView,
    EmojiAssetView,
    ExecutionTraceFilter,
    ExecutionTraceView,
    IdentityBindingView,
    ManagementHealthView,
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
    SystemSnapshot,
    YukiSummaryView,
)
from qq_ai_bot.control_plane.surface import ControlSurfaceView, describe_surface, method_capability
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.identity import ConversationId, RequestId


def _require_context(context: object) -> DecisionContext[ControlPrincipal, object, object]:
    if type(context) is not DecisionContext:
        raise TypeError("context must be DecisionContext")
    if type(context.principal) is not ControlPrincipal:
        raise TypeError("principal must be ControlPrincipal")
    return context


def _require_capability(
    context: DecisionContext[ControlPrincipal, object, object],
    capability: str,
) -> None:
    decision = decide(context, capability)
    if decision.allowed:
        return
    problem = (
        decision.problem if decision.problem is not None else Problem(ProblemCode.CAPABILITY_DENIED)
    )
    raise ControlQueryError(problem)


def _reveal_external(context: DecisionContext[ControlPrincipal, object, object]) -> bool:
    return context.principal.allows("identity.binding.read_external")


class ControlQueryService:
    """Authorize then project. Does not invent principals or actors."""

    async def read_conversation_execution(
        self,
        context: object,
        conversation_id: ConversationId,
        *,
        include_content: bool = False,
        turn_id: str | None = None,
        before_step_id: int | None = None,
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_conversation_execution"))
        if type(include_content) is not bool:
            raise TypeError("include_content must be bool")
        if include_content:
            _require_capability(authorized, "control.chat.content.read")
        if turn_id is None and before_step_id is None:
            return await self._port.read_conversation_execution(
                conversation_id, include_content=include_content
            )
        return await self._port.read_conversation_execution(
            conversation_id,
            include_content=include_content,
            turn_id=turn_id,
            before_step_id=before_step_id,
        )

    async def list_event_turns(
        self,
        context: object,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        event_id: int,
        direction: str,
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_event_turns"))
        return await self._port.list_event_turns(
            request, conversation_id=conversation_id, event_id=event_id, direction=direction
        )

    async def read_display_names(self, context: object, references: JsonObject) -> ActivityView:
        from qq_ai_bot.persistence.control_names import NAME_CAPABILITIES

        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_display_names"))
        allowed = frozenset(
            kind
            for kind, capability in NAME_CAPABILITIES.items()
            if authorized.principal.allows(capability)
        )
        return await self._port.read_display_names(references, allowed)

    async def download_emoji(self, context: object, asset_id: str) -> DownloadView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("download_emoji"))
        return await self._port.download_emoji(asset_id)

    async def download_avatar(self, context: object, kind: str, owner_id: str) -> DownloadView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("download_avatar"))
        return await self._port.download_avatar(kind, owner_id)

    async def download_environment_file(self, context: object, path: str) -> DownloadView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("download_environment_file"))
        return await self._port.download_environment_file(path)

    async def list_plugin_background_turns(
        self, context: object, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_plugin_background_turns"))
        return await self._port.list_plugin_background_turns(request, plugin_id=plugin_id)

    async def list_participation_feedback(
        self, context: object, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_participation_feedback"))
        if type(include_content) is not bool:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        return await self._port.list_participation_feedback(
            request,
            run_id=run_id,
            include_content=include_content
            and authorized.principal.allows("control.execution.content.read"),
        )

    async def list_plugin_outbox(
        self, context: object, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_plugin_outbox"))
        return await self._port.list_plugin_outbox(request, plugin_id=plugin_id)

    async def read_plugin_observation(
        self, context: object, plugin_id: str, *, cursor: str | None = None, limit: int = 10
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_plugin_observation"))
        return await self._port.read_plugin_observation(plugin_id, cursor=cursor, limit=limit)

    async def read_plugin_configuration(
        self,
        context: object,
        plugin_id: str,
        *,
        scope_type: str = "global",
        owner_id: str | None = None,
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_plugin_configuration"))
        return await self._port.read_plugin_configuration(
            plugin_id, scope_type=scope_type, owner_id=owner_id
        )

    def __init__(self, port: ControlQueryPort) -> None:
        if port is None:
            raise TypeError("port is required")
        self._port = port

    async def download_workspace(self, context: object, artifact_id: str) -> DownloadView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("download_workspace"))
        return await self._port.download_workspace(artifact_id)

    async def download_chat_media(
        self, context: object, conversation_id: ConversationId, event_id: int, attachment_index: int
    ) -> DownloadView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("download_chat_media"))
        return await self._port.download_chat_media(conversation_id, event_id, attachment_index)

    async def read_model_catalog(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_model_catalog"))
        return await self._port.read_model_catalog()

    async def read_config_file(self, context: object, file_id: str) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_config_file"))
        return await self._port.read_config_file(file_id)

    async def read_persona(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_persona"))
        return await self._port.read_persona()

    async def list_participation_runs(
        self,
        context: object,
        request: PageRequest,
        *,
        conversation_id: ConversationId | None = None,
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_participation_runs"))
        return await self._port.list_participation_runs(request, conversation_id=conversation_id)

    async def read_participation(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_participation"))
        return await self._port.read_participation()

    async def read_work(
        self, context: object, work_id: str, *, include_content: bool = False
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_work"))
        if type(include_content) is not bool:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if include_content:
            _require_capability(authorized, "control.execution.content.read")
        return await self._port.read_work(work_id, include_content=include_content)

    async def list_work_history(
        self,
        context: object,
        request: PageRequest,
        *,
        work_id: str,
        section: str,
        include_content: bool = False,
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_work_history"))
        if type(include_content) is not bool:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if include_content:
            _require_capability(authorized, "control.execution.content.read")
        return await self._port.list_work_history(
            request, work_id=work_id, section=section, include_content=include_content
        )

    async def list_work(
        self, context: object, request: PageRequest, *, include_content: bool = False
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_work"))
        if type(include_content) is not bool:
            raise TypeError("include_content must be bool")
        if include_content:
            _require_capability(authorized, "control.execution.content.read")
        return await self._port.list_work(request, include_content=include_content)

    async def read_automation_schema(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_automation_schema"))
        return await self._port.read_automation_schema()

    async def read_memory_maintenance_schema(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_memory_maintenance_schema"))
        return await self._port.read_memory_maintenance_schema()

    async def list_memory_rebuild_proposals(
        self, context: object, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_rebuild_proposals"))
        if type(include_content) is not bool:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        return await self._port.list_memory_rebuild_proposals(
            request,
            run_id=run_id,
            include_content=include_content
            and authorized.principal.allows("control.memory.content.read"),
        )

    async def read_memory_maintenance_run(self, context: object, operation_id: str) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_memory_maintenance_run"))
        return await self._port.read_memory_maintenance_run(operation_id)

    async def read_automation(self, context: object, automation_id: int) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_automation"))
        return await self._port.read_automation(automation_id)

    async def list_automation_runs(
        self, context: object, request: PageRequest, *, automation_id: int
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_automation_runs"))
        return await self._port.list_automation_runs(request, automation_id=automation_id)

    async def list_automation_steps(
        self,
        context: object,
        request: PageRequest,
        *,
        automation_id: int,
        run_id: int | None = None,
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_automation_steps"))
        return await self._port.list_automation_steps(
            request, automation_id=automation_id, run_id=run_id
        )

    async def list_model_usage(self, context: object, request: PageRequest) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_model_usage"))
        return await self._port.list_model_usage(request)

    async def read_model_usage_summary(self, context: object, window: str) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_model_usage_summary"))
        return await self._port.read_model_usage_summary(window)

    async def list_workspace(self, context: object, request: PageRequest) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_workspace"))
        return await self._port.list_workspace(request)

    async def read_terminal_submission(
        self, context: object, request_id: RequestId
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_terminal_submission"))
        if type(request_id) is not RequestId:
            raise TypeError("request_id must be RequestId")
        return await self._port.read_terminal_submission(
            request_id, authorized.principal.principal_id
        )

    async def read_environment(
        self, context: object, section: str, arguments: JsonObject
    ) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_environment"))
        if section in {"files", "file"}:
            _require_capability(authorized, "control.workspace.content.read")
        elif section == "terminal":
            _require_capability(authorized, "control.terminal.content.read")
        return await self._port.read_environment(section, arguments)

    async def read_workspace(self, context: object, artifact_id: str) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_workspace"))
        return await self._port.read_workspace(artifact_id)

    async def list_execution_trace(
        self,
        context: object,
        request: PageRequest,
        *,
        scope: ExecutionTraceFilter,
        include_content: bool = False,
    ) -> Page[ExecutionTraceView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_execution_trace"))
        if type(include_content) is not bool:
            raise TypeError("include_content must be bool")
        if include_content:
            _require_capability(authorized, "control.execution.content.read")
        return await self._port.list_execution_trace(
            request, scope=scope, include_content=include_content
        )

    async def read_execution_trace(
        self, context: object, entry_id: int, *, conversation_id: ConversationId | None = None
    ) -> ExecutionTraceView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_execution_trace"))
        return await self._port.read_execution_trace(entry_id, conversation_id=conversation_id)

    async def list_chat_events(
        self,
        context: object,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        include_content: bool = False,
        history: ChatHistoryFilter | None = None,
    ) -> Page[ChatEventView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_chat_events"))
        if type(include_content) is not bool:
            raise TypeError("include_content must be bool")
        if include_content:
            _require_capability(authorized, "control.chat.content.read")
        options = {} if history is None else {"history": history}
        return await self._port.list_chat_events(
            request, conversation_id=conversation_id, include_content=include_content, **options
        )

    async def list_social_receipts(
        self, context: object, request: PageRequest, *, conversation_id: ConversationId
    ) -> Page[SocialReceiptView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_social_receipts"))
        return await self._port.list_social_receipts(request, conversation_id=conversation_id)

    def describe(self, context: object) -> ControlSurfaceView:
        authorized = _require_context(context)
        if authorized.source is not authorized.principal.source:
            raise ValueError("decision source must match principal source")
        return describe_surface(authorized.principal)

    async def read_system(self, context: object) -> SystemSnapshot:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_system"))
        return await self._port.read_system()

    async def read_yuki(self, context: object) -> YukiSummaryView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_yuki"))
        return await self._port.read_yuki()

    async def read_health(self, context: object) -> ManagementHealthView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_health"))
        return await self._port.read_health()

    async def list_persons(self, context: object, request: PageRequest) -> Page[PersonView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_persons"))
        return await self._port.list_persons(request)

    async def list_identity_bindings(
        self, context: object, request: PageRequest
    ) -> Page[IdentityBindingView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_identity_bindings"))
        return await self._port.list_identity_bindings(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_spaces(self, context: object, request: PageRequest) -> Page[SpaceView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_spaces"))
        return await self._port.list_spaces(request, reveal_external=_reveal_external(authorized))

    async def list_space_bindings(
        self, context: object, request: PageRequest
    ) -> Page[SpaceBindingView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_space_bindings"))
        return await self._port.list_space_bindings(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_presences(self, context: object, request: PageRequest) -> Page[PresenceView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_presences"))
        return await self._port.list_presences(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_conversations(
        self, context: object, request: PageRequest
    ) -> Page[ConversationView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_conversations"))
        return await self._port.list_conversations(request)

    async def list_person_active_routes(
        self, context: object, request: PageRequest
    ) -> Page[PersonActiveRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_person_active_routes"))
        return await self._port.list_person_active_routes(request)

    async def list_space_binding_ingest_routes(
        self, context: object, request: PageRequest
    ) -> Page[SpaceBindingIngestRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_space_binding_ingest_routes"))
        return await self._port.list_space_binding_ingest_routes(request)

    async def list_space_active_routes(
        self, context: object, request: PageRequest
    ) -> Page[SpaceActiveRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_space_active_routes"))
        return await self._port.list_space_active_routes(request)

    async def list_audit_events(
        self, context: object, request: PageRequest
    ) -> Page[AuditEventView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_audit_events"))
        return await self._port.list_audit_events(request)

    async def list_config_specs(
        self, context: object, request: PageRequest
    ) -> Page[ConfigSpecView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_config_specs"))
        return await self._port.list_config_specs(request)

    async def list_operations(
        self, context: object, request: PageRequest, *, kind: OperationKind = OperationKind.CONTROL
    ) -> Page[OperationRef]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_operations"))
        return await self._port.list_operations(request, kind=kind)

    async def read_operation(self, context: object, operation_id: str) -> OperationRef:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_operation"))
        return await self._port.read_operation(operation_id)

    async def list_effective_configs(
        self, context: object, request: PageRequest, *, scope: ConfigQueryScope | None = None
    ) -> Page[EffectiveConfigView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_effective_configs"))
        return await self._port.list_effective_configs(request, scope=scope)

    async def list_config_overrides(
        self, context: object, request: PageRequest
    ) -> Page[ConfigOverrideView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_config_overrides"))
        return await self._port.list_config_overrides(
            request, reveal_external=_reveal_external(authorized)
        )

    async def read_self_reflection_health(self, context: object) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_self_reflection_health"))
        return await self._port.read_self_reflection_health()

    async def list_self_reflection_history(
        self,
        context: object,
        request: PageRequest,
        *,
        section: str,
        scope: ReflectionQueryFilter | None = None,
    ) -> Page[ActivityView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_self_reflection_history"))
        return await self._port.list_self_reflection_history(request, section=section, scope=scope)

    async def list_memory_facts(
        self, context: object, request: PageRequest, *, scope: MemoryQueryFilter | None = None
    ) -> Page[MemoryFactView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_facts"))
        return await self._port.list_memory_facts(
            request,
            include_content=authorized.principal.allows("control.memory.content.read"),
            scope=scope,
        )

    async def list_memory_evidence(
        self, context: object, request: PageRequest, *, scope: MemoryQueryFilter | None = None
    ) -> Page[MemoryEvidenceView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_evidence"))
        return await self._port.list_memory_evidence(
            request,
            include_content=authorized.principal.allows("control.memory.content.read"),
            scope=scope,
        )

    async def read_memory_fact(self, context: object, fact_id: int) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_memory_fact"))
        return await self._port.read_memory_fact(
            fact_id, include_content=authorized.principal.allows("control.memory.content.read")
        )

    async def list_memory_jobs(self, context: object, request: PageRequest) -> Page[MemoryJobView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_jobs"))
        return await self._port.list_memory_jobs(request)

    async def read_memory_health(self, context: object) -> MemoryHealthView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_memory_health"))
        return await self._port.read_memory_health()

    async def list_automations(self, context: object, request: PageRequest) -> Page[AutomationView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_automations"))
        return await self._port.list_automations(request)

    async def list_plugins(self, context: object, request: PageRequest) -> Page[PluginView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_plugins"))
        return await self._port.list_plugins(request)

    async def read_plugin_approval(self, context: object, plugin_id: str) -> ActivityView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_plugin_approval"))
        return await self._port.read_plugin_approval(plugin_id)

    async def read_plugin_runtime(self, context: object, plugin_id: str) -> PluginRuntimeView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_plugin_runtime"))
        return await self._port.read_plugin_runtime(plugin_id)

    async def list_emoji_assets(
        self, context: object, request: PageRequest
    ) -> Page[EmojiAssetView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_emoji_assets"))
        return await self._port.list_emoji_assets(
            request,
            reveal_first_seen_person=authorized.principal.allows("identity.person.read"),
            reveal_first_seen_space=authorized.principal.allows("identity.space.read"),
        )
