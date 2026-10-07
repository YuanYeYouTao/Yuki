"""Reviewed application methods; discovery is not dispatch or permission granting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from qq_ai_bot.control_plane.capabilities import control_capability_descriptor
from qq_ai_bot.control_plane.json_types import freeze_json_object
from qq_ai_bot.control_plane.operations import OperationKind
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ChatHistoryFilter,
    ConfigQueryScope,
    ControlQueryError,
    ExecutionTraceFilter,
    MemoryQueryFilter,
    ReflectionQueryFilter,
)
from qq_ai_bot.domain.identity import ConversationId, PersonId, RequestId, SpaceId


@dataclass(frozen=True, slots=True)
class ControlMethodView:
    name: str
    kind: Literal["query", "command"]
    capability: str
    sensitivity: str
    authorized: bool


@dataclass(frozen=True, slots=True)
class ControlSurfaceView:
    protocol_version: str
    methods: tuple[ControlMethodView, ...]


@dataclass(frozen=True, slots=True)
class ControlMethod:
    kind: Literal["query", "command"]
    name: str
    capability: str
    bind: Callable[[Any], Any]
    simple: bool = False


_METHODS = (
    ControlMethod(
        "query",
        "download_workspace",
        "control.workspace.content.read",
        lambda service: service.download_workspace,
        simple=False,
    ),
    ControlMethod(
        "query",
        "download_chat_media",
        "control.chat.content.read",
        lambda service: service.download_chat_media,
        simple=False,
    ),
    ControlMethod(
        "query",
        "download_emoji",
        "control.emoji.read",
        lambda service: service.download_emoji,
        simple=False,
    ),
    ControlMethod(
        "query",
        "download_avatar",
        "control.system.read",
        lambda service: service.download_avatar,
        simple=True,
    ),
    ControlMethod(
        "query",
        "download_environment_file",
        "control.workspace.content.read",
        lambda service: service.download_environment_file,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_config_file",
        "control.config.file.content.read",
        lambda service: service.read_config_file,
        simple=False,
    ),
    ControlMethod(
        "command",
        "save_config_file",
        "control.config.file.mutate",
        lambda service: service.save_config_file,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_model_catalog",
        "control.config.read",
        lambda service: service.read_model_catalog,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_persona",
        "control.execution.content.read",
        lambda service: service.read_persona,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_participation_runs",
        "control.execution.metadata.read",
        lambda service: service.list_participation_runs,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_participation_feedback",
        "control.execution.metadata.read",
        lambda service: service.list_participation_feedback,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_participation",
        "control.execution.metadata.read",
        lambda service: service.read_participation,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_work",
        "control.execution.metadata.read",
        lambda service: service.read_work,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_work_history",
        "control.execution.metadata.read",
        lambda service: service.list_work_history,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_environment_file",
        "control.environment.file.mutate",
        lambda service: service.mutate_environment_file,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_environment_terminal",
        "control.terminal.mutate",
        lambda service: service.mutate_environment_terminal,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_work",
        "control.work.mutate",
        lambda service: service.mutate_work,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_work",
        "control.execution.metadata.read",
        lambda service: service.list_work,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_automation",
        "control.automation.content.read",
        lambda service: service.read_automation,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_automation_schema",
        "control.automation.read",
        lambda service: service.read_automation_schema,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_memory_maintenance_schema",
        "control.memory.metadata.read",
        lambda service: service.read_memory_maintenance_schema,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_memory_rebuild_proposals",
        "control.memory.metadata.read",
        lambda service: service.list_memory_rebuild_proposals,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_memory_maintenance_run",
        "control.memory.metadata.read",
        lambda service: service.read_memory_maintenance_run,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_automation_runs",
        "control.automation.read",
        lambda service: service.list_automation_runs,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_automation_steps",
        "control.automation.read",
        lambda service: service.list_automation_steps,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_model_usage",
        "control.execution.metadata.read",
        lambda service: service.list_model_usage,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_model_usage_summary",
        "control.execution.metadata.read",
        lambda service: service.read_model_usage_summary,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_terminal_submission",
        "control.terminal.content.read",
        lambda service: service.read_terminal_submission,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_environment",
        "control.workspace.metadata.read",
        lambda service: service.read_environment,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_workspace",
        "control.workspace.metadata.read",
        lambda service: service.list_workspace,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_workspace",
        "control.workspace.content.read",
        lambda service: service.read_workspace,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_execution_trace",
        "control.execution.metadata.read",
        lambda service: service.list_execution_trace,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_conversation_execution",
        "control.execution.metadata.read",
        lambda service: service.read_conversation_execution,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_event_turns",
        "control.execution.metadata.read",
        lambda service: service.list_event_turns,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_execution_trace",
        "control.execution.content.read",
        lambda service: service.read_execution_trace,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_chat_events",
        "control.chat.metadata.read",
        lambda service: service.list_chat_events,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_social_receipts",
        "control.execution.metadata.read",
        lambda service: service.list_social_receipts,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_system",
        "control.system.read",
        lambda service: service.read_system,
        simple=True,
    ),
    ControlMethod(
        "query", "read_yuki", "control.system.read", lambda service: service.read_yuki, simple=True
    ),
    ControlMethod(
        "query",
        "read_display_names",
        "control.system.read",
        lambda service: service.read_display_names,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_health",
        "control.health.read",
        lambda service: service.read_health,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_persons",
        "identity.person.read",
        lambda service: service.list_persons,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_identity_bindings",
        "identity.binding.read",
        lambda service: service.list_identity_bindings,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_spaces",
        "identity.space.read",
        lambda service: service.list_spaces,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_space_bindings",
        "identity.space.read",
        lambda service: service.list_space_bindings,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_presences",
        "identity.presence.read",
        lambda service: service.list_presences,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_conversations",
        "conversation.metadata.read",
        lambda service: service.list_conversations,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_person_active_routes",
        "route.read",
        lambda service: service.list_person_active_routes,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_space_binding_ingest_routes",
        "route.read",
        lambda service: service.list_space_binding_ingest_routes,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_space_active_routes",
        "route.read",
        lambda service: service.list_space_active_routes,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_audit_events",
        "control.audit.read",
        lambda service: service.list_audit_events,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_config_specs",
        "control.config.read",
        lambda service: service.list_config_specs,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_operations",
        "control.operation.read",
        lambda service: service.list_operations,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_operation",
        "control.operation.read",
        lambda service: service.read_operation,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_effective_configs",
        "control.config.read",
        lambda service: service.list_effective_configs,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_config_overrides",
        "control.config.read",
        lambda service: service.list_config_overrides,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_self_reflection_health",
        "control.memory.metadata.read",
        lambda service: service.read_self_reflection_health,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_self_reflection_history",
        "control.memory.metadata.read",
        lambda service: service.list_self_reflection_history,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_relationships",
        "control.relationship.read",
        lambda service: service.list_relationships,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_relationship",
        "control.relationship.read",
        lambda service: service.read_relationship,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_relationship_history",
        "control.relationship.read",
        lambda service: service.list_relationship_history,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_memory_facts",
        "control.memory.metadata.read",
        lambda service: service.list_memory_facts,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_memory_fact",
        "control.memory.metadata.read",
        lambda service: service.read_memory_fact,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_memory_evidence",
        "control.memory.metadata.read",
        lambda service: service.list_memory_evidence,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_memory_jobs",
        "control.memory.metadata.read",
        lambda service: service.list_memory_jobs,
        simple=True,
    ),
    ControlMethod(
        "query",
        "read_memory_health",
        "control.memory.metadata.read",
        lambda service: service.read_memory_health,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_automations",
        "control.automation.read",
        lambda service: service.list_automations,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_plugins",
        "control.plugin.read",
        lambda service: service.list_plugins,
        simple=True,
    ),
    ControlMethod(
        "query",
        "list_plugin_outbox",
        "control.plugin.read",
        lambda service: service.list_plugin_outbox,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_plugin_background_turns",
        "control.plugin.read",
        lambda service: service.list_plugin_background_turns,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_plugin_approval",
        "control.plugin.read",
        lambda service: service.read_plugin_approval,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_plugin_runtime",
        "control.plugin.read",
        lambda service: service.read_plugin_runtime,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_plugin_configuration",
        "control.plugin.config.content.read",
        lambda service: service.read_plugin_configuration,
        simple=False,
    ),
    ControlMethod(
        "query",
        "read_plugin_observation",
        "control.plugin.config.content.read",
        lambda service: service.read_plugin_observation,
        simple=False,
    ),
    ControlMethod(
        "command",
        "configure_plugin",
        "control.plugin.config.mutate",
        lambda service: service.configure_plugin,
        simple=False,
    ),
    ControlMethod(
        "query",
        "list_emoji_assets",
        "control.emoji.read",
        lambda service: service.list_emoji_assets,
        simple=True,
    ),
    ControlMethod(
        "command",
        "enable_person",
        "identity.person.enable",
        lambda service: service.enable_person,
        simple=False,
    ),
    ControlMethod(
        "command",
        "disable_person",
        "identity.person.disable",
        lambda service: service.disable_person,
        simple=False,
    ),
    ControlMethod(
        "command",
        "attach_identity_binding",
        "identity.binding.attach",
        lambda service: service.attach_identity_binding,
        simple=False,
    ),
    ControlMethod(
        "command",
        "enable_space",
        "identity.space.enable",
        lambda service: service.enable_space,
        simple=False,
    ),
    ControlMethod(
        "command",
        "disable_space",
        "identity.space.disable",
        lambda service: service.disable_space,
        simple=False,
    ),
    ControlMethod(
        "command",
        "attach_space_binding",
        "identity.space.binding.attach",
        lambda service: service.attach_space_binding,
        simple=False,
    ),
    ControlMethod(
        "command",
        "register_presence",
        "identity.presence.register",
        lambda service: service.register_presence,
        simple=False,
    ),
    ControlMethod(
        "command",
        "start_presence",
        "identity.presence.start",
        lambda service: service.start_presence,
        simple=False,
    ),
    ControlMethod(
        "command",
        "stop_presence",
        "identity.presence.stop",
        lambda service: service.stop_presence,
        simple=False,
    ),
    ControlMethod(
        "command",
        "set_presence_ingest",
        "identity.presence.set_ingest",
        lambda service: service.set_presence_ingest,
        simple=False,
    ),
    ControlMethod(
        "command", "set_route", "route.set", lambda service: service.set_route, simple=False
    ),
    ControlMethod(
        "command", "pause_route", "route.pause", lambda service: service.pause_route, simple=False
    ),
    ControlMethod(
        "command",
        "resume_route",
        "route.resume",
        lambda service: service.resume_route,
        simple=False,
    ),
    ControlMethod(
        "command",
        "set_config",
        "control.config.mutate",
        lambda service: service.set_config,
        simple=False,
    ),
    ControlMethod(
        "command",
        "unset_config",
        "control.config.mutate",
        lambda service: service.unset_config,
        simple=False,
    ),
    ControlMethod(
        "command",
        "rollback_config",
        "control.config.mutate",
        lambda service: service.rollback_config,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_relationship",
        "control.relationship.mutate",
        lambda service: service.mutate_relationship,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_memory",
        "control.memory.mutate",
        lambda service: service.mutate_memory,
        simple=False,
    ),
    ControlMethod(
        "command",
        "rebuild_memory",
        "control.memory.rebuild",
        lambda service: service.rebuild_memory,
        simple=False,
    ),
    ControlMethod(
        "command",
        "dream_memory",
        "control.memory.dream",
        lambda service: service.dream_memory,
        simple=False,
    ),
    ControlMethod(
        "command",
        "maintain_memory",
        "control.memory.maintenance",
        lambda service: service.maintain_memory,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_automation",
        "control.automation.mutate",
        lambda service: service.mutate_automation,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_plugin",
        "control.plugin.mutate",
        lambda service: service.mutate_plugin,
        simple=False,
    ),
    ControlMethod(
        "command",
        "mutate_emoji",
        "control.emoji.mutate",
        lambda service: service.mutate_emoji,
        simple=False,
    ),
    ControlMethod(
        "command",
        "cancel_operation",
        "control.operation.cancel",
        lambda service: service.cancel_operation,
        simple=False,
    ),
    ControlMethod(
        "command",
        "retry_operation",
        "control.operation.retry",
        lambda service: service.retry_operation,
        simple=False,
    ),
)
_BY_NAME = {method.name: method for method in _METHODS}
if len(_BY_NAME) != len(_METHODS):
    raise ValueError("duplicate control method")
if any(control_capability_descriptor(method.capability) is None for method in _METHODS):
    raise ValueError("unreviewed control capability")


def method_capability(name: str) -> str:
    return _BY_NAME[name].capability


def describe_surface(principal: ControlPrincipal) -> ControlSurfaceView:
    if not principal.authenticated:
        raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
    if not principal.active:
        raise ControlQueryError(Problem(ProblemCode.PRECONDITION_FAILED))
    methods = []
    for method in _METHODS:
        kind, name, capability = method.kind, method.name, method.capability
        descriptor = control_capability_descriptor(capability)
        if descriptor is None:
            raise RuntimeError("control capability descriptor missing")
        methods.append(
            ControlMethodView(
                name, kind, capability, descriptor.sensitivity.value, principal.allows(capability)
            )
        )
    return ControlSurfaceView("control.v1", tuple(methods))


def _history(raw: Any) -> ChatHistoryFilter:
    if type(raw) is not dict or set(raw) - {
        "descending",
        "event_id",
        "through_event_id",
        "since",
        "until",
    }:
        raise ValueError("invalid chat history filter")
    return ChatHistoryFilter(
        descending=raw.get("descending", False),
        event_id=raw.get("event_id"),
        through_event_id=raw.get("through_event_id"),
        since=datetime.fromisoformat(raw["since"]) if raw.get("since") else None,
        until=datetime.fromisoformat(raw["until"]) if raw.get("until") else None,
    )


async def execute_query(queries: Any, ctx: Any, method: str, data: dict[str, Any]) -> object:
    from qq_ai_bot.control_plane.wire import decode_page

    descriptor = _BY_NAME.get(method)
    if descriptor is None or descriptor.kind != "query":
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    page = decode_page(data.get("page", {}))
    result: object
    if method == "read_display_names":
        if set(data) != {"references"}:
            raise ValueError("read_display_names requires references only")
        result = await queries.read_display_names(
            ctx, freeze_json_object(data.get("references", {}))
        )
    elif descriptor.simple:
        if set(data) - {"page"}:
            raise ValueError("unknown query fields")
        result = (
            await descriptor.bind(queries)(ctx, page)
            if method.startswith("list_")
            else await descriptor.bind(queries)(ctx)
        )
    elif method in {"list_chat_events", "list_social_receipts"}:
        allowed = {"page", "conversation_id"}
        if method == "list_chat_events":
            allowed |= {"include_content", "history"}
        if set(data) - allowed:
            raise ValueError("unknown query fields")
        conversation = ConversationId.parse(data["conversation_id"])
        result = (
            await queries.list_chat_events(
                ctx,
                page,
                conversation_id=conversation,
                include_content=data.get("include_content", False),
                history=_history(data.get("history", {})),
            )
            if method == "list_chat_events"
            else await queries.list_social_receipts(ctx, page, conversation_id=conversation)
        )
    elif method == "read_conversation_execution":
        if (
            set(data) - {"conversation_id", "include_content", "turn_id", "before_step_id"}
            or "conversation_id" not in data
        ):
            raise ValueError("invalid conversation execution query")
        result = await queries.read_conversation_execution(
            ctx,
            ConversationId.parse(data["conversation_id"]),
            include_content=data.get("include_content", False),
            turn_id=data.get("turn_id"),
            before_step_id=data.get("before_step_id"),
        )
    elif method == "read_model_usage_summary":
        if (
            set(data) != {"window"}
            or type(data["window"]) is not str
            or data["window"] not in {"24h", "7d", "30d"}
        ):
            raise ValueError("invalid model usage window")
        result = await queries.read_model_usage_summary(ctx, data["window"])
    elif method == "list_event_turns":
        if set(data) - {"conversation_id", "event_id", "direction", "page"} or not {
            "conversation_id",
            "event_id",
            "direction",
        } <= set(data):
            raise ValueError("invalid event turn query")
        result = await queries.list_event_turns(
            ctx,
            page,
            conversation_id=ConversationId.parse(data["conversation_id"]),
            event_id=data["event_id"],
            direction=data["direction"],
        )
    elif method == "list_execution_trace":
        if set(data) - {"page", "scope", "include_content"}:
            raise ValueError("unknown query fields")
        scope = dict(data.get("scope", {}))
        if scope.get("conversation_id") is not None:
            scope["conversation_id"] = ConversationId.parse(scope["conversation_id"])
        result = await queries.list_execution_trace(
            ctx,
            page,
            scope=ExecutionTraceFilter(**scope),
            include_content=data.get("include_content", False),
        )
    elif method == "list_memory_rebuild_proposals":
        if set(data) - {"page", "run_id", "include_content"} or "run_id" not in data:
            raise ValueError("invalid rebuild history")
        result = await queries.list_memory_rebuild_proposals(
            ctx, page, run_id=data["run_id"], include_content=data.get("include_content", False)
        )
    elif method == "read_terminal_submission":
        if set(data) != {"request_id"}:
            raise ValueError("invalid terminal request")
        result = await queries.read_terminal_submission(ctx, RequestId.parse(data["request_id"]))
    elif method == "read_environment":
        if set(data) != {"section", "arguments"}:
            raise ValueError("invalid environment query")
        result = await queries.read_environment(
            ctx, data["section"], freeze_json_object(data["arguments"])
        )
    elif method == "read_execution_trace":
        if set(data) != {"entry_id"}:
            raise ValueError("invalid trace lookup")
        result = await queries.read_execution_trace(ctx, data["entry_id"])
    elif method == "list_self_reflection_history":
        if set(data) - {"page", "section", "scope"} or "section" not in data:
            raise ValueError("invalid reflection history")
        if type(data.get("scope", {})) is not dict:
            raise ValueError("scope must be an object")
        scope = dict(data.get("scope", {}))
        for key, cls in (("person_id", PersonId), ("space_id", SpaceId)):
            if scope.get(key) is not None:
                scope[key] = cls.parse(scope[key])
        result = await queries.list_self_reflection_history(
            ctx, page, section=data["section"], scope=ReflectionQueryFilter(**scope)
        )
    elif method == "read_relationship":
        if set(data) != {"person_id"}:
            raise ValueError("invalid relationship lookup")
        result = await queries.read_relationship(ctx, PersonId.parse(data["person_id"]))
    elif method == "list_relationship_history":
        if set(data) - {"page", "person_id", "section"} or not {"person_id", "section"} <= set(
            data
        ):
            raise ValueError("invalid relationship history")
        result = await queries.list_relationship_history(
            ctx, page, person_id=PersonId.parse(data["person_id"]), section=data["section"]
        )
    elif method in {"list_memory_facts", "list_memory_evidence"}:
        if set(data) - {"page", "scope"}:
            raise ValueError("invalid memory scope")
        if type(data.get("scope", {})) is not dict:
            raise ValueError("scope must be an object")
        scope = dict(data.get("scope", {}))
        for key, cls in (
            ("person_id", PersonId),
            ("space_id", SpaceId),
            ("visibility_person_id", PersonId),
            ("visibility_space_id", SpaceId),
        ):
            if scope.get(key) is not None:
                scope[key] = cls.parse(scope[key])
        result = await descriptor.bind(queries)(ctx, page, scope=MemoryQueryFilter(**scope))
    elif method == "read_memory_fact":
        if set(data) != {"fact_id"}:
            raise ValueError("invalid memory lookup")
        result = await queries.read_memory_fact(ctx, data["fact_id"])
    elif method == "list_effective_configs":
        raw = data.get("scope", {})
        if set(data) - {"page", "scope"} or set(raw) - {"person_id", "space_id"}:
            raise ValueError("invalid config scope")
        result = await queries.list_effective_configs(
            ctx,
            page,
            scope=ConfigQueryScope(
                person_id=PersonId.parse(raw["person_id"]) if raw.get("person_id") else None,
                space_id=SpaceId.parse(raw["space_id"]) if raw.get("space_id") else None,
            ),
        )
    elif method == "list_operations":
        if set(data) - {"page", "kind"}:
            raise ValueError("invalid operation scope")
        result = await queries.list_operations(
            ctx, page, kind=OperationKind(data.get("kind", "control"))
        )
    elif method == "list_participation_runs":
        if set(data) - {"page", "conversation_id"}:
            raise ValueError("invalid participation scope")
        result = await queries.list_participation_runs(
            ctx,
            page,
            conversation_id=ConversationId.parse(data["conversation_id"])
            if data.get("conversation_id")
            else None,
        )
    elif method == "read_work":
        if set(data) - {"work_id", "include_content"} or "work_id" not in data:
            raise ValueError("invalid work lookup")
        result = await queries.read_work(
            ctx, data["work_id"], include_content=data.get("include_content", False)
        )
    elif method == "list_work":
        if set(data) - {"page", "include_content"}:
            raise ValueError("invalid work query")
        result = await queries.list_work(
            ctx, page, include_content=data.get("include_content", False)
        )
    elif method == "list_work_history":
        if set(data) - {"page", "work_id", "section", "include_content"} or not {
            "work_id",
            "section",
        } <= set(data):
            raise ValueError("invalid work history")
        result = await queries.list_work_history(
            ctx,
            page,
            work_id=data["work_id"],
            section=data["section"],
            include_content=data.get("include_content", False),
        )
    elif method == "read_config_file":
        if set(data) != {"file_id"}:
            raise ValueError("invalid configuration file lookup")
        result = await queries.read_config_file(ctx, data["file_id"])
    elif method in {"read_automation", "read_workspace"}:
        key = "automation_id" if method == "read_automation" else "artifact_id"
        if set(data) != {key}:
            raise ValueError("invalid activity lookup")
        result = await descriptor.bind(queries)(ctx, data[key])
    elif method in {"list_automation_runs", "list_automation_steps"}:
        keys = {"page", "automation_id"} | (
            {"run_id"} if method == "list_automation_steps" else set()
        )
        if set(data) - keys or "automation_id" not in data:
            raise ValueError("invalid automation history")
        args = {"automation_id": data["automation_id"]}
        if method == "list_automation_steps":
            args["run_id"] = data.get("run_id")
        result = await descriptor.bind(queries)(ctx, page, **args)
    elif method == "list_participation_feedback":
        if set(data) - {"run_id", "page", "include_content"} or "run_id" not in data:
            raise ValueError("invalid participation feedback scope")
        result = await queries.list_participation_feedback(
            ctx, page, run_id=data["run_id"], include_content=data.get("include_content", False)
        )
    elif method in {"list_plugin_outbox", "list_plugin_background_turns"}:
        if set(data) - {"plugin_id", "page"} or "plugin_id" not in data:
            raise ValueError("invalid plugin outbox scope")
        result = await descriptor.bind(queries)(ctx, page, plugin_id=data["plugin_id"])
    elif method == "read_plugin_observation":
        if set(data) - {"plugin_id", "cursor", "limit"} or "plugin_id" not in data:
            raise ValueError("invalid plugin observation lookup")
        result = await queries.read_plugin_observation(
            ctx, data["plugin_id"], cursor=data.get("cursor"), limit=data.get("limit", 10)
        )
    elif method == "read_plugin_configuration":
        if set(data) - {"plugin_id", "scope_type", "owner_id"} or "plugin_id" not in data:
            raise ValueError("invalid plugin configuration lookup")
        result = await queries.read_plugin_configuration(
            ctx,
            data["plugin_id"],
            scope_type=data.get("scope_type", "global"),
            owner_id=data.get("owner_id"),
        )
    elif method == "read_memory_maintenance_run":
        if set(data) != {"operation_id"}:
            raise ValueError("invalid maintenance lookup")
        result = await queries.read_memory_maintenance_run(ctx, data["operation_id"])
    elif method in {"read_operation", "read_plugin_runtime", "read_plugin_approval"}:
        if method == "read_operation" and set(data) == {"request_id"}:
            original = RequestId.parse(data["request_id"])
            principal_id = ctx.principal.principal_id.text
            data = {"operation_id": f"control:{principal_id}:{original.text}"}
        key = "operation_id" if method == "read_operation" else "plugin_id"
        if set(data) != {key}:
            raise ValueError("invalid lookup")
        result = await descriptor.bind(queries)(ctx, data[key])
    else:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    return result


async def execute_command(commands: Any, ctx: Any, method: str, command: object) -> object:
    descriptor = _BY_NAME.get(method)
    if descriptor is None or descriptor.kind != "command":
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    return await descriptor.bind(commands)(ctx, command)
