"""Periodic plugin cleanup has bounded pages and rechecks renewed or closed rows."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_storage_writer_boundaries import session_repository, sql_capture

from qq_ai_bot.plugin_host.db_models import PluginAgentSessionModel, PluginStateModel
from qq_ai_bot.plugin_host.repository import PluginStateRepository


@pytest.mark.parametrize("kind", ["state", "session_expiry", "session_status"])
async def test_periodic_cleanup_empty_writer_backlog_and_changed_eligibility(
    database, monkeypatch, kind
):
    sessions = await session_repository(database)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    old = now - timedelta(days=1)
    if kind == "state":
        model = PluginStateModel
        primary_key = model.id
        repository = PluginStateRepository(database)
        rows = [
            model(
                plugin_id="metadata.session",
                namespace="cleanup",
                key=f"key-{i:03}",
                value_json="{}",
                updated_at=old,
                expires_at=old,
            )
            for i in range(129)
        ]
        changed_values = {"expires_at": now + timedelta(days=1)}

        async def cleanup():
            return await repository.cleanup_expired(now=now)

    else:
        model = PluginAgentSessionModel
        primary_key = model.session_id
        rows = [
            model(
                session_id=f"cleanup-{i:03}",
                plugin_id="metadata.session",
                scope_type="plugin",
                instructions="test",
                created_at=old,
                updated_at=old,
                last_active_at=old,
                expires_at=old,
            )
            for i in range(129)
        ]
        changed_values = (
            {"expires_at": now + timedelta(days=1)}
            if kind == "session_expiry"
            else {"status": "closed"}
        )

        async def cleanup():
            return await sessions.expire_due(now=now)

    # Real competing writer: an empty maintenance round must not wait for it.
    async with database.immediate_session():
        with sql_capture(database) as statements:
            assert await asyncio.wait_for(cleanup(), timeout=1) == 0
        assert not any(
            statement.startswith(("BEGIN IMMEDIATE", "UPDATE", "INSERT", "DELETE"))
            for statement in statements
        )
    async with database.immediate_session() as writer:
        writer.add_all(rows)
        await writer.flush()
        protected_key = rows[0].id if kind == "state" else rows[0].session_id
    original = database.immediate_session
    changed = False

    @asynccontextmanager
    async def changed_before_writer():
        nonlocal changed
        if not changed:
            changed = True
            async with original() as writer:
                await writer.execute(
                    update(model).where(primary_key == protected_key).values(**changed_values)
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", changed_before_writer)
    assert await cleanup() == 127
    assert await cleanup() == 1
    assert await cleanup() == 0
    async with database.sessions() as reader:
        retained = await reader.get(model, protected_key)
        assert retained is not None
        if kind == "state":
            assert await reader.scalar(select(func.count()).select_from(model)) == 1
        else:
            assert retained.status == ("closed" if kind == "session_status" else "active")
            assert (
                await reader.scalar(
                    select(func.count()).select_from(model).where(model.status == "expired")
                )
                == 128
            )
