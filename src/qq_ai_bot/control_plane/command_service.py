"""Application command services. Default-deny over DecisionContext."""

from __future__ import annotations

from qq_ai_bot.control_plane.command_port import ControlCommandPort
from qq_ai_bot.control_plane.command_types import ControlCommandError
from qq_ai_bot.control_plane.commands import ControlCommand, ControlResult
from qq_ai_bot.control_plane.decision import decide
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
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
    raise ControlCommandError(problem)


def _require_command(
    context: DecisionContext[ControlPrincipal, object, object],
    command: object,
) -> ControlCommand:
    if type(command) is not ControlCommand:
        raise TypeError("command must be ControlCommand")
    if command.request_id != context.request_id:
        raise ControlCommandError(Problem(ProblemCode.VALIDATION_ERROR))
    return command


class ControlCommandService:
    """Authorize then mutate. Does not invent principals, actors, or capabilities."""

    def __init__(self, port: ControlCommandPort) -> None:
        if port is None:
            raise TypeError("port is required")
        self._port = port

    async def enable_person(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.person.enable")
        return await self._port.enable_person(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def disable_person(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.person.disable")
        return await self._port.disable_person(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def attach_identity_binding(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.binding.attach")
        return await self._port.attach_identity_binding(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def enable_space(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.space.enable")
        return await self._port.enable_space(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def disable_space(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.space.disable")
        return await self._port.disable_space(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def attach_space_binding(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.space.binding.attach")
        return await self._port.attach_space_binding(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def register_presence(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.presence.register")
        return await self._port.register_presence(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def start_presence(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.presence.start")
        return await self._port.start_presence(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def stop_presence(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.presence.stop")
        return await self._port.stop_presence(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def set_presence_ingest(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "identity.presence.set_ingest")
        return await self._port.set_presence_ingest(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def set_route(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "route.set")
        return await self._port.set_route(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def pause_route(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "route.pause")
        return await self._port.pause_route(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )

    async def resume_route(self, context: object, command: object) -> ControlResult:
        authorized = _require_context(context)
        _require_capability(authorized, "route.resume")
        return await self._port.resume_route(
            authorized.principal, authorized.canonical_target, _require_command(authorized, command)
        )
