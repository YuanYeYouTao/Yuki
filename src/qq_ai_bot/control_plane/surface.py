"""Reviewed application methods; discovery is not dispatch or permission granting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from qq_ai_bot.control_plane.capabilities import control_capability_descriptor
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError


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


# Each entry points at an actual public Query/Command method. Business catalogs
# and CONTROL_CAPABILITY_DESCRIPTORS remain authoritative; no plugin call dispatch.
_METHODS: tuple[tuple[Literal["query", "command"], str, str], ...] = (
    ("query", "download_workspace", "control.workspace.content.read"),
    ("query", "download_chat_media", "control.chat.content.read"),
    ("query", "read_model_catalog", "control.config.read"),
    ("query", "read_persona", "control.execution.content.read"),
    ("query", "list_participation_runs", "control.execution.metadata.read"),
    ("query", "read_participation", "control.execution.metadata.read"),
    ("query", "list_work", "control.execution.metadata.read"),
    ("query", "read_automation", "control.automation.content.read"),
    ("query", "list_model_usage", "control.execution.metadata.read"),
    ("query", "list_workspace", "control.workspace.metadata.read"),
    ("query", "read_workspace", "control.workspace.content.read"),
    ("query", "list_execution_trace", "control.execution.metadata.read"),
    ("query", "read_execution_trace", "control.execution.content.read"),
    ("query", "list_chat_events", "control.chat.metadata.read"),
    ("query", "list_social_receipts", "control.execution.metadata.read"),
    ("query", "read_system", "control.system.read"),
    ("query", "read_yuki", "control.system.read"),
    ("query", "read_health", "control.health.read"),
    ("query", "list_persons", "identity.person.read"),
    ("query", "list_identity_bindings", "identity.binding.read"),
    ("query", "list_spaces", "identity.space.read"),
    ("query", "list_space_bindings", "identity.space.read"),
    ("query", "list_presences", "identity.presence.read"),
    ("query", "list_conversations", "conversation.metadata.read"),
    ("query", "list_person_active_routes", "route.read"),
    ("query", "list_space_binding_ingest_routes", "route.read"),
    ("query", "list_space_active_routes", "route.read"),
    ("query", "list_audit_events", "control.audit.read"),
    ("query", "list_config_specs", "control.config.read"),
    ("query", "list_operations", "control.operation.read"),
    ("query", "read_operation", "control.operation.read"),
    ("query", "list_effective_configs", "control.config.read"),
    ("query", "list_config_overrides", "control.config.read"),
    ("query", "list_memory_facts", "control.memory.metadata.read"),
    ("query", "list_memory_evidence", "control.memory.metadata.read"),
    ("query", "list_memory_jobs", "control.memory.metadata.read"),
    ("query", "read_memory_health", "control.memory.metadata.read"),
    ("query", "list_automations", "control.automation.read"),
    ("query", "list_plugins", "control.plugin.read"),
    ("query", "read_plugin_runtime", "control.plugin.read"),
    ("query", "list_mcp_servers", "control.mcp.read"),
    ("query", "list_emoji_assets", "control.emoji.read"),
    ("query", "list_speech_profiles", "control.speech.read"),
    ("command", "enable_person", "identity.person.enable"),
    ("command", "disable_person", "identity.person.disable"),
    ("command", "attach_identity_binding", "identity.binding.attach"),
    ("command", "enable_space", "identity.space.enable"),
    ("command", "disable_space", "identity.space.disable"),
    ("command", "attach_space_binding", "identity.space.binding.attach"),
    ("command", "register_presence", "identity.presence.register"),
    ("command", "start_presence", "identity.presence.start"),
    ("command", "stop_presence", "identity.presence.stop"),
    ("command", "set_presence_ingest", "identity.presence.set_ingest"),
    ("command", "set_route", "route.set"),
    ("command", "pause_route", "route.pause"),
    ("command", "resume_route", "route.resume"),
    ("command", "set_config", "control.config.mutate"),
    ("command", "unset_config", "control.config.mutate"),
    ("command", "rollback_config", "control.config.mutate"),
    ("command", "mutate_memory", "control.memory.mutate"),
    ("command", "rebuild_memory", "control.memory.rebuild"),
    ("command", "dream_memory", "control.memory.dream"),
    ("command", "maintain_memory", "control.memory.maintenance"),
    ("command", "mutate_automation", "control.automation.mutate"),
    ("command", "mutate_plugin", "control.plugin.mutate"),
    ("command", "mutate_mcp", "control.mcp.mutate"),
    ("command", "mutate_emoji", "control.emoji.mutate"),
    ("command", "mutate_speech", "control.speech.mutate"),
    ("command", "cancel_operation", "control.operation.cancel"),
    ("command", "retry_operation", "control.operation.retry"),
)
if len({name for _, name, _ in _METHODS}) != len(_METHODS):
    raise ValueError("duplicate control method")
if any(control_capability_descriptor(capability) is None for _, _, capability in _METHODS):
    raise ValueError("unreviewed control capability")


def method_capability(name: str) -> str:
    return next(capability for _, method, capability in _METHODS if method == name)


def describe_surface(principal: ControlPrincipal) -> ControlSurfaceView:
    if not principal.authenticated:
        raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
    if not principal.active:
        raise ControlQueryError(Problem(ProblemCode.PRECONDITION_FAILED))
    methods = []
    for kind, name, capability in _METHODS:
        descriptor = control_capability_descriptor(capability)
        if descriptor is None:
            raise RuntimeError("control capability descriptor missing")
        methods.append(
            ControlMethodView(
                name, kind, capability, descriptor.sensitivity.value, principal.allows(capability)
            )
        )
    return ControlSurfaceView("control.v1", tuple(methods))
