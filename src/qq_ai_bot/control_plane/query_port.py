"""Control query port. Implementations live outside this package."""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.query_types import (
    AuditEventView,
    BackfillConflictView,
    BackfillOperationView,
    ConversationView,
    IdentityBindingView,
    ManagementHealthView,
    PersonActiveRouteView,
    PersonView,
    PresenceView,
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SystemSnapshot,
    YukiSummaryView,
)


class ControlQueryPort(Protocol):
    """Read-only projections. Must not return catalog rows or session objects."""

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

    async def list_backfill_operations(
        self, request: PageRequest
    ) -> Page[BackfillOperationView]: ...

    async def list_backfill_conflicts(self, request: PageRequest) -> Page[BackfillConflictView]: ...
