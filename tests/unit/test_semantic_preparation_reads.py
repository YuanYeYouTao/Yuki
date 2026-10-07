"""Real idle tick queries and policy refresh after external observation."""

import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from tests.conftest import make_settings
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.semantic_participation import SemanticParticipationService


async def _policy_override(database, enabled):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            RuntimeConfigOverrideModel(
                config_key="conversation.semantic_participation_enabled",
                scope_type="global",
                value_json="true" if enabled else "false",
                value_type="boolean",
                apply_mode="hot",
                version=1,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-test",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_count", [1, 2, 4])
async def test_warm_idle_tick_reuses_only_current_preparation_policy(
    database, tmp_path, monkeypatch, scope_count
):
    host, _ = await _host(database, tmp_path, observer=False)
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    host.app.runtime_config = runtime
    ledger = EventLedgerRepository(database)
    sql, sessions = [], []
    original_sessions = database.sessions
    original_binding = SemanticParticipationService._binding
    original_read = runtime._repository.read_relevant_snapshot

    async def previous_snapshot(*, user_id, group_id):
        rows = await runtime._repository.list_relevant(user_id=user_id, group_id=group_id)
        owners = await runtime._owner_match(user_id=user_id, group_id=group_id)
        return rows, *owners

    async def previous_binding(self, item, **_kwargs):
        return await original_binding(self, item)

    def factory(*args, **kwargs):
        sessions.append(1)
        return original_sessions(*args, **kwargs)

    def capture(_connection, _cursor, statement, *_args):
        sql.append(statement)

    async def measured_tick():
        sessions.clear()
        sql.clear()
        host._last_discovery_at = time.time()
        await host.tick()
        assert host._failures == 0
        assert all(statement.startswith(("SELECT", "BEGIN")) for statement in sql)
        return len(sessions), sum(statement.startswith("SELECT") for statement in sql)

    try:
        for group in ("2001", "2999", "3001", "3002")[:scope_count]:
            row = await _event_and_route(database, ledger, group=group)
            await _item(host, row)
        host._last_discovery_at = time.time()
        await host.tick()
        monkeypatch.setattr(database, "sessions", factory)
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        with monkeypatch.context() as baseline:
            baseline.setattr(runtime._repository, "read_relevant_snapshot", previous_snapshot)
            baseline.setattr(SemanticParticipationService, "_binding", previous_binding)
            before = await measured_tick()
        assert runtime._repository.read_relevant_snapshot == original_read
        after = await measured_tick()
        assert before == (13 * scope_count + 3, 43 * scope_count + 4)
        assert after == (10 * scope_count + 3, 21 * scope_count + 4)
        assert len(host._sessions) == scope_count
        assert all(item.controller.state.now > 0 for item in host._sessions.values())
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        await host._store.close()


@pytest.mark.asyncio
async def test_observer_await_reloads_current_policy_before_controller_advancement(
    database, tmp_path, monkeypatch
):
    from sqlalchemy import update

    host, _ = await _host(database, tmp_path, observer=True)
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    host.app.runtime_config = runtime
    await _policy_override(database, True)
    row = await _event_and_route(database, host.app.ledger)
    item = await _item(host, row)
    snapshots = []
    original_snapshot = runtime.snapshot

    async def snapshot(**kwargs):
        result = await original_snapshot(**kwargs)
        snapshots.append(result.conversation.semantic_participation_enabled)
        return result

    async def observe_then_disable(*_args, **_kwargs):
        async with database.immediate_session() as writer:
            await writer.execute(
                update(RuntimeConfigOverrideModel)
                .where(
                    RuntimeConfigOverrideModel.config_key
                    == "conversation.semantic_participation_enabled"
                )
                .values(value_json="false", version=2)
            )

    monkeypatch.setattr(runtime, "snapshot", snapshot)
    monkeypatch.setattr(item.observation, "evaluate_due", observe_then_disable)
    try:
        await host._advance_scene(item)
        assert snapshots == [True, False]
        binding = await host.repository.get_binding(
            item.scene.conversation_id, item.scene.generation
        )
        assert not binding.external_enabled
        assert item.controller.state.now >= item.controller.state.last_human_at
    finally:
        await host._store.close()


@pytest.mark.asyncio
async def test_generation_change_still_rejects_old_resident_before_policy_reuse(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    row = await _event_and_route(database, host.app.ledger)
    item = await _item(host, row)
    async with database.immediate_session() as writer:
        await bump_canonical_generation(writer, item.scene.conversation_id, event_id=row.id)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stale generation cannot enter hydration or binding")

    monkeypatch.setattr(host, "_hydrate", forbidden)
    monkeypatch.setattr(host, "_binding", forbidden)
    try:
        await host._advance_scene(item)
    finally:
        await host._store.close()
