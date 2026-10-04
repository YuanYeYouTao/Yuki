"""Real WAL source checks do not reserve the writer or confer mutation authority."""

import asyncio
from contextvars import ContextVar

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text, update
from tests.unit import test_chat_preparation_timings as chat_fixture
from tests.unit.test_semantic_participation_host import _event_and_route
from tests.unit.test_work_source_guard import _guard

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import scope
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


async def test_guard_finishes_while_another_wal_writer_is_held(database, monkeypatch):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    commits = []
    dialect = database.engine.sync_engine.dialect
    original_commit = dialect.do_commit

    def commit(connection):
        commits.append(True)
        return original_commit(connection)

    monkeypatch.setattr(dialect, "do_commit", commit)
    statements = []

    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)

    async with database.sessions() as held:
        await held.execute(text("BEGIN IMMEDIATE"))
        sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            assert await asyncio.wait_for(guard.check(control), timeout=1)
        finally:
            sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
            await held.rollback()
    assert statements.count("BEGIN") == 2
    assert all(s.startswith(("BEGIN", "SELECT")) for s in statements)
    assert not commits
    assert guard.fingerprint is not None


@pytest.mark.parametrize("change", ["owner", "fence", "generation", "cancel", "expired"])
async def test_second_snapshot_checks_original_lease_at_sql_time(database, monkeypatch, change):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    original = control.repository._assert_lease_readonly
    changes = {
        "owner": {"owner": "replacement"},
        "fence": {"fence": control.lease.fence + 1},
        "generation": {"generation": control.lease.generation + 1},
        "cancel": {"cancel_epoch": control.lease.cancel_epoch + 1},
        "expired": {"lease_until": 0},
    }

    async def replace_before_lease_read(session, lease):
        async with database.immediate_session() as writer:
            await writer.execute(
                update(scope)
                .where(scope.c.conversation_id == lease.conversation_id)
                .values(**changes[change])
            )
        await original(session, lease)

    monkeypatch.setattr(control.repository, "_assert_lease_readonly", replace_before_lease_read)
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await guard.check(control)
    assert guard.fingerprint is None
    assert control.session.source_revision == 0


@pytest.mark.parametrize("change", ["metadata", "privacy"])
async def test_second_snapshot_rechecks_source_dependencies(database, monkeypatch, change):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    original = control.repository._assert_lease_readonly

    async def change_before_lease_read(session, lease):
        async with database.immediate_session() as writer:
            if change == "metadata":
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(sender_nickname="changed source")
                )
            else:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        await original(session, lease)

    monkeypatch.setattr(control.repository, "_assert_lease_readonly", change_before_lease_read)
    assert not await guard.check(control)
    assert guard.fingerprint is None


async def test_read_snapshot_is_coherent_and_real_work_mutation_rechecks_lease(
    database, monkeypatch
):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    work = await control.repository.accept(
        control.lease, source_key="readonly-guard", source={}, goal="retain original writer fence"
    )
    original = control.repository._assert_lease_readonly

    async def replace_after_snapshot_is_established(session, lease):
        await original(session, lease)
        # A read transaction keeps a coherent snapshot, even when another WAL
        # writer commits after its first SELECT. It does not block cancellation.
        async with database.immediate_session() as writer:
            await writer.execute(
                update(scope)
                .where(scope.c.conversation_id == lease.conversation_id)
                .values(owner="replacement", fence=scope.c.fence + 1)
            )
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == lease.conversation_id)
                .values(
                    prompt_source_revision=CanonicalConversationModel.prompt_source_revision + 1
                )
            )

    monkeypatch.setattr(
        control.repository, "_assert_lease_readonly", replace_after_snapshot_is_established
    )
    assert await guard.check(control)
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await control.repository.checkpoint(control.lease, work["id"], {"forbidden": True})
    retained = await control.repository.get(work["id"])
    assert retained is not None
    assert '"forbidden"' not in retained["checkpoint_json"]


async def test_ordinary_no_work_guard_checks_never_write_before_provider(
    database, tmp_path, monkeypatch
):
    active_check = ContextVar("readonly_guard_check", default=None)
    checks = []
    before_providers = []
    make_settings = chat_fixture.make_settings
    monkeypatch.setattr(
        chat_fixture,
        "make_settings",
        lambda *args, **kwargs: make_settings(*args, **{**kwargs, "runtime_work_enabled": True}),
    )
    original_check = WorkSourceGuard.check

    async def check(self, control, **kwargs):
        row = {"has_work": control.current is not None, "sql": [], "commits": 0}
        token = active_check.set(row)
        try:
            return await original_check(self, control, **kwargs)
        finally:
            checks.append(row)
            active_check.reset(token)

    monkeypatch.setattr(WorkSourceGuard, "check", check)
    original_append = TraceRecorder.append

    async def append(self, trace_scope, kind, payload, **kwargs):
        if kind == "provider_start":
            before_providers.append(len(checks))
        await original_append(self, trace_scope, kind, payload, **kwargs)

    monkeypatch.setattr(TraceRecorder, "append", append)
    dialect = database.engine.sync_engine.dialect
    original_commit = dialect.do_commit

    def commit(connection):
        row = active_check.get()
        if row is not None:
            row["commits"] += 1
        return original_commit(connection)

    monkeypatch.setattr(dialect, "do_commit", commit)

    def record(_connection, _cursor, statement, *_args):
        row = active_check.get()
        if row is not None:
            row["sql"].append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        await chat_fixture.test_real_chat_preparation_uses_root_ids_and_preserves_business_flow(
            database, tmp_path, monkeypatch, "ready"
        )
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert len(checks) == 5
    assert before_providers and before_providers[0] == 3
    assert not any(row["has_work"] or row["commits"] for row in checks)
    assert all(
        statement.startswith(("SELECT", "BEGIN")) for row in checks for statement in row["sql"]
    )
