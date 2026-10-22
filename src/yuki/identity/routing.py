"""Deterministic Person/Space route resolution through the memory Registry."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from yuki.conversation.event_entry import event_entry_matches
from yuki.gateway.models import ConnectionSnapshot
from yuki.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionRegistry,
    RegistryClosed,
    require_capability,
)
from yuki.gateway.social import PlatformSocialOperations
from yuki.identity.db_models import (
    AccountModel,
    CanonicalPersonModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import (
    current_person_enabled_condition,
    current_person_id,
    current_person_ids,
    person_family_condition,
    same_person_id,
)
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel

MembershipProbe = Callable[[object, str, str], Awaitable[bool]]


class RouteSendError(RuntimeError):
    """Paused, ambiguous, empty, or disconnected send route."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    presence_id: str
    binding_id: str
    platform: str
    external_target_id: str


@dataclass(frozen=True, slots=True)
class ResolvedSend:
    presence_id: str
    binding_id: str
    platform: str
    external_target_id: str
    route_generation: int
    connection: ConnectionResolution
    kind: str
    sender_account_id: str
    conversation_id: str | None = None
    thread_id: str | None = None
    source_event_id: int | None = None


@dataclass(frozen=True, slots=True)
class ObservedPersonRoute:
    route_generation: int
    revision: int
    identity_binding_id: str
    presence_id: str
    paused: bool


@dataclass(frozen=True, slots=True)
class ObservedSpaceRoute:
    route_generation: int
    revision: int
    space_binding_id: str
    presence_id: str
    paused: bool


@dataclass(frozen=True, slots=True)
class ObservedIngestRoute:
    route_generation: int
    revision: int
    ingest_presence_id: str
    paused: bool


