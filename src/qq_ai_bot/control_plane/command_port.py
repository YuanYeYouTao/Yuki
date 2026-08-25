"""Control command port. Implementations live outside this package."""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.control_plane.commands import ControlCommand, ControlResult
from qq_ai_bot.control_plane.principal import ControlPrincipal


class ControlCommandPort(Protocol):
    """Authorized mutations. Must not invent principals or capability strings."""

    async def enable_person(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def disable_person(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def attach_identity_binding(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def enable_space(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def disable_space(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def attach_space_binding(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def register_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def start_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def stop_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def set_presence_ingest(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def set_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def pause_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...

    async def resume_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult: ...
