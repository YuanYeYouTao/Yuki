"""Authenticated ingress reuses only an existing local same-Presence pin."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.support.gateway import napcat_registry
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from qq_ai_bot.conversation.canonical_db_models import SpaceBindingIngestRouteModel
from qq_ai_bot.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.identity.db_models import PresenceModel, SpaceBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.persistence.database import Database


async def _forbidden_probe(*args: object, **kwargs: object) -> bool:
    pytest.fail("healthy authenticated ingress must not call a membership API")


@pytest.mark.asyncio
async def test_existing_ingress_pin_uses_one_checkout_and_zero_probe_or_dml(
    database: Database,
) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)  # Exercise automatic binding from a presence=None snapshot.
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, first.space_binding_id)
        assert route is not None
        original = (route.ingest_presence_id, route.route_generation, route.revision)

    registry = napcat_registry(gateway_instance_id="fresh-registry")
    registry.connect(bot)
    assert registry.resolve_by_handle(bot).snapshot.presence_id is None
    resolver = CanonicalIngressResolver(
        database, registry, PresenceRouter(database, registry, membership_probe=_forbidden_probe)
    )

    # A nested checkout times out: the healthy path must reuse the ingress session.
    engine = create_async_engine(database.url, pool_size=1, max_overflow=0, pool_timeout=0.1)
    event.listen(engine.sync_engine, "connect", database._configure_sqlite_connection)
    statements: list[str] = []
    checkouts = 0

    def record_sql(*args: object) -> None:
        statements.append(str(args[2]).strip().upper())

    def record_checkout(*args: object) -> None:
        nonlocal checkouts
        checkouts += 1

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    event.listen(engine.sync_engine, "checkout", record_checkout)
    original_sessions = database.sessions
    database.sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    resolver._router._probe = _forbidden_probe
    try:
        for index in range(2):
            admitted = await asyncio.wait_for(
                resolver.pre_admit(bot, _message(message_id=f"warm-{index}", group_id="2001")),
                2,
            )
            assert admitted is not None and not admitted.dropped
        assert checkouts == 2
        assert not any(
            sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for sql in statements
        )
        assert registry.resolve_by_handle(bot).snapshot.presence_id == first.presence_id
        async with database.sessions() as session:
            route = await session.get(SpaceBindingIngestRouteModel, first.space_binding_id)
            assert route is not None
            assert original == (route.ingest_presence_id, route.route_generation, route.revision)
    finally:
        database.sessions = original_sessions
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_hot_read_rejects_original_connection_changed_during_sql(
    database: Database, reconnect: bool
) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    resolver._router._probe = _forbidden_probe
    changed = False

    def change_connection(*args: object) -> None:
        nonlocal changed
        if not changed and "FROM space_binding_ingest_routes" in str(args[2]):
            changed = True
            registry.disconnect(bot)
            if reconnect:
                registry.connect(bot)

    event.listen(database.engine.sync_engine, "before_cursor_execute", change_connection)
    try:
        admitted = await resolver.pre_admit(bot, _message(message_id="raced", group_id="2001"))
        assert changed and admitted is not None and admitted.dropped
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", change_connection)


@pytest.mark.asyncio
async def test_unhealthy_same_pin_still_runs_original_candidate_recovery(
    database: Database,
) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    async with database.immediate_session() as session:
        presence = await session.get(PresenceModel, first.presence_id)
        assert presence is not None
        presence.ingest_eligible = False
        replacement_id = await ensure_presence(session, "8001")
    replacement = _Bot("8001")
    registry.connect(replacement)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=replacement_id)
    probes: list[object] = []

    async def probe(bot: object, group: str, account: str) -> bool:
        probes.append(bot)
        return True

    resolver._router._probe = probe
    admitted = await resolver.pre_admit(bot, _message(message_id="ineligible", group_id="2001"))
    assert admitted is not None and admitted.dropped and probes == [replacement]
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, first.space_binding_id)
        assert route is not None
        assert route.ingest_presence_id == replacement_id and route.route_generation == 2


@pytest.mark.asyncio
async def test_cancelled_cold_probe_does_not_install_a_route(database: Database) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    entered = asyncio.Event()

    async def probe(bot: object, group: str, account: str) -> bool:
        entered.set()
        await asyncio.Event().wait()
        return True

    resolver._router._probe = probe
    pending = asyncio.create_task(
        resolver.pre_admit(bot, _message(message_id="cancelled", group_id="2001"))
    )
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with database.sessions() as session:
        assert not list(await session.scalars(select(SpaceBindingIngestRouteModel)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["paused", "binding", "disabled", "ineligible", "disconnect", "forged", "account"]
)
async def test_existing_authenticated_pin_rejects_invalid_local_evidence(
    database: Database, changed: str
) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    async with database.immediate_session() as session:
        route = await session.get(SpaceBindingIngestRouteModel, first.space_binding_id)
        presence = await session.get(PresenceModel, first.presence_id)
        binding = await session.get(SpaceBindingModel, first.space_binding_id)
        assert route is not None and presence is not None and binding is not None
        if changed == "paused":
            route.paused = True
        elif changed == "binding":
            binding.status = "disabled"
        elif changed == "disabled":
            presence.enabled = False
        elif changed == "ineligible":
            presence.ingest_eligible = False
        elif changed == "account":
            presence.external_account_id = "different-account"
    if changed == "disconnect":
        registry.disconnect(bot)
    resolver._router._probe = _forbidden_probe
    admitted = await resolver.pre_admit(
        bot,
        _message(
            message_id="invalid",
            group_id="2001",
            bot_user_id="8001" if changed == "forged" else "8000",
        ),
    )
    assert admitted is not None and admitted.dropped


class _LimitedProvider:
    provider_id = "limited"

    def __init__(self, omitted: str) -> None:
        self.omitted = omitted

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform="qq",
            external_account_id=str(getattr(handle, "self_id", "")),
            capabilities=frozenset({"send_group", "group_member_probe"} - {self.omitted}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted", ["send_group", "group_member_probe"])
async def test_existing_pin_still_requires_gateway_capabilities(
    database: Database, omitted: str
) -> None:
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    limited = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog((_LimitedProvider(omitted),))
    )
    limited.connect(bot)
    assert first.presence_id is not None
    limited.bind_presence(platform="qq", external_account_id="8000", presence_id=first.presence_id)
    router = PresenceRouter(database, limited, membership_probe=_forbidden_probe)
    admitted = await CanonicalIngressResolver(database, limited, router).pre_admit(
        bot, _message(message_id="limited", group_id="2001")
    )
    assert admitted is not None and admitted.dropped


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["paused", "transferred"])
async def test_warm_admission_still_rechecks_route_at_ledger_first_write(
    database: Database, changed: str
) -> None:
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped
    resolver._router._probe = _forbidden_probe
    admitted = await resolver.pre_admit(bot, _message(message_id="warm", group_id="2001"))
    assert admitted is not None and not admitted.dropped
    async with database.immediate_session() as session:
        route = await session.get(SpaceBindingIngestRouteModel, admitted.space_binding_id)
        assert route is not None
        if changed == "paused":
            route.paused = True
        else:
            route.ingest_presence_id = await ensure_presence(session, "8001")
    with pytest.raises(CanonicalIdentityError) as failure:
        await uow.append_inbound(admitted.message, admitted)
    assert failure.value.category == ("paused" if changed == "paused" else "not_ingest")


@pytest.mark.asyncio
async def test_cold_ingress_and_disconnected_owner_keep_membership_recovery(
    database: Database,
) -> None:
    registry, resolver, _ = await _stack(database)
    probes: list[object] = []

    async def probe(bot: object, group: str, account: str) -> bool:
        probes.append(bot)
        return True

    resolver._router._probe = probe
    bot = _Bot("8000")
    registry.connect(bot)
    first = await resolver.pre_admit(bot, _message(message_id="cold", group_id="2001"))
    assert first is not None and not first.dropped and probes == [bot]
    registry.disconnect(bot)
    async with database.immediate_session() as session:
        replacement_id = await ensure_presence(session, "8001")
    replacement = _Bot("8001")
    registry.connect(replacement)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=replacement_id)
    probes.clear()
    admitted = await resolver.pre_admit(
        replacement, _message(message_id="recovered", group_id="2001", bot_user_id="8001")
    )
    assert admitted is not None and not admitted.dropped and probes == [replacement]
    async with database.sessions() as session:
        route = await session.get(SpaceBindingIngestRouteModel, first.space_binding_id)
        assert route is not None
        assert route.ingest_presence_id == replacement_id and route.route_generation == 2
