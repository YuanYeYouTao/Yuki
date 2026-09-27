"""Application query services. Default-deny over DecisionContext."""

from __future__ import annotations

from qq_ai_bot.control_plane.decision import decide
from qq_ai_bot.control_plane.operations import OperationKind, OperationRef
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_port import ControlQueryPort
from qq_ai_bot.control_plane.query_types import (
    AuditEventView,
    AutomationView,
    ConfigOverrideView,
    ConfigQueryScope,
    ConfigSpecView,
    ControlQueryError,
    ConversationView,
    EffectiveConfigView,
    EmojiAssetView,
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
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SpeechProfileView,
    SystemSnapshot,
    YukiSummaryView,
)
from qq_ai_bot.control_plane.surface import ControlSurfaceView, describe_surface, method_capability
from qq_ai_bot.domain.control import DecisionContext


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

    def __init__(self, port: ControlQueryPort) -> None:
        if port is None:
            raise TypeError("port is required")
        self._port = port

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

    async def list_memory_facts(
        self, context: object, request: PageRequest
    ) -> Page[MemoryFactView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_facts"))
        return await self._port.list_memory_facts(
            request, include_content=authorized.principal.allows("control.memory.content.read")
        )

    async def list_memory_evidence(
        self, context: object, request: PageRequest
    ) -> Page[MemoryEvidenceView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_memory_evidence"))
        return await self._port.list_memory_evidence(
            request, include_content=authorized.principal.allows("control.memory.content.read")
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

    async def read_plugin_runtime(self, context: object, plugin_id: str) -> PluginRuntimeView:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("read_plugin_runtime"))
        return await self._port.read_plugin_runtime(plugin_id)

    async def list_mcp_servers(self, context: object, request: PageRequest) -> Page[McpServerView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_mcp_servers"))
        return await self._port.list_mcp_servers(request)

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

    async def list_speech_profiles(
        self, context: object, request: PageRequest
    ) -> Page[SpeechProfileView]:
        authorized = _require_context(context)
        _require_capability(authorized, method_capability("list_speech_profiles"))
        return await self._port.list_speech_profiles(request)