class PresenceRouter:
    """Resolve send/ingest through enabled Presence + unique Registry active."""

    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        *,
        membership_probe: MembershipProbe | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._probe = membership_probe or self._native_membership_probe
        self._cas_hold: Callable[[], Awaitable[None]] | None = None

    def operations(self, route: ResolvedSend) -> PlatformSocialOperations:
        return self._registry.social(route.connection)

    async def _native_membership_probe(self, bot: object, group_id: str, user_id: str) -> bool:
        connection = self._registry.resolve_by_handle(bot)
        try:
            operations = self._registry.social(connection)
            return await operations.is_member(bot, group_id, user_id)
        except Exception:
            return False

    async def _await_cas_hold(self) -> None:
        hold = self._cas_hold
        if hold is not None:
            await hold()

    async def resolve_presence(self, presence_id: str) -> ResolvedSend:
        """Resolve an exact enabled account without consulting an outbound route."""
        async with self._database.read_session() as session:
            presence = await session.get(PresenceModel, presence_id)
        if presence is None or not presence.enabled:
            raise RouteSendError("original_presence_unavailable")
        return self._presence_connection(presence)

    def _presence_connection(self, presence: PresenceModel) -> ResolvedSend:
        """Use the Presence already selected in this read preparation."""
        try:
            resolution = self._registry.resolve_active(presence.id)
        except RegistryClosed as exc:
            raise RouteSendError("original_presence_unavailable") from exc
        return ResolvedSend(
            presence_id=presence.id,
            binding_id="",
            platform=presence.platform,
            external_target_id="",
            route_generation=resolution.snapshot.generation,
            connection=resolution,
            kind="account",
            sender_account_id=presence.external_account_id,
        )

    async def validate_prepared_send(
        self,
        session: AsyncSession,
        prepared: ResolvedSend,
        *,
        target_kind: str,
        target_id: str,
    ) -> None:
        """Recheck an observed route without network probes or route provisioning."""
        presence = await session.get(PresenceModel, prepared.presence_id)
        if (
            presence is None
            or not presence.enabled
            or presence.platform != prepared.platform
            or presence.external_account_id != prepared.sender_account_id
        ):
            raise RouteSendError("route_changed")
        if prepared.binding_id:
            binding = (
                await session.get(IdentityBindingModel, prepared.binding_id)
                if target_kind == "person"
                else await session.get(SpaceBindingModel, prepared.binding_id)
            )
            if (
                binding is None
                or binding.status != "active"
                or binding.platform != prepared.platform
                or not (
                    await same_person_id(session, binding.person_id, target_id)
                    if isinstance(binding, IdentityBindingModel)
                    else binding.space_id == target_id
                )
                or (
                    binding.external_account_id
                    if isinstance(binding, IdentityBindingModel)
                    else binding.external_space_id
                )
                != prepared.external_target_id
            ):
                raise RouteSendError("route_changed")
        if target_kind == "person" and prepared.binding_id:
            assert isinstance(binding, IdentityBindingModel)
            if not await session.scalar(
                select(current_person_enabled_condition(binding.person_id))
            ):
                raise RouteSendError("route_changed")
        if prepared.kind in {"person", "space"}:
            route = (
                await session.get(
                    PersonActiveRouteModel, await current_person_id(session, target_id)
                )
                if target_kind == "person"
                else await session.get(SpaceActiveRouteModel, target_id)
            )
            if (
                route is None
                or route.paused
                or route.presence_id != prepared.presence_id
                or route.route_generation != prepared.route_generation
                or (
                    route.identity_binding_id
                    if isinstance(route, PersonActiveRouteModel)
                    else route.space_binding_id
                )
                != prepared.binding_id
            ):
                raise RouteSendError("route_changed")
        if prepared.kind == "conversation":
            conversation = await session.get(CanonicalConversationModel, prepared.conversation_id)
            original_entry = False
            if (
                conversation is not None
                and target_kind == "person"
                and conversation.identity_binding_id == prepared.binding_id
            ):
                original_binding = await session.get(IdentityBindingModel, prepared.binding_id)
                if original_binding is not None:
                    original_entry = await event_entry_matches(
                        session, conversation, original_binding, presence, prepared.source_event_id
                    )
            if (
                conversation is None
                or conversation.thread_id != prepared.thread_id
                or not (
                    conversation.person_id is not None
                    and await same_person_id(session, conversation.person_id, target_id)
                    if target_kind == "person"
                    else conversation.space_id == target_id
                )
                or (
                    target_kind == "person"
                    and (
                        conversation.identity_binding_id != prepared.binding_id
                        or (
                            conversation.entry_presence_id is not None
                            and conversation.entry_presence_id != prepared.presence_id
                            and not original_entry
                        )
                    )
                )
                or (
                    target_kind == "space"
                    and conversation.space_binding_id is not None
                    and conversation.space_binding_id != prepared.binding_id
                )
            ):
                raise RouteSendError("route_changed")
        self.validate_prepared_connection(prepared)

    def validate_prepared_connection(self, prepared: ResolvedSend) -> None:
        """Check the exact live connection without I/O or changing the route."""

        try:
            current = self._registry.resolve_active(prepared.presence_id)
        except RegistryClosed as exc:
            raise RouteSendError(exc.category) from exc
        if current.snapshot != prepared.connection.snapshot:
            raise RouteSendError("route_changed")

    async def accessible_group_connections(
        self,
        space_id: str,
        *,
        binding_id: str | None = None,
        presence_id: str | None = None,
    ) -> list[ResolvedSend]:
        """Read-only reachability; neither outbound nor ingest pins govern reads."""
        async with self._database.read_session() as session:
            binding_query = select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == space_id,
                SpaceBindingModel.status == "active",
            )
            if binding_id is not None:
                binding_query = binding_query.where(SpaceBindingModel.id == binding_id)
            bindings = list(await session.scalars(binding_query))
            presence_query = select(PresenceModel).where(
                PresenceModel.enabled.is_(True),
            )
            if presence_id is not None:
                presence_query = presence_query.where(PresenceModel.id == presence_id)
            presences = list(await session.scalars(presence_query.order_by(PresenceModel.id)))
        if len(bindings) != 1:
            raise RouteSendError("binding_ambiguous" if bindings else "binding_unavailable")
        binding = bindings[0]
        connections = []
        for index, presence in enumerate(presences):
            if presence.platform != binding.platform:
                continue
            try:
                route = (
                    self._presence_connection(presence)
                    if index == 0
                    else await self.resolve_presence(presence.id)
                )
            except RouteSendError:
                continue
            if await self._probe(
                route.connection.bot, binding.external_space_id, presence.external_account_id
            ):
                connections.append(
                    ResolvedSend(
                        presence_id=route.presence_id,
                        binding_id=binding.id,
                        platform=binding.platform,
                        external_target_id=binding.external_space_id,
                        route_generation=route.route_generation,
                        connection=route.connection,
                        kind="space",
                        sender_account_id=route.sender_account_id,
                    )
                )
        if not connections:
            raise RouteSendError("group_unavailable")
        return connections

    async def resolve_send_for_conversation(
        self,
        conversation_id: str,
        *,
        presence_id: str | None = None,
        source_event_id: int | None = None,
    ) -> ResolvedSend:
        """Send to the original platform entry, preserving its private bot or topic."""
        async with self._database.read_session() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if conversation is None:
                raise RouteSendError("conversation_unavailable")
            if conversation.kind == "private":
                binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
                entry_presence_id = conversation.entry_presence_id
                if entry_presence_id is None:
                    # The old schema did not store an entry bot. Read the actual
                    # historical ingress, never the Person's current route.
                    originals = list(
                        await session.scalars(
                            select(ChatEventModel.ingress_presence_id)
                            .where(
                                ChatEventModel.canonical_conversation_id == conversation.id,
                                ChatEventModel.ingress_presence_id.is_not(None),
                            )
                            .distinct()
                            .limit(2)
                        )
                    )
                    if presence_id is not None:
                        original = await session.scalar(
                            select(ChatEventModel.id)
                            .where(
                                ChatEventModel.canonical_conversation_id == conversation.id,
                                ChatEventModel.ingress_presence_id == presence_id,
                            )
                            .limit(1)
                        )
                        if original is not None:
                            entry_presence_id = presence_id
                    elif len(originals) == 1:
                        entry_presence_id = originals[0]
                if (
                    binding is not None
                    and presence_id is not None
                    and entry_presence_id is not None
                    and presence_id != entry_presence_id
                ):
                    old_presence = await session.get(PresenceModel, presence_id)
                    if old_presence is not None and await event_entry_matches(
                        session, conversation, binding, old_presence, source_event_id
                    ):
                        entry_presence_id = presence_id
                if (
                    binding is None
                    or binding.status != "active"
                    or binding.person_id != conversation.person_id
                    or entry_presence_id is None
                    or (presence_id is not None and presence_id != entry_presence_id)
                ):
                    raise RouteSendError("conversation_entry_unavailable")
                presence = await session.get(PresenceModel, entry_presence_id)
                if presence is None or presence.platform != binding.platform:
                    raise RouteSendError("conversation_entry_unavailable")
                target = binding.external_account_id
                binding_id = binding.id
                platform = binding.platform
            else:
                space_binding = (
                    await session.get(SpaceBindingModel, conversation.space_binding_id)
                    if conversation.space_binding_id is not None
                    else None
                )
                if space_binding is None and conversation.space_binding_id is None:
                    # Only a single original legacy Space binding is readable.
                    original_bindings = list(
                        await session.scalars(
                            select(SpaceBindingModel)
                            .where(
                                SpaceBindingModel.space_id == conversation.space_id,
                                SpaceBindingModel.status == "active",
                            )
                            .limit(2)
                        )
                    )
                    space_binding = original_bindings[0] if len(original_bindings) == 1 else None
                if (
                    space_binding is None
                    or space_binding.status != "active"
                    or space_binding.space_id != conversation.space_id
                ):
                    raise RouteSendError("conversation_entry_unavailable")
                if presence_id is None:
                    route = await session.get(SpaceActiveRouteModel, space_binding.space_id)
                    if route is not None and route.space_binding_id == space_binding.id:
                        if route.paused:
                            raise RouteSendError("paused")
                        presence_id = route.presence_id
                    else:
                        ingest = await session.get(SpaceBindingIngestRouteModel, space_binding.id)
                        if ingest is not None and not ingest.paused:
                            presence_id = ingest.ingest_presence_id
                presence = (
                    await session.get(PresenceModel, presence_id)
                    if presence_id is not None
                    else None
                )
                if presence_id is not None and presence is None:
                    raise RouteSendError("original_presence_unavailable")
                target = space_binding.external_space_id
                binding_id = space_binding.id
                platform = space_binding.platform
        if conversation.kind == "space" and presence is None:
            connections = await self.accessible_group_connections(
                conversation.space_id or "", binding_id=binding_id
            )
            if len(connections) != 1:
                raise RouteSendError("ambiguous")
            resolved = connections[0]
        else:
            if presence is None or not presence.enabled:
                raise RouteSendError("original_presence_unavailable")
            if presence.platform != platform:
                raise RouteSendError("conversation_entry_unavailable")
            resolved = self._presence_connection(presence)
            try:
                require_capability(
                    resolved.connection,
                    "send_private" if conversation.kind == "private" else "send_group",
                )
            except RegistryClosed as exc:
                raise RouteSendError(exc.category) from exc
        return replace(
            resolved,
            kind="conversation",
            binding_id=binding_id,
            external_target_id=target,
            route_generation=0,
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
            source_event_id=source_event_id,
        )

    async def resolve_send_for_binding(
        self, person_id: str, binding_id: str, preferred_presence_id: str | None = None
    ) -> ResolvedSend:
        candidates = [
            item
            for item in await self._person_candidates(person_id)
            if item.binding_id == binding_id
        ]
        preferred = [item for item in candidates if item.presence_id == preferred_presence_id]
        if preferred:
            candidates = preferred
        if len(candidates) != 1:
            raise RouteSendError("ambiguous" if candidates else "binding_unavailable")
        candidate = candidates[0]
        route = await self.resolve_presence(candidate.presence_id)
        require_capability(route.connection, "send_private")
        return replace(
            route,
            binding_id=candidate.binding_id,
            external_target_id=candidate.external_target_id,
            platform=candidate.platform,
            kind="binding",
            route_generation=0,
        )

    async def resolve_send_for_person(self, person_id: str) -> ResolvedSend:
        async with self._database.read_session() as session:
            person_id = await current_person_id(session, person_id)
            route = await session.get(PersonActiveRouteModel, person_id)
        if route is None:
            await self.cas_takeover_person(person_id)
        async with self._database.read_session() as session:
            route = await session.get(PersonActiveRouteModel, person_id)
            if route is None:
                raise RouteSendError("none")
            if route.paused:
                raise RouteSendError("paused")
            binding = await session.get(IdentityBindingModel, route.identity_binding_id)
            presence = await session.get(PresenceModel, route.presence_id)
            if binding is None or presence is None or binding.status != "active":
                raise RouteSendError("none")
            if not await session.scalar(
                select(current_person_enabled_condition(binding.person_id))
            ):
                raise RouteSendError("paused")
            if (
                not await same_person_id(session, binding.person_id, person_id)
                or binding.platform != presence.platform
            ):
                raise RouteSendError("none")
            if not presence.enabled:
                raise RouteSendError("paused")
            try:
                resolution = self._registry.resolve_active(presence.id)
                require_capability(resolution, "send_private")
            except RegistryClosed as exc:
                raise RouteSendError(exc.category) from exc
            return ResolvedSend(
                presence_id=presence.id,
                binding_id=binding.id,
                platform=binding.platform,
                external_target_id=binding.external_account_id,
                route_generation=int(route.route_generation),
                connection=resolution,
                kind="person",
                sender_account_id=presence.external_account_id,
            )

    async def resolve_send_for_space_binding(
        self, space_id: str, space_binding_id: str, preferred_presence_id: str | None = None
    ) -> ResolvedSend:
        """Use the requested physical entry; a prepared receipt never changes routes."""
        async with self._database.read_session() as session:
            binding = await session.get(SpaceBindingModel, space_binding_id)
            if binding is None or binding.space_id != space_id or binding.status != "active":
                raise RouteSendError("binding_unavailable")
            if preferred_presence_id is None:
                route = await session.get(SpaceActiveRouteModel, space_id)
                if route is not None and route.space_binding_id == binding.id:
                    if route.paused:
                        raise RouteSendError("paused")
                    preferred_presence_id = route.presence_id
                else:
                    ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
                    if ingest is not None and not ingest.paused:
                        preferred_presence_id = ingest.ingest_presence_id
            presence = (
                await session.get(PresenceModel, preferred_presence_id)
                if preferred_presence_id
                else None
            )
        if preferred_presence_id is None:
            candidates = await self.accessible_group_connections(
                space_id, binding_id=space_binding_id
            )
            if len(candidates) != 1:
                raise RouteSendError("ambiguous" if candidates else "binding_unavailable")
            resolved = candidates[0]
        else:
            if presence is None or not presence.enabled or presence.platform != binding.platform:
                raise RouteSendError("original_presence_unavailable")
            resolved = self._presence_connection(presence)
            require_capability(resolved.connection, "send_group")
        return replace(
            resolved,
            kind="space_binding",
            binding_id=binding.id,
            external_target_id=binding.external_space_id,
            platform=binding.platform,
            route_generation=0,
        )

    async def resolve_send_for_space(self, space_id: str) -> ResolvedSend:
        async with self._database.read_session() as session:
            route = await session.get(SpaceActiveRouteModel, space_id)
        if route is None:
            await self.cas_takeover_space(space_id)
        async with self._database.read_session() as session:
            route = await session.get(SpaceActiveRouteModel, space_id)
            if route is None:
                raise RouteSendError("none")
            if route.paused:
                raise RouteSendError("paused")
            binding = await session.get(SpaceBindingModel, route.space_binding_id)
            presence = await session.get(PresenceModel, route.presence_id)
            if binding is None or presence is None:
                raise RouteSendError("none")
            if binding.space_id != space_id or binding.platform != presence.platform:
                raise RouteSendError("none")
            if not presence.enabled:
                raise RouteSendError("paused")
            try:
                resolution = self._registry.resolve_active(presence.id)
                require_capability(resolution, "send_group")
            except RegistryClosed as exc:
                raise RouteSendError(exc.category) from exc
            return ResolvedSend(
                presence_id=presence.id,
                binding_id=binding.id,
                platform=binding.platform,
                external_target_id=binding.external_space_id,
                route_generation=int(route.route_generation),
                connection=resolution,
                kind="space",
                sender_account_id=presence.external_account_id,
            )

    async def evaluate_ingest(
        self,
        *,
        space_binding_id: str,
        event_presence_id: str,
        expected_connection: ConnectionSnapshot | None = None,
        expected_presence_revision: int | None = None,
        expected_binding: tuple[str, int] | None = None,
    ) -> str:
        """Return 'ok', 'paused', or 'not_ingest' without touching policy."""

        status = await self._provision_ingest_route(
            space_binding_id,
            event_presence_id=event_presence_id,
            expected_connection=expected_connection,
            expected_presence_revision=expected_presence_revision,
            expected_binding=expected_binding,
        )
        if status == "conflict":
            return await self._ingest_read_status(
                space_binding_id, event_presence_id=event_presence_id
            )
        if status != "ok":
            return status
        return await self._ingest_read_status(space_binding_id, event_presence_id=event_presence_id)

    async def cas_takeover_person(
        self,
        person_id: str,
        *,
        expected_generation: int | None = None,
    ) -> str:
        async with self._database.read_session() as session:
            person_id = await current_person_id(session, person_id)
        candidates = await self._person_candidates(person_id)
        return await self._apply_person_takeover(
            person_id,
            candidates,
            command_generation=expected_generation,
        )

    async def cas_takeover_space(
        self,
        space_id: str,
        *,
        expected_generation: int | None = None,
    ) -> str:
        candidates = await self._space_candidates(space_id)
        return await self._apply_space_takeover(
            space_id,
            candidates,
            command_generation=expected_generation,
        )

    async def reconcile_person(self, person_id: str) -> str:
        return await self.cas_takeover_person(person_id)

    async def reconcile_space(self, space_id: str) -> str:
        return await self.cas_takeover_space(space_id)

    async def group_recovery_candidate(self, space_binding_id: str) -> RouteCandidate:
        """Plan an explicit recovery without changing a route or overriding a live pin."""

        async with self._database.read_session() as session:
            binding = await session.get(SpaceBindingModel, space_binding_id)
            route = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
        if binding is None or binding.status != "active":
            raise RouteSendError("none")
        if route is not None and await self._ingest_pin_healthy(route, binding):
            return RouteCandidate(
                route.ingest_presence_id, binding.id, binding.platform, binding.external_space_id
            )
        candidates = [
            candidate
            for candidate in _unique_candidates(
                await self._space_candidates(binding.space_id, ingest=True)
            )
            if candidate.binding_id == binding.id
        ]
        if len(candidates) != 1:
            raise RouteSendError("ambiguous" if candidates else "none")
        return candidates[0]

    async def unbound_group_recovery_presence(self, group_id: str, *, platform: str) -> str:
        """Prove one live platform member before an explicit first group registration."""
        identities = self._registry.connected_presence_ids(platform)
        if not identities:
            raise RouteSendError("none")
        async with self._database.read_session() as session:
            presences = list(
                await session.scalars(
                    select(PresenceModel)
                    .where(PresenceModel.id.in_(identities))
                    .order_by(PresenceModel.id)
                    .limit(len(identities))
                )
            )
        found: list[str] = []
        for presence in presences:
            if (
                not presence.enabled
                or not presence.ingest_eligible
                or presence.platform != platform
            ):
                continue
            try:
                connection = self._registry.resolve_active(presence.id)
                if (
                    connection.snapshot.platform != presence.platform
                    or connection.snapshot.external_account_id != presence.external_account_id
                ):
                    continue
                require_capability(connection, "group_member_probe")
                require_capability(connection, "send_group")
            except RegistryClosed:
                continue
            if await self._probe(connection.bot, group_id, presence.external_account_id):
                found.append(presence.id)
                if len(found) > 1:
                    raise RouteSendError("ambiguous")
        if not found:
            raise RouteSendError("none")
        return found[0]

    async def space_send_pin_healthy(self, space_id: str) -> bool:
        """Whether explicit group recovery should preserve the existing send route."""

        async with self._database.read_session() as session:
            route = await session.get(SpaceActiveRouteModel, space_id)
        return route is not None and not route.paused and await self._space_pin_healthy(route)

    async def reconcile_all(self) -> tuple[int, int]:
        async with self._database.read_session() as session:
            people = list(await session.scalars(select(PersonActiveRouteModel.person_id)))
            extra_people = list(await session.scalars(select(IdentityBindingModel.person_id)))
            spaces = list(await session.scalars(select(SpaceActiveRouteModel.space_id)))
            extra_spaces = list(await session.scalars(select(SpaceBindingModel.space_id)))
            ingest_bindings = list(await session.scalars(select(SpaceBindingModel.id)))
        people = list(dict.fromkeys([*people, *extra_people]))
        spaces = list(dict.fromkeys([*spaces, *extra_spaces]))
        person_count = 0
        space_count = 0
        for person_id in people:
            await self.reconcile_person(str(person_id))
            person_count += 1
        for space_id in spaces:
            await self.reconcile_space(str(space_id))
            space_count += 1
        for binding_id in ingest_bindings:
            await self._provision_ingest_route(str(binding_id), event_presence_id=None)
        return person_count, space_count

    async def _person_candidates(
        self, person_id: str, *, session: AsyncSession | None = None
    ) -> list[RouteCandidate]:
        if session is None:
            async with self._database.read_session() as reader:
                return await self._person_candidates(person_id, session=reader)
        bindings = list(
            await session.scalars(
                select(IdentityBindingModel)
                .join(AccountModel)
                .options(contains_eager(IdentityBindingModel.account))
                .where(
                    person_family_condition(IdentityBindingModel.person_id, person_id),
                    current_person_enabled_condition(IdentityBindingModel.person_id),
                    IdentityBindingModel.status == "active",
                )
            )
        )
        platforms = {binding.platform for binding in bindings}
        connected_ids = {
            identity
            for platform in platforms
            for identity in self._registry.connected_presence_ids(platform)
        }
        presences = (
            list(
                await session.scalars(
                    select(PresenceModel)
                    .join(AccountModel)
                    .options(contains_eager(PresenceModel.account))
                    .where(PresenceModel.status == "active", PresenceModel.id.in_(connected_ids))
                )
            )
            if connected_ids
            else []
        )
        by_platform: dict[str, list[PresenceModel]] = {}
        for presence in presences:
            try:
                resolution = self._registry.resolve_active(presence.id)
                require_capability(resolution, "send_private")
            except RegistryClosed:
                continue
            if (
                resolution.snapshot.platform == presence.platform
                and resolution.snapshot.external_account_id == presence.external_account_id
            ):
                by_platform.setdefault(presence.platform, []).append(presence)
        found: list[RouteCandidate] = []
        for binding in bindings:
            for presence in by_platform.get(binding.platform, ()):
                found.append(
                    RouteCandidate(
                        presence_id=presence.id,
                        binding_id=binding.id,
                        platform=binding.platform,
                        external_target_id=binding.external_account_id,
                    )
                )
        return found

    async def prepare_person_merge_routes(
        self, session: AsyncSession, source_person_id: str, target_person_id: str
    ) -> Callable[[AsyncSession], Awaitable[Callable[[], None]]]:
        """Bind live route rows and actual available candidates in the applying writer."""

        async def bind(active: AsyncSession) -> Callable[[], None]:
            roots = await current_person_ids(active, (source_person_id, target_person_id))
            if source_person_id not in roots or target_person_id not in roots:
                raise CanonicalIdentityError("missing_canonical_owner")
            source_id, target_id = roots[source_person_id], roots[target_person_id]
            if source_id == target_id:
                return lambda: None
            routes = {
                row.person_id: row
                for row in await active.scalars(
                    select(PersonActiveRouteModel).where(
                        PersonActiveRouteModel.person_id.in_((source_id, target_id))
                    )
                )
            }
            source, target = routes.get(source_id), routes.get(target_id)
            source_candidates = await self._person_candidates(source_id, session=active)
            target_candidates = await self._person_candidates(target_id, session=active)
            available = {
                (item.binding_id, item.presence_id)
                for item in (*source_candidates, *target_candidates)
            }
            target_person = await active.get(CanonicalPersonModel, target_id)
            keep_target = target is not None and (
                target.paused or ((target.identity_binding_id, target.presence_id) in available)
            )
            inherit_source = (
                source is not None
                and not source.paused
                and target_person is not None
                and target_person.enabled
                and ((source.identity_binding_id, source.presence_id) in available)
            )
            now = datetime.now(UTC)

            def apply() -> None:
                if not keep_target:
                    if inherit_source:
                        assert source is not None
                        if target is None:
                            active.add(
                                PersonActiveRouteModel(
                                    person_id=target_id,
                                    identity_binding_id=source.identity_binding_id,
                                    presence_id=source.presence_id,
                                    route_generation=1,
                                    paused=False,
                                    revision=1,
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                        else:
                            target.identity_binding_id = source.identity_binding_id
                            target.presence_id = source.presence_id
                            target.paused = False
                            target.route_generation += 1
                            target.revision += 1
                            target.updated_at = now
                    elif target is not None:
                        active.sync_session.delete(target)
                if source is not None:
                    active.sync_session.delete(source)

            return apply

        return bind

    async def _space_candidates(
        self,
        space_id: str,
        *,
        ingest: bool = False,
    ) -> list[RouteCandidate]:
        async with self._database.read_session() as session:
            bindings = list(
                await session.scalars(
                    select(SpaceBindingModel).where(
                        SpaceBindingModel.space_id == space_id, SpaceBindingModel.status == "active"
                    )
                )
            )
            identities = {
                identity
                for binding in bindings
                for identity in self._registry.connected_presence_ids(binding.platform)
            }
            presences = (
                list(
                    await session.scalars(
                        select(PresenceModel).where(
                            PresenceModel.id.in_(identities), PresenceModel.status == "active"
                        )
                    )
                )
                if identities
                else []
            )
        found: list[RouteCandidate] = []
        for binding in bindings:
            for presence in presences:
                if ingest and not presence.ingest_eligible:
                    continue
                if presence.platform != binding.platform:
                    continue
                try:
                    resolution = self._registry.resolve_active(presence.id)
                    require_capability(
                        resolution,
                        "group_member_probe" if ingest else "send_group",
                    )
                    if ingest:
                        require_capability(resolution, "send_group")
                except RegistryClosed:
                    continue
                if not await self._probe(
                    resolution.bot,
                    binding.external_space_id,
                    presence.external_account_id,
                ):
                    continue
                found.append(
                    RouteCandidate(
                        presence_id=presence.id,
                        binding_id=binding.id,
                        platform=binding.platform,
                        external_target_id=binding.external_space_id,
                    )
                )
        return found

    async def _space_pin_healthy(
        self,
        route: SpaceActiveRouteModel,
        *,
        ingest: bool = False,
    ) -> bool:
        async with self._database.read_session() as session:
            binding = await session.get(SpaceBindingModel, route.space_binding_id)
            presence = await session.get(PresenceModel, route.presence_id)
        if binding is None or presence is None or binding.status != "active":
            return False
        if not presence.enabled:
            return False
        if ingest and not presence.ingest_eligible:
            return False
        try:
            resolution = self._registry.resolve_active(presence.id)
            require_capability(resolution, "send_group")
            if ingest:
                require_capability(resolution, "group_member_probe")
        except RegistryClosed:
            return False
        return await self._probe(
            resolution.bot,
            binding.external_space_id,
            presence.external_account_id,
        )

    async def _ingest_pin_healthy(
        self,
        route: SpaceBindingIngestRouteModel,
        binding: SpaceBindingModel,
    ) -> bool:
        async with self._database.read_session() as session:
            presence = await session.get(PresenceModel, route.ingest_presence_id)
            current = await session.get(SpaceBindingModel, binding.id)
        if presence is None or current is None or current.status != "active":
            return False
        if not presence.enabled or not presence.ingest_eligible:
            return False
        try:
            resolution = self._registry.resolve_active(presence.id)
            require_capability(resolution, "send_group")
            require_capability(resolution, "group_member_probe")
        except RegistryClosed:
            return False
        return await self._probe(
            resolution.bot,
            current.external_space_id,
            presence.external_account_id,
        )

    async def _ingest_read_status(
        self,
        space_binding_id: str,
        *,
        event_presence_id: str,
    ) -> str:
        async with self._database.read_session() as session:
            return await self.ingest_status_in_session(
                session, space_binding_id=space_binding_id, event_presence_id=event_presence_id
            )

    async def authenticated_ingest_status_in_session(
        self,
        session: AsyncSession,
        *,
        space_binding_id: str,
        event_presence_id: str,
        connection: ConnectionResolution,
    ) -> str | None:
        """Reuse a same-Presence pin only for an authenticated inbound handle.

        None leaves cold provisioning and unhealthy-pin recovery to the original
        membership/CAS path. This read does not authorize the eventual ledger write.
        """
        route = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
        if route is None:
            return None
        if route.paused:
            return "paused"
        if route.ingest_presence_id != event_presence_id:
            return None
        status = await self.ingest_status_in_session(
            session,
            space_binding_id=space_binding_id,
            event_presence_id=event_presence_id,
            require_connected=False,
        )
        if status != "ok":
            return None
        presence = await session.get(PresenceModel, event_presence_id)
        binding = await session.get(SpaceBindingModel, space_binding_id)
        assert presence is not None and binding is not None
        original = connection.snapshot
        if (
            presence.platform != original.platform
            or presence.external_account_id != original.external_account_id
            or binding.platform != original.platform
        ):
            return "not_ingest"
        try:
            current = self._registry.resolve_by_handle(connection.bot)
        except RegistryClosed:
            return "paused"
        if (
            current.snapshot.connection_id != original.connection_id
            or current.snapshot.generation != original.generation
            or current.snapshot.gateway_instance_id != original.gateway_instance_id
            or current.snapshot.platform != original.platform
            or current.snapshot.external_account_id != original.external_account_id
            or current.snapshot.presence_id != event_presence_id
        ):
            return "not_ingest"
        try:
            active = self._registry.resolve_active(event_presence_id)
            require_capability(active, "send_group")
            require_capability(active, "group_member_probe")
        except RegistryClosed:
            return None
        if active.snapshot != current.snapshot or active.bot is not connection.bot:
            return "not_ingest"
        return "ok"

    async def ingest_status_in_session(
        self,
        session: AsyncSession,
        *,
        space_binding_id: str,
        event_presence_id: str,
        require_connected: bool = True,
    ) -> str:
        """Recheck the route; an already admitted command may outlive its socket."""
        binding = await session.get(SpaceBindingModel, space_binding_id)
        if binding is None or binding.status != "active":
            return "not_ingest"
        route = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
        if route is None:
            return "not_ingest"
        if route.paused:
            return "paused"
        if route.ingest_presence_id != event_presence_id:
            return "not_ingest"
        presence = await session.get(PresenceModel, route.ingest_presence_id)
        if presence is None or not presence.enabled or not presence.ingest_eligible:
            return "not_ingest"
        if require_connected:
            try:
                self._registry.resolve_active(presence.id)
            except RegistryClosed:
                return "paused"
        return "ok"

    async def _apply_person_takeover(
        self,
        person_id: str,
        candidates: list[RouteCandidate],
        *,
        command_generation: int | None,
    ) -> str:
        now = datetime.now(UTC)
        unique = _unique_candidates(candidates)
        commanded = command_generation is not None
        async with self._database.read_session() as session:
            existing = await session.get(PersonActiveRouteModel, person_id)
            observed = None if existing is None else _observe_person(existing)
        if observed is None:
            if len(unique) != 1:
                return "paused" if unique else "none"
            await self._await_cas_hold()
            return await self._insert_person_route(person_id, unique[0], now=now)
        if commanded and observed.route_generation != command_generation:
            return "conflict"
        if observed.paused and not commanded:
            return "paused"
        if not commanded and any(
            item.binding_id == observed.identity_binding_id
            and item.presence_id == observed.presence_id
            for item in unique
        ):
            return "unchanged"
        await self._await_cas_hold()
        return await self._write_person_transition(
            person_id,
            unique,
            observed=observed,
            command_generation=command_generation,
            now=now,
        )

    async def _insert_person_route(
        self,
        person_id: str,
        winner: RouteCandidate,
        *,
        now: datetime,
    ) -> str:
        async with self._database.immediate_session() as session:
            if await session.get(PersonActiveRouteModel, person_id) is not None:
                return "conflict"
            session.add(
                PersonActiveRouteModel(
                    person_id=person_id,
                    identity_binding_id=winner.binding_id,
                    presence_id=winner.presence_id,
                    route_generation=1,
                    paused=False,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        return "taken"

    async def _write_person_transition(
        self,
        person_id: str,
        unique: list[RouteCandidate],
        *,
        observed: ObservedPersonRoute,
        command_generation: int | None,
        now: datetime,
    ) -> str:
        async with self._database.immediate_session() as session:
            route = await session.get(PersonActiveRouteModel, person_id)
            if route is None:
                return "conflict"
            if command_generation is not None:
                if int(route.route_generation) != command_generation:
                    return "conflict"
            elif not _person_matches(route, observed):
                return "conflict"
            if not unique:
                # A gateway may reconnect a moment after its WebSocket closes.
                # Keep the last deterministic route pinned while it is offline;
                # send resolution still fails closed through the Registry.
                return "none"
            if len(unique) > 1:
                if route.paused:
                    return "paused"
                route.paused = True
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
                return "ambiguous"
            winner = unique[0]
            if (
                route.identity_binding_id == winner.binding_id
                and route.presence_id == winner.presence_id
                and not route.paused
            ):
                return "unchanged"
            route.identity_binding_id = winner.binding_id
            route.presence_id = winner.presence_id
            route.paused = False
            route.route_generation += 1
            route.revision += 1
            route.updated_at = now
            return "taken"

    async def _apply_space_takeover(
        self,
        space_id: str,
        candidates: list[RouteCandidate],
        *,
        command_generation: int | None,
    ) -> str:
        now = datetime.now(UTC)
        unique = _unique_candidates(candidates)
        commanded = command_generation is not None
        async with self._database.read_session() as session:
            existing = await session.get(SpaceActiveRouteModel, space_id)
            observed = None if existing is None else _observe_space(existing)
        if observed is None:
            if len(unique) != 1:
                return "paused" if unique else "none"
            await self._await_cas_hold()
            return await self._insert_space_route(space_id, unique[0], now=now)
        if commanded and observed.route_generation != command_generation:
            return "conflict"
        if observed.paused and not commanded:
            return "paused"
        if not commanded and existing is not None and await self._space_pin_healthy(existing):
            return "unchanged"
        await self._await_cas_hold()
        return await self._write_space_transition(
            space_id,
            unique,
            observed=observed,
            command_generation=command_generation,
            now=now,
        )

    async def _insert_space_route(
        self,
        space_id: str,
        winner: RouteCandidate,
        *,
        now: datetime,
    ) -> str:
        async with self._database.immediate_session() as session:
            if await session.get(SpaceActiveRouteModel, space_id) is not None:
                return "conflict"
            session.add(
                SpaceActiveRouteModel(
                    space_id=space_id,
                    space_binding_id=winner.binding_id,
                    presence_id=winner.presence_id,
                    route_generation=1,
                    paused=False,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        return "taken"

    async def _write_space_transition(
        self,
        space_id: str,
        unique: list[RouteCandidate],
        *,
        observed: ObservedSpaceRoute,
        command_generation: int | None,
        now: datetime,
    ) -> str:
        async with self._database.immediate_session() as session:
            route = await session.get(SpaceActiveRouteModel, space_id)
            if route is None:
                return "conflict"
            if command_generation is not None:
                if int(route.route_generation) != command_generation:
                    return "conflict"
            elif not _space_matches(route, observed):
                return "conflict"
            if not unique:
                # Zero live candidates means temporary unavailability, not an
                # operator pause. Preserve the route for the reconnecting Presence.
                return "none"
            if len(unique) > 1:
                if route.paused:
                    return "paused"
                route.paused = True
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
                return "ambiguous"
            winner = unique[0]
            if (
                route.space_binding_id == winner.binding_id
                and route.presence_id == winner.presence_id
                and not route.paused
            ):
                return "unchanged"
            route.space_binding_id = winner.binding_id
            route.presence_id = winner.presence_id
            route.paused = False
            route.route_generation += 1
            route.revision += 1
            route.updated_at = now
            return "taken"

    async def _provision_ingest_route(
        self,
        space_binding_id: str,
        *,
        event_presence_id: str | None,
        expected_connection: ConnectionSnapshot | None = None,
        expected_presence_revision: int | None = None,
        expected_binding: tuple[str, int] | None = None,
    ) -> str:
        async with self._database.read_session() as session:
            binding = await session.get(SpaceBindingModel, space_binding_id)
            if binding is None:
                return "not_ingest"
            binding_version = (binding.space_id, binding.revision)
            if expected_binding is not None and binding_version != expected_binding:
                return "not_ingest"
            route = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
            observed = None if route is None else _observe_ingest(route)
            space_id = binding.space_id
        # Bound preparation/probes, not the route's physical commit lifecycle.
        probe_started = time.perf_counter()
        async with asyncio.timeout(10):
            if observed is not None and route is not None:
                if observed.paused:
                    return "paused"
                if await self._ingest_pin_healthy(route, binding):
                    if (
                        event_presence_id is None
                        or event_presence_id == observed.ingest_presence_id
                    ):
                        return "ok"
                    return "not_ingest"
            candidates = await self._space_candidates(space_id, ingest=True)
        logging.getLogger(__name__).info(
            "ingress_probe preparation_seconds=%.6f candidate_count=%d",
            time.perf_counter() - probe_started,
            len(candidates),
        )
        unique = [
            item for item in _unique_candidates(candidates) if item.binding_id == space_binding_id
        ]
        now = datetime.now(UTC)
        await self._await_cas_hold()
        async with self._database.immediate_session() as session:
            current_binding = await session.get(SpaceBindingModel, space_binding_id)
            if (
                current_binding is None
                or current_binding.status != "active"
                or (current_binding.space_id, current_binding.revision) != binding_version
            ):
                return "conflict"
            if expected_connection is not None:
                try:
                    live = self._registry.resolve_active(event_presence_id or "")
                except RegistryClosed:
                    return "conflict"
                if live.snapshot != expected_connection:
                    return "conflict"
                original_presence = await session.get(
                    PresenceModel, expected_connection.presence_id
                )
                if (
                    original_presence is None
                    or original_presence.revision != expected_presence_revision
                ):
                    return "conflict"
            for candidate in unique:
                candidate_presence = await session.get(PresenceModel, candidate.presence_id)
                if (
                    candidate_presence is None
                    or not candidate_presence.enabled
                    or not candidate_presence.ingest_eligible
                    or candidate_presence.platform != current_binding.platform
                ):
                    return "conflict"
            current = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
            if not _ingest_matches(current, observed):
                return "conflict"
            if current is None:
                if len(unique) != 1:
                    return "paused" if unique else "not_ingest"
                winner = unique[0]
                if event_presence_id is not None and winner.presence_id != event_presence_id:
                    return "not_ingest"
                session.add(
                    SpaceBindingIngestRouteModel(
                        space_binding_id=space_binding_id,
                        ingest_presence_id=winner.presence_id,
                        route_generation=1,
                        paused=False,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return "ok"
            if current.paused:
                return "paused"
            if not unique:
                # Do not turn a transient provider disconnect into a durable
                # ingest pause. The connection fence remains closed until reconnect.
                return "not_ingest"
            if len(unique) > 1:
                current.paused = True
                current.route_generation += 1
                current.revision += 1
                current.updated_at = now
                return "paused"
            winner = unique[0]
            if current.ingest_presence_id == winner.presence_id:
                return "ok"
            current.ingest_presence_id = winner.presence_id
            current.paused = False
            current.route_generation += 1
            current.revision += 1
            current.updated_at = now
            return "ok"


def _observe_person(route: PersonActiveRouteModel) -> ObservedPersonRoute:
    return ObservedPersonRoute(
        route_generation=int(route.route_generation),
        revision=int(route.revision),
        identity_binding_id=route.identity_binding_id,
        presence_id=route.presence_id,
        paused=bool(route.paused),
    )


def _person_matches(route: PersonActiveRouteModel, observed: ObservedPersonRoute) -> bool:
    return (
        int(route.route_generation) == observed.route_generation
        and int(route.revision) == observed.revision
        and route.identity_binding_id == observed.identity_binding_id
        and route.presence_id == observed.presence_id
        and bool(route.paused) == observed.paused
    )


def _observe_space(route: SpaceActiveRouteModel) -> ObservedSpaceRoute:
    return ObservedSpaceRoute(
        route_generation=int(route.route_generation),
        revision=int(route.revision),
        space_binding_id=route.space_binding_id,
        presence_id=route.presence_id,
        paused=bool(route.paused),
    )


def _space_matches(route: SpaceActiveRouteModel, observed: ObservedSpaceRoute) -> bool:
    return (
        int(route.route_generation) == observed.route_generation
        and int(route.revision) == observed.revision
        and route.space_binding_id == observed.space_binding_id
        and route.presence_id == observed.presence_id
        and bool(route.paused) == observed.paused
    )


def _observe_ingest(route: SpaceBindingIngestRouteModel) -> ObservedIngestRoute:
    return ObservedIngestRoute(
        route_generation=int(route.route_generation),
        revision=int(route.revision),
        ingest_presence_id=route.ingest_presence_id,
        paused=bool(route.paused),
    )


def _ingest_matches(
    route: SpaceBindingIngestRouteModel | None,
    observed: ObservedIngestRoute | None,
) -> bool:
    if route is None or observed is None:
        return route is None and observed is None
    return (
        int(route.route_generation) == observed.route_generation
        and int(route.revision) == observed.revision
        and route.ingest_presence_id == observed.ingest_presence_id
        and bool(route.paused) == observed.paused
    )


def _unique_candidates(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
    seen: dict[tuple[str, str], RouteCandidate] = {}
    for item in candidates:
        seen[(item.presence_id, item.binding_id)] = item
    return list(seen.values())


class RouteMonitor:
    """Reconcile routes when Registry membership changes. No first-item fallback."""

    def __init__(self, router: PresenceRouter) -> None:
        self._router = router
        self.last_reconcile_id = str(uuid4())

    async def on_connection_change(self) -> tuple[int, int]:
        result = await self._router.reconcile_all()
        self.last_reconcile_id = str(uuid4())
        return result
