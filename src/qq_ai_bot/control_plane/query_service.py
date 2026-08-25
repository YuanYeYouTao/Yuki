"""Application query services. Default-deny over DecisionContext."""

from __future__ import annotations

from qq_ai_bot.control_plane.decision import decide
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_port import ControlQueryPort
from qq_ai_bot.control_plane.query_types import (
    AuditEventView,
    AutomationView,
    BackfillConflictView,
    BackfillOperationView,
    ConfigOverrideView,
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

    async def read_system(self, context: object) -> SystemSnapshot:
        authorized = _require_context(context)
        _require_capability(authorized, "control.system.read")
        return await self._port.read_system()

    async def read_yuki(self, context: object) -> YukiSummaryView:
        authorized = _require_context(context)
        _require_capability(authorized, "control.system.read")
        return await self._port.read_yuki()

    async def read_health(self, context: object) -> ManagementHealthView:
        authorized = _require_context(context)
        _require_capability(authorized, "control.health.read")
        return await self._port.read_health()

    async def list_persons(self, context: object, request: PageRequest) -> Page[PersonView]:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.person.read")
        return await self._port.list_persons(request)

    async def list_identity_bindings(
        self, context: object, request: PageRequest
    ) -> Page[IdentityBindingView]:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.binding.read")
        return await self._port.list_identity_bindings(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_spaces(self, context: object, request: PageRequest) -> Page[SpaceView]:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.space.read")
        return await self._port.list_spaces(request, reveal_external=_reveal_external(authorized))

    async def list_space_bindings(
        self, context: object, request: PageRequest
    ) -> Page[SpaceBindingView]:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.space.read")
        return await self._port.list_space_bindings(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_presences(self, context: object, request: PageRequest) -> Page[PresenceView]:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.presence.read")
        return await self._port.list_presences(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_conversations(
        self, context: object, request: PageRequest
    ) -> Page[ConversationView]:
        authorized = _require_context(context)
        _require_capability(authorized, "conversation.metadata.read")
        return await self._port.list_conversations(request)

    async def list_person_active_routes(
        self, context: object, request: PageRequest
    ) -> Page[PersonActiveRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, "route.read")
        return await self._port.list_person_active_routes(request)

    async def list_space_binding_ingest_routes(
        self, context: object, request: PageRequest
    ) -> Page[SpaceBindingIngestRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, "route.read")
        return await self._port.list_space_binding_ingest_routes(request)

    async def list_space_active_routes(
        self, context: object, request: PageRequest
    ) -> Page[SpaceActiveRouteView]:
        authorized = _require_context(context)
        _require_capability(authorized, "route.read")
        return await self._port.list_space_active_routes(request)

    async def list_audit_events(
        self, context: object, request: PageRequest
    ) -> Page[AuditEventView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.audit.read")
        return await self._port.list_audit_events(request)

    async def list_backfill_operations(
        self, context: object, request: PageRequest
    ) -> Page[BackfillOperationView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.operation.read")
        return await self._port.list_backfill_operations(request)

    async def list_backfill_conflicts(
        self, context: object, request: PageRequest
    ) -> Page[BackfillConflictView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.operation.read")
        return await self._port.list_backfill_conflicts(request)

    async def list_config_specs(
        self, context: object, request: PageRequest
    ) -> Page[ConfigSpecView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.config.read")
        return await self._port.list_config_specs(request)

    async def list_effective_configs(
        self, context: object, request: PageRequest
    ) -> Page[EffectiveConfigView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.config.read")
        return await self._port.list_effective_configs(request)

    async def list_config_overrides(
        self, context: object, request: PageRequest
    ) -> Page[ConfigOverrideView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.config.read")
        return await self._port.list_config_overrides(
            request, reveal_external=_reveal_external(authorized)
        )

    async def list_memory_facts(
        self, context: object, request: PageRequest
    ) -> Page[MemoryFactView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.memory.metadata.read")
        return await self._port.list_memory_facts(
            request, include_content=authorized.principal.allows("control.memory.content.read")
        )

    async def list_memory_evidence(
        self, context: object, request: PageRequest
    ) -> Page[MemoryEvidenceView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.memory.metadata.read")
        return await self._port.list_memory_evidence(
            request, include_content=authorized.principal.allows("control.memory.content.read")
        )

    async def list_memory_jobs(self, context: object, request: PageRequest) -> Page[MemoryJobView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.memory.metadata.read")
        return await self._port.list_memory_jobs(request)

    async def read_memory_health(self, context: object) -> MemoryHealthView:
        authorized = _require_context(context)
        _require_capability(authorized, "control.memory.metadata.read")
        return await self._port.read_memory_health()

    async def list_automations(self, context: object, request: PageRequest) -> Page[AutomationView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.automation.read")
        return await self._port.list_automations(request)

    async def list_plugins(self, context: object, request: PageRequest) -> Page[PluginView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.plugin.read")
        return await self._port.list_plugins(request)

    async def list_mcp_servers(self, context: object, request: PageRequest) -> Page[McpServerView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.mcp.read")
        return await self._port.list_mcp_servers(request)

    async def list_emoji_assets(
        self, context: object, request: PageRequest
    ) -> Page[EmojiAssetView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.emoji.read")
        return await self._port.list_emoji_assets(
            request,
            reveal_first_seen_person=authorized.principal.allows("identity.person.read"),
            reveal_first_seen_space=authorized.principal.allows("identity.space.read"),
        )

    async def list_speech_profiles(
        self, context: object, request: PageRequest
    ) -> Page[SpeechProfileView]:
        authorized = _require_context(context)
        _require_capability(authorized, "control.speech.read")
        return await self._port.list_speech_profiles(request)
