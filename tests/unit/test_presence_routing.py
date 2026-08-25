"""C18 deterministic routing: 0/1/>1 takeover, pause, generation isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.support.gateway import napcat_registry

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.routing import PresenceRouter, RouteMonitor, RouteSendError
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_takeover_zero_one_many_and_route_pause(database: Database) -> None:
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-route")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        person_id = await _ensure_person_id(session, "1001")
    empty = await router.cas_takeover_person(person_id)
    assert empty == "none"
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    taken = await router.cas_takeover_person(person_id)
    assert taken == "taken"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.paused is False
        first_generation = int(route.route_generation)
        conversation_generation_untouched = 1
    async with database.sessions() as session, session.begin():
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    kept = await router.cas_takeover_person(person_id)
    assert kept == "unchanged"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.paused is False
        assert route.presence_id == presence_a
        assert int(route.route_generation) == first_generation
    resolved = await router.resolve_send_for_person(person_id)
    assert resolved.sender_account_id == "8000"
    registry.disconnect(bot_a)
    taken = await router.cas_takeover_person(person_id)
    assert taken == "taken"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.paused is False
        assert route.presence_id == presence_b
        assert int(route.route_generation) == first_generation + 1
    assert conversation_generation_untouched == 1


@pytest.mark.asyncio
async def test_transient_disconnect_preserves_routes_until_same_presence_reconnects(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.identity.db_models import SpaceBindingModel
    from qq_ai_bot.identity.dual_write import ensure_canonical_space_preconfig
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-gen")
    router = PresenceRouter(database, registry, membership_probe=_true)
    monitor = RouteMonitor(router)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        person_id = await _ensure_person_id(session, "1001")
        space_id = await ensure_canonical_space_preconfig(session, "2001")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        binding_id = binding.id
        private_conversation = await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key="bot:8000:private:1001",
            person_id=person_id,
        )
        space_conversation = await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="bot:8000:group:2001",
            space_id=space_id,
        )
    first = registry.connect(bot, presence_id=presence)
    assert await router.cas_takeover_person(person_id) == "taken"
    assert await router.cas_takeover_space(space_id) == "taken"
    assert (
        await router.evaluate_ingest(
            space_binding_id=binding_id,
            event_presence_id=presence,
        )
        == "ok"
    )
    async with database.sessions() as session:
        person_route = await session.get(PersonActiveRouteModel, person_id)
        space_route = await session.get(SpaceActiveRouteModel, space_id)
        ingest_route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert person_route is not None
        assert space_route is not None
        assert ingest_route is not None
        route_state = (
            person_route.identity_binding_id,
            person_route.presence_id,
            int(person_route.route_generation),
            int(person_route.revision),
            space_route.space_binding_id,
            space_route.presence_id,
            int(space_route.route_generation),
            int(space_route.revision),
            ingest_route.ingest_presence_id,
            int(ingest_route.route_generation),
            int(ingest_route.revision),
        )
        conversations = (
            await session.get(CanonicalConversationModel, private_conversation.conversation_id),
            await session.get(CanonicalConversationModel, space_conversation.conversation_id),
        )
        assert conversations[0] is not None
        assert conversations[1] is not None
        conversation_generations = tuple(int(row.generation) for row in conversations)

    registry.disconnect(bot)
    await monitor.on_connection_change()
    with pytest.raises(RouteSendError) as person_error:
        await router.resolve_send_for_person(person_id)
    assert person_error.value.category == "disconnected"
    with pytest.raises(RouteSendError) as space_error:
        await router.resolve_send_for_space(space_id)
    assert space_error.value.category == "disconnected"
    assert (
        await router.evaluate_ingest(
            space_binding_id=binding_id,
            event_presence_id=presence,
        )
        == "not_ingest"
    )

    reconnected = _Bot("8000")
    second = registry.connect(reconnected, presence_id=presence)
    assert second.generation == first.generation + 1
    await monitor.on_connection_change()
    assert (await router.resolve_send_for_person(person_id)).presence_id == presence
    assert (await router.resolve_send_for_space(space_id)).presence_id == presence
    assert (
        await router.evaluate_ingest(
            space_binding_id=binding_id,
            event_presence_id=presence,
        )
        == "ok"
    )

    async with database.sessions() as session:
        person_route = await session.get(PersonActiveRouteModel, person_id)
        space_route = await session.get(SpaceActiveRouteModel, space_id)
        ingest_route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert person_route is not None
        assert space_route is not None
        assert ingest_route is not None
        assert person_route.paused is False
        assert space_route.paused is False
        assert ingest_route.paused is False
        assert (
            person_route.identity_binding_id,
            person_route.presence_id,
            int(person_route.route_generation),
            int(person_route.revision),
            space_route.space_binding_id,
            space_route.presence_id,
            int(space_route.route_generation),
            int(space_route.revision),
            ingest_route.ingest_presence_id,
            int(ingest_route.route_generation),
            int(ingest_route.revision),
        ) == route_state
        conversations = (
            await session.get(CanonicalConversationModel, private_conversation.conversation_id),
            await session.get(CanonicalConversationModel, space_conversation.conversation_id),
        )
        assert conversations[0] is not None
        assert conversations[1] is not None
        assert tuple(int(row.generation) for row in conversations) == conversation_generations


@pytest.mark.asyncio
async def test_space_takeover_zero_one_many_and_membership_probe(database: Database) -> None:
    from qq_ai_bot.identity.dual_write import ensure_canonical_space_preconfig as ensure_v2_space

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    calls: list[str] = []

    async def _probe(bot: object, group_id: str, user_id: str) -> bool:
        calls.append(f"{getattr(bot, 'self_id', '')}:{group_id}:{user_id}")
        return getattr(bot, "self_id", "") == "8000"

    registry = napcat_registry(gateway_instance_id="gw-space")
    router = PresenceRouter(database, registry, membership_probe=_probe)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        space_id = await ensure_v2_space(session, "2001")
    empty = await router.cas_takeover_space(space_id)
    assert empty == "none"
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    taken = await router.cas_takeover_space(space_id)
    assert taken == "taken"
    assert calls
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        first_generation = int(route.route_generation)
    async with database.sessions() as session, session.begin():
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    # bot_b fails the membership probe, so the unique live member stays bot_a.
    same = await router.cas_takeover_space(space_id)
    assert same == "unchanged"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        assert route.paused is False
        assert int(route.route_generation) == first_generation

    async def _all_members(*_args: object, **_kwargs: object) -> bool:
        return True

    both = PresenceRouter(database, registry, membership_probe=_all_members)
    kept = await both.cas_takeover_space(space_id)
    assert kept == "unchanged"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        assert route.paused is False
        assert int(route.route_generation) == first_generation
    registry.disconnect(bot_a)
    taken = await both.cas_takeover_space(space_id)
    assert taken == "taken"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        assert route.paused is False
        assert route.presence_id == presence_b
        assert int(route.route_generation) == first_generation + 1


@pytest.mark.asyncio
async def test_authoritative_ingest_survives_second_presence(database: Database) -> None:
    from qq_ai_bot.identity.db_models import SpaceBindingModel
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_presence_preconfig as ensure_v2_presence,
    )
    from qq_ai_bot.identity.dual_write import (
        ensure_v2_space,
    )

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-ingest")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        space_id = await ensure_v2_space(session, "2001")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        binding_id = binding.id
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_a)
    assert first == "ok"
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        generation = int(route.route_generation)
        revision = int(route.revision)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    second = await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_b)
    assert second == "not_ingest"
    still = await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_a)
    assert still == "ok"
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        assert route.paused is False
        assert route.ingest_presence_id == presence_a
        assert int(route.route_generation) == generation
        assert int(route.revision) == revision
    registry.disconnect(bot_a)
    taken = await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_b)
    assert taken == "ok"
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        assert route.paused is False
        assert route.ingest_presence_id == presence_b
        assert int(route.route_generation) == generation + 1


@pytest.mark.asyncio
async def test_reconcile_paused_is_idempotent_and_keeps_explicit_pause(
    database: Database,
) -> None:
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_presence_preconfig as ensure_v2_presence,
    )
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-pause")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    extra = _Bot("8001")
    other = _Bot("8002")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        person_id = await _ensure_person_id(session, "1001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person_id) == "taken"
    async with database.sessions() as session, session.begin():
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        route.paused = True
        generation = int(route.route_generation)
        revision = int(route.revision)
    first = await router.reconcile_person(person_id)
    second = await router.reconcile_person(person_id)
    assert first == "paused"
    assert second == "paused"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.paused is True
        assert int(route.route_generation) == generation
        assert int(route.revision) == revision
    registry.disconnect(bot)
    async with database.sessions() as session, session.begin():
        presence_b = await ensure_v2_presence(session, "8001")
        presence_c = await ensure_v2_presence(session, "8002")
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        route.paused = False
    registry.connect(extra)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.connect(other)
    registry.bind_presence(platform="qq", external_account_id="8002", presence_id=presence_c)
    paused = await router.cas_takeover_person(person_id)
    assert paused == "ambiguous"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        after = int(route.route_generation)
        after_rev = int(route.revision)
        assert route.paused is True
        assert after == generation + 1
    again = await router.reconcile_person(person_id)
    assert again == "paused"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert int(route.route_generation) == after
        assert int(route.revision) == after_rev


@pytest.mark.asyncio
async def test_ingest_eligible_does_not_block_person_or_space_send(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import PresenceModel
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_presence_preconfig as ensure_v2_presence,
    )
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_space_preconfig as ensure_v2_space,
    )
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-elig")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        person_id = await _ensure_person_id(session, "1001")
        space_id = await ensure_v2_space(session, "2001")
        row = await session.get(PresenceModel, presence)
        assert row is not None
        row.ingest_eligible = False
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    person = await router.resolve_send_for_person(person_id)
    space = await router.resolve_send_for_space(space_id)
    assert person.sender_account_id == "8000"
    assert space.sender_account_id == "8000"
    assert person.kind == "person"
    assert space.kind == "space"


def _hold_pair() -> tuple[asyncio.Event, asyncio.Event, object]:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    return entered, release, hold


@pytest.mark.asyncio
async def test_person_takeover_cas_does_not_overwrite_concurrent_write(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-cas-person")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        presence_c = await ensure_v2_presence(session, "8002")
        person_id = await _ensure_person_id(session, "1001")
        assert (
            await session.scalar(
                select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
            )
            is not None
        )
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person_id) == "taken"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        first_generation = int(route.route_generation)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    entered, release, hold = _hold_pair()
    router._cas_hold = hold
    task = asyncio.create_task(router.cas_takeover_person(person_id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    async with database.immediate_session() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        route.presence_id = presence_c
        route.paused = True
        route.route_generation = first_generation + 1
        route.revision += 1
    release.set()
    assert await task == "conflict"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.presence_id == presence_c
        assert route.paused is True
        assert int(route.route_generation) == first_generation + 1
    router._cas_hold = None
    assert await router.reconcile_person(person_id) == "paused"
    async with database.sessions() as session:
        route = await session.get(PersonActiveRouteModel, person_id)
        assert route is not None
        assert route.presence_id == presence_c
        assert int(route.route_generation) == first_generation + 1


@pytest.mark.asyncio
async def test_space_takeover_cas_does_not_overwrite_concurrent_write(
    database: Database,
) -> None:
    from qq_ai_bot.identity.dual_write import ensure_canonical_space_preconfig as ensure_v2_space

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-cas-space")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        presence_c = await ensure_v2_presence(session, "8002")
        space_id = await ensure_v2_space(session, "2001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_space(space_id) == "taken"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        first_generation = int(route.route_generation)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    entered, release, hold = _hold_pair()
    router._cas_hold = hold
    task = asyncio.create_task(router.cas_takeover_space(space_id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    async with database.immediate_session() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        route.presence_id = presence_c
        route.paused = True
        route.route_generation = first_generation + 1
        route.revision += 1
    release.set()
    assert await task == "conflict"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        assert route.presence_id == presence_c
        assert route.paused is True
        assert int(route.route_generation) == first_generation + 1
    router._cas_hold = None
    assert await router.reconcile_space(space_id) == "paused"
    async with database.sessions() as session:
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert route is not None
        assert int(route.route_generation) == first_generation + 1


@pytest.mark.asyncio
async def test_ingest_provision_cas_does_not_overwrite_concurrent_write(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import SpaceBindingModel
    from qq_ai_bot.identity.dual_write import ensure_canonical_space_preconfig as ensure_v2_space

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-cas-ingest")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        presence_c = await ensure_v2_presence(session, "8002")
        space_id = await ensure_v2_space(session, "2001")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        binding_id = binding.id
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert (
        await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_a)
        == "ok"
    )
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        first_generation = int(route.route_generation)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    entered, release, hold = _hold_pair()
    router._cas_hold = hold
    task = asyncio.create_task(
        router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_b)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    async with database.immediate_session() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        route.ingest_presence_id = presence_c
        route.paused = True
        route.route_generation = first_generation + 1
        route.revision += 1
    release.set()
    assert await task == "paused"
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        assert route.ingest_presence_id == presence_c
        assert route.paused is True
        assert int(route.route_generation) == first_generation + 1
    router._cas_hold = None
    assert (
        await router.evaluate_ingest(space_binding_id=binding_id, event_presence_id=presence_b)
        == "paused"
    )
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        assert route is not None
        assert route.ingest_presence_id == presence_c
        assert int(route.route_generation) == first_generation + 1
