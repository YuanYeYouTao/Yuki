"""Real SQLite/ASGI gateway facts and narrowly scoped work diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_participation_latency_health_runtime import (
    HEALTH_TOKEN,
    build_real_container,
)
from tests.unit.test_gateway_providers import _Bot, _Provider

import yuki.main as main
from yuki.admin_api.http import attach_admin_api
from yuki.gateway.health import platform_health
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    replace_assignment,
)
from yuki.identity.db_models import AccountAssignmentModel, PresenceModel


async def configured_gateway(database, tmp_path, monkeypatch):
    app, _, _ = await build_real_container(database, tmp_path, monkeypatch, False)
    providers = GatewayProviderCatalog(
        _Provider(name, platform=name) for name in ("aaa", "beta", "owner", "zeta")
    )
    registry = GatewayConnectionRegistry(providers=providers)
    now = datetime.now(UTC)
    try:
        async with database.immediate_session() as session:
            alpha = await create_presence(
                session, external_account_id="alpha", platform="aaa", now=now
            )
            disabled = await create_presence(
                session, external_account_id="disabled", platform="disabled", now=now
            )
            disabled.enabled = False
            history = await create_presence(
                session, external_account_id="historical", platform="historical", now=now
            )
            await replace_assignment(session, history.account, history.id, None, now=now)
            recycled = await create_presence(
                session, external_account_id="recycled", platform="aaa", now=now
            )
            await replace_assignment(session, recycled.account, recycled.id, None, now=now)
            again = await replace_assignment(session, recycled.account, None, "yuki", now=now)
            await replace_assignment(session, recycled.account, again.id, None, now=now)
            current = await replace_assignment(session, recycled.account, None, "yuki", now=now)
            zeta = await create_presence(
                session, external_account_id="zeta", platform="zeta", now=now
            )
            zeta.enabled = False
            owned = await create_presence(
                session, external_account_id="owner", platform="owner", now=now
            )
            await create_person_binding(
                session,
                external_account_id="person",
                display_name="synthetic",
                platform="persononly",
                now=now,
            )
            ids = {"alpha": alpha.id, "zeta": zeta.id, "owner": owned.id, "current": current.id}
        handles = {name: _Bot(name) for name in ("alpha", "beta", "owner", "zeta")}
        registry.connect(handles["alpha"], provider_id="aaa", presence_id=ids["alpha"])
        registry.connect(handles["beta"], provider_id="beta")
        registry.connect(handles["owner"], provider_id="owner", presence_id=ids["owner"])
        owner_facts = {"enabled": False, "running": False, "healthy": None}

        async def owner_health():
            return owner_facts

        app.gateway_providers = providers
        app.gateway_registry = registry
        app.gateway_health_sources = {"owner": owner_health}
        return app, providers, registry, ids, handles, owner_facts
    except BaseException:
        await app.close()
        raise


def http_client(app, monkeypatch):
    http_app = FastAPI()
    monkeypatch.setattr(main, "get_container", lambda: app)
    main.register_health_routes(http_app)
    attach_admin_api(http_app, lambda: app.admin_api)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app, raise_app_exceptions=False),
        base_url="http://gateway-health-test",
    )


async def test_gateway_health_real_asgi_preserves_presence_and_owner_semantics(
    database, tmp_path, monkeypatch, latency_evidence
):
    app, providers, registry, ids, _, owner = await configured_gateway(
        database, tmp_path, monkeypatch
    )
    try:
        facts = await platform_health(database, providers, registry, app.gateway_health_sources)
        assert "persononly" not in facts
        assert facts["aaa"].summary() == {"enabled": True, "running": True, "healthy": True}
        assert facts["aaa"].connected is True
        for name in ("historical", "disabled", "zeta"):
            assert facts[name].summary() == {"enabled": False, "running": False, "healthy": None}
        assert facts["beta"].summary() == {"enabled": False, "running": False, "healthy": None}
        assert facts["beta"].connected is True  # A live handle has no bound Presence.
        assert facts["owner"].summary() == owner and facts["owner"].connected is True
        async with http_client(app, monkeypatch) as client:
            assert (await client.get("/admin/v1/health")).status_code == 401
            public = await client.get("/healthz")
            admin = await client.get(
                "/admin/v1/health", headers={"Authorization": f"Bearer {HEALTH_TOKEN}"}
            )
            assert public.status_code == admin.status_code == 200
            admin_rows = {row["name"]: row for row in admin.json()["data"]}
            public_rows = public.json()["gateways"]
            for name, value in facts.items():
                assert public_rows[name] == value.summary()
                assert {
                    key: admin_rows[name][key] for key in ("enabled", "running", "healthy")
                } == value.summary()
            assert "persononly" not in public_rows
        latency_evidence.update(
            boundary="actual ASGI public/admin, SQLite and gateway registry; synthetic handles",
            historical_and_disabled_preserved=True,
            person_assignment_excluded=True,
            live_unbound_connection_is_connected=True,
            owner_precedence_preserved=True,
            authorized_admin_and_unauthenticated_denial=True,
            original_current_presence_preserved=bool(ids["current"]),
        )
    finally:
        await app.close()


async def test_gateway_health_keeps_original_read_and_connection_order_across_await(
    database, tmp_path, monkeypatch, latency_evidence
):
    app, providers, registry, ids, handles, _ = await configured_gateway(
        database, tmp_path, monkeypatch
    )
    gate, entered = asyncio.Event(), asyncio.Event()
    calls = []
    original_connected, original_active = registry.connected_presence_ids, registry.has_any_active

    def connected(platform):
        value = original_connected(platform)
        if platform == "aaa":
            calls.append(("connected_before_owner", value))
        return value

    def active(*, platform=None):
        value = original_active(platform=platform)
        if platform == "aaa":
            calls.append(("active_after_owner", value))
        return value

    async def waiting_owner():
        calls.append(("owner_entered", None))
        entered.set()
        await gate.wait()
        return {"enabled": True, "running": True, "healthy": True}

    monkeypatch.setattr(registry, "connected_presence_ids", connected)
    monkeypatch.setattr(registry, "has_any_active", active)
    sources = {"aaa": waiting_owner}
    task = asyncio.create_task(platform_health(database, providers, registry, sources))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert calls == [("connected_before_owner", (ids["alpha"],)), ("owner_entered", None)]
        registry.disconnect(handles["alpha"])
        registry.connect(handles["zeta"], provider_id="zeta", presence_id=ids["zeta"])
        async with database.immediate_session() as writer:
            zeta = await writer.get(PresenceModel, ids["zeta"])
            zeta.enabled = True
        gate.set()
        first = await task
        assert calls[2] == ("active_after_owner", False)
        assert first["aaa"].connected is False
        assert first["zeta"].summary() == {"enabled": False, "running": False, "healthy": None}
        assert first["zeta"].connected is True
        later = await platform_health(database, providers, registry, sources)
        assert later["zeta"].summary() == {"enabled": True, "running": True, "healthy": True}
        latency_evidence.update(
            boundary="actual gateway owner await and SQLite mutation with bound registry handles",
            pre_owner_presence_connection_seen=True,
            post_owner_disconnection_seen=True,
            first_request_kept_original_database_snapshot=True,
            next_request_saw_committed_database_and_connection=True,
        )
    finally:
        gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.close()


@pytest.mark.parametrize("requirement", ["narrow_projection", "single_pass"])
async def test_gateway_health_avoids_unconsumed_presence_work(
    database, tmp_path, monkeypatch, latency_evidence, requirement
):
    app, providers, registry, _, _, _ = await configured_gateway(database, tmp_path, monkeypatch)
    statements = []
    presence_property = inspect.getattr_static(AccountAssignmentModel, "platform")
    original_getter = presence_property.fget
    accesses = 0

    def observed_getter(instance):
        nonlocal accesses
        accesses += 1
        return original_getter(instance)

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select ") and "from account_assignments" in normalized:
            statements.append(normalized)

    try:
        async with database.read_session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AccountAssignmentModel)
                .where(AccountAssignmentModel.kind == "yuki")
            )
        monkeypatch.setattr(presence_property, "fget", observed_getter)
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            facts = await platform_health(database, providers, registry, app.gateway_health_sources)
            small_accesses = accesses
            expanded = GatewayProviderCatalog(
                _Provider(name, platform=name)
                for name in (
                    "aaa",
                    "beta",
                    "owner",
                    "zeta",
                    *(f"extra{index}" for index in range(7)),
                )
            )
            expanded_facts = await platform_health(
                database, expanded, registry, app.gateway_health_sources
            )
            expanded_accesses = accesses - small_accesses
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert statements and count > 0 and len(expanded_facts) > len(facts) > 1
        forbidden = (
            "account_assignments.person_id",
            "account_assignments.created_at",
            "account_assignments.updated_at",
            "account_assignments.revision",
            "accounts_1.external_account_id",
            "accounts_1.display_name",
        )
        wide = any(field in statement for statement in statements for field in forbidden)
        latency_evidence.update(
            boundary="actual SQLite Presence query and real gateway registry; diagnostic counter",
            requirement=requirement,
            yuki_presence_rows=count,
            platforms_small=len(facts),
            platforms_expanded=len(expanded_facts),
            presence_platform_property_reads_small=small_accesses,
            presence_platform_property_reads_expanded=expanded_accesses,
            contains_unconsumed_wide_columns=wide,
            selected_statements=statements,
        )
        if requirement == "narrow_projection":
            assert not wide, "gateway health selected unconsumed Presence/Account fields"
        else:
            assert expanded_accesses == small_accesses, (
                "unrelated platforms amplified Presence work"
            )
    finally:
        await app.close()


async def test_gateway_health_presence_read_failure_preserves_http_failure(
    database, tmp_path, monkeypatch, latency_evidence
):
    app, _, _, _, _, _ = await configured_gateway(database, tmp_path, monkeypatch)

    def fail_presence(_connection, _cursor, statement, _parameters, _context, _many):
        if "from account_assignments" in " ".join(statement.lower().split()):
            raise OperationalError(statement, (), RuntimeError("synthetic Presence read failure"))

    try:
        async with http_client(app, monkeypatch) as client:
            event.listen(database.engine.sync_engine, "before_cursor_execute", fail_presence)
            try:
                public = await client.get("/healthz")
                admin = await client.get(
                    "/admin/v1/health", headers={"Authorization": f"Bearer {HEALTH_TOKEN}"}
                )
                unauthorized = await client.get("/admin/v1/health")
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", fail_presence)
            assert public.status_code == admin.status_code == 500
            assert unauthorized.status_code == 401
        latency_evidence.update(
            boundary="actual ASGI routes and SQLite query fault; no external network",
            public_status=public.status_code,
            authorized_admin_status=admin.status_code,
            unauthorized_admin_status=unauthorized.status_code,
        )
    finally:
        await app.close()


@pytest.mark.parametrize("empty_catalog", [False, True])
async def test_gateway_health_missing_account_keeps_original_failure(
    database, tmp_path, monkeypatch, latency_evidence, empty_catalog
):
    app, providers, registry, ids, _, _ = await configured_gateway(database, tmp_path, monkeypatch)
    try:
        # Explicitly inject broken referential data; normal writes enforce the foreign key.
        filename = database.url.removeprefix("sqlite+aiosqlite:///")
        with sqlite3.connect(filename) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM accounts WHERE id=(SELECT account_id FROM account_assignments "
                "WHERE id=?)",
                (ids["alpha"],),
            )
        if empty_catalog:
            # Normal GatewayProviderCatalog construction rejects an empty catalog.
            providers = object.__new__(GatewayProviderCatalog)
            providers._providers = {}
        with pytest.raises(AttributeError):
            await platform_health(database, providers, registry, {})
        latency_evidence.update(
            boundary=(
                "actual SQLite deliberately corrupted by test SQL; normal FK writes prevent it"
            ),
            empty_catalog_injected=empty_catalog,
            original_attribute_error_preserved=True,
        )
    finally:
        await app.close()
