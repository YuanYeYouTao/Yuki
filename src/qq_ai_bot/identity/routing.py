"""Deterministic Person/Space route resolution through the memory Registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionRegistry,
    RegistryClosed,
    require_capability,
)
from qq_ai_bot.identity.db_models import (
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.persistence.database import Database

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


class PresenceReader(Protocol):
    async def get(self, presence_id: str) -> PresenceModel | None: ...


async def default_membership_probe(bot: object, group_id: str, user_id: str) -> bool:
    """Realtime group membership. Fail-closed; never logs identifiers."""

    call_api = getattr(bot, "call_api", None)
    if not callable(call_api):
        return False
    try:
        await call_api(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=True,
        )
    except Exception:
        return False
    return True


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
        self._probe = membership_probe or default_membership_probe
        self._cas_hold: Callable[[], Awaitable[None]] | None = None

    async def uses_canonical_send(self) -> bool:
        async with self._database.sessions() as session:
            return await identity_runtime_is_complete_v2(session)

    async def person_owns_external(
        self,
        person_id: str,
        external_id: str,
        *,
        allow_unknown: bool = False,
    ) -> bool:
        async with self._database.sessions() as session:
            from qq_ai_bot.identity.shadows import person_id_for

            found = await person_id_for(session, external_id)
        if found is None:
            return allow_unknown
        return found == person_id

    async def space_owns_external(
        self,
        space_id: str,
        external_id: str,
        *,
        allow_unknown: bool = False,
    ) -> bool:
        async with self._database.sessions() as session:
            from qq_ai_bot.identity.shadows import space_id_for

            found = await space_id_for(session, external_id)
        if found is None:
            return allow_unknown
        return found == space_id

    async def _await_cas_hold(self) -> None:
        hold = self._cas_hold
        if hold is not None:
            await hold()

    async def resolve_send_for_account(
        self,
        bot_user_id: str,
        *,
        capability: str = "send_private",
    ) -> ResolvedSend:
        """v1 provenance path: exact account through Registry, never get_bots()."""

        resolution = self._registry.resolve_account(IDENTITY_PLATFORM, bot_user_id)
        require_capability(resolution, capability)
        return ResolvedSend(
            presence_id=resolution.snapshot.presence_id or "",
            binding_id="",
            platform=resolution.snapshot.platform,
            external_target_id="",
            route_generation=resolution.snapshot.generation,
            connection=resolution,
            kind="account",
            sender_account_id=resolution.snapshot.external_account_id,
        )

    async def resolve_send_for_target(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
    ) -> ResolvedSend:
        """v2 Person/Space route at send time; v1 stays exact-account Registry."""

        capability = "send_group" if target_type == "group" else "send_private"
        async with self._database.sessions() as session:
            v2 = await identity_runtime_is_complete_v2(session)
            person_id = None
            space_id = None
            if v2 and target_type == "private":
                from qq_ai_bot.identity.shadows import person_id_for

                person_id = await person_id_for(session, target_id)
            elif v2 and target_type == "group":
                from qq_ai_bot.identity.shadows import space_id_for

                space_id = await space_id_for(session, target_id)
            elif v2:
                raise RouteSendError("none")
        if v2:
            if target_type == "private":
                if person_id is None:
                    raise RouteSendError("none")
                return await self.resolve_send_for_person(person_id)
            if space_id is None:
                raise RouteSendError("none")
            return await self.resolve_send_for_space(space_id)
        return await self.resolve_send_for_account(bot_user_id, capability=capability)

    async def resolve_send_for_person(self, person_id: str) -> ResolvedSend:
        async with self._database.sessions() as session:
            route = await session.get(PersonActiveRouteModel, person_id)
        if route is None:
            await self.cas_takeover_person(person_id)
        async with self._database.sessions() as session:
            route = await session.get(PersonActiveRouteModel, person_id)
            if route is None:
                raise RouteSendError("none")
            if route.paused:
                raise RouteSendError("paused")
            binding = await session.get(IdentityBindingModel, route.identity_binding_id)
            presence = await session.get(PresenceModel, route.presence_id)
            if binding is None or presence is None:
                raise RouteSendError("none")
            if binding.person_id != person_id or binding.platform != presence.platform:
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

    async def resolve_send_for_space(self, space_id: str) -> ResolvedSend:
        async with self._database.sessions() as session:
            route = await session.get(SpaceActiveRouteModel, space_id)
        if route is None:
            await self.cas_takeover_space(space_id)
        async with self._database.sessions() as session:
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
            member = await self._probe(
                resolution.bot,
                binding.external_space_id,
                presence.external_account_id,
            )
            if not member:
                raise RouteSendError("paused")
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
    ) -> str:
        """Return 'ok', 'paused', or 'not_ingest' without touching policy."""

        status = await self._provision_ingest_route(
            space_binding_id,
            event_presence_id=event_presence_id,
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

    async def reconcile_all(self) -> tuple[int, int]:
        async with self._database.sessions() as session:
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

    async def _person_candidates(self, person_id: str) -> list[RouteCandidate]:
        async with self._database.sessions() as session:
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
                )
            )
            presences = list(await session.scalars(select(PresenceModel)))
        found: list[RouteCandidate] = []
        for binding in bindings:
            if binding.status != "active":
                continue
            for presence in presences:
                if not presence.enabled:
                    continue
                if presence.platform != binding.platform:
                    continue
                try:
                    resolution = self._registry.resolve_active(presence.id)
                    require_capability(resolution, "send_private")
                except RegistryClosed:
                    continue
                found.append(
                    RouteCandidate(
                        presence_id=presence.id,
                        binding_id=binding.id,
                        platform=binding.platform,
                        external_target_id=binding.external_account_id,
                    )
                )
        return found

    async def _space_candidates(
        self,
        space_id: str,
        *,
        ingest: bool = False,
    ) -> list[RouteCandidate]:
        async with self._database.sessions() as session:
            bindings = list(
                await session.scalars(
                    select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
                )
            )
            presences = list(await session.scalars(select(PresenceModel)))
        found: list[RouteCandidate] = []
        for binding in bindings:
            if binding.status != "active":
                continue
            for presence in presences:
                if not presence.enabled:
                    continue
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

    async def _person_pin_healthy(self, route: PersonActiveRouteModel) -> bool:
        async with self._database.sessions() as session:
            binding = await session.get(IdentityBindingModel, route.identity_binding_id)
            presence = await session.get(PresenceModel, route.presence_id)
        if binding is None or presence is None or binding.status != "active":
            return False
        if not presence.enabled:
            return False
        try:
            resolution = self._registry.resolve_active(presence.id)
            require_capability(resolution, "send_private")
        except RegistryClosed:
            return False
        return True

    async def _space_pin_healthy(
        self,
        route: SpaceActiveRouteModel,
        *,
        ingest: bool = False,
    ) -> bool:
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        if not commanded and existing is not None and await self._person_pin_healthy(existing):
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
            if len(unique) != 1:
                if route.paused:
                    return "paused" if unique else "none"
                route.paused = True
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
                return "paused" if not unique else "ambiguous"
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
        async with self._database.sessions() as session:
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
            if len(unique) != 1:
                if route.paused:
                    return "paused" if unique else "none"
                route.paused = True
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
                return "paused" if not unique else "ambiguous"
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
    ) -> str:
        async with self._database.sessions() as session:
            binding = await session.get(SpaceBindingModel, space_binding_id)
            if binding is None:
                return "not_ingest"
            route = await session.get(SpaceBindingIngestRouteModel, space_binding_id)
            observed = None if route is None else _observe_ingest(route)
            space_id = binding.space_id
        if observed is not None and route is not None:
            if observed.paused:
                return "paused"
            if await self._ingest_pin_healthy(route, binding):
                if event_presence_id is None or event_presence_id == observed.ingest_presence_id:
                    return "ok"
                return "not_ingest"
        candidates = await self._space_candidates(space_id, ingest=True)
        unique = [
            item for item in _unique_candidates(candidates) if item.binding_id == space_binding_id
        ]
        now = datetime.now(UTC)
        await self._await_cas_hold()
        async with self._database.immediate_session() as session:
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
            if len(unique) != 1:
                current.paused = True
                current.route_generation += 1
                current.revision += 1
                current.updated_at = now
                return "paused" if unique else "not_ingest"
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


async def send_uses_canonical_route(session: AsyncSession) -> bool:
    return await identity_runtime_is_complete_v2(session)
