"""Current identity and overrides share one real SQLite read view, without caching."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel


async def _override(database, owner, value, scope="user"):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            RuntimeConfigOverrideModel(
                config_key="context.compaction_window_tokens",
                scope_type=scope,
                canonical_person_id=owner if scope == "user" else None,
                canonical_space_id=owner if scope == "group" else None,
                value_json=str(value),
                value_type="integer",
                apply_mode="hot",
                version=1,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-test",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "person", "space", "both", "canonical", "memory"])
async def test_snapshot_matches_previous_complete_config_and_resolves_once(
    database, monkeypatch, scope
):
    from qq_ai_bot.admin import config_service as module

    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    person, space = await runtime._owner_match(user_id="10001", group_id="2001")
    await _override(database, person, 70000)
    await _override(database, space, 80000, "group")
    kwargs = {
        "global": {},
        "person": {"user_id": "10001"},
        "space": {"group_id": "2001"},
        "both": {"user_id": "10001", "group_id": "2001"},
        "canonical": {"user_id": person, "group_id": space},
        "memory": {"memory_scope": MemoryConfigScope(person, space)},
    }[scope]

    async def previous(*, user_id, group_id):
        records = await runtime._repository.list_relevant(user_id=user_id, group_id=group_id)
        owners = await runtime._owner_match(user_id=user_id, group_id=group_id)
        return records, *owners

    with monkeypatch.context() as baseline:
        baseline.setattr(runtime._repository, "read_relevant_snapshot", previous)
        expected = await runtime.snapshot(**kwargs)
    sessions, sql, resolved = [], [], []
    original_sessions = database.sessions

    def factory(*args, **kw):
        sessions.append(1)
        return original_sessions(*args, **kw)

    def capture(_connection, _cursor, statement, *_args):
        sql.append(statement)

    for name in ("resolve_live_person_id", "resolve_live_space_id"):
        original = getattr(module, name)

        async def wrapped(*args, _original=original, _name=name, **kw):
            resolved.append(_name)
            return await _original(*args, **kw)

        monkeypatch.setattr(module, name, wrapped)
    monkeypatch.setattr(database, "sessions", factory)
    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        actual = await runtime.snapshot(**kwargs)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert actual == expected
    assert len(sessions) == 1
    assert all(statement.lstrip().startswith(("BEGIN", "SELECT")) for statement in sql)
    assert sql[0] == "BEGIN"
    assert resolved.count("resolve_live_person_id") == int("user_id" in kwargs)
    assert resolved.count("resolve_live_space_id") == int("group_id" in kwargs)
    if scope == "space":
        assert sum(statement.startswith("SELECT") for statement in sql) == 8


@pytest.mark.asyncio
async def test_rebind_and_override_commit_between_queries_cannot_mix_snapshot_owners(
    database, monkeypatch
):
    from qq_ai_bot.admin import config_service as module

    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    old, _ = await runtime._owner_match(user_id="10001", group_id=None)
    new, _ = await runtime._owner_match(user_id="10002", group_id=None)
    await _override(database, old, 70000)
    await _override(database, new, 80000)
    resolve = module.resolve_live_person_id
    changed = False

    async def concurrent_rebind(session, raw):
        nonlocal changed
        owner = await resolve(session, raw)
        if not changed:
            changed = True
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(IdentityBindingModel)
                    .where(IdentityBindingModel.external_account_id == "10001")
                    .values(person_id=new, revision=IdentityBindingModel.revision + 1)
                )
                await writer.execute(
                    update(RuntimeConfigOverrideModel)
                    .where(RuntimeConfigOverrideModel.canonical_person_id == old)
                    .values(value_json="71000", version=RuntimeConfigOverrideModel.version + 1)
                )
        return owner

    monkeypatch.setattr(module, "resolve_live_person_id", concurrent_rebind)
    assert (await runtime.snapshot(user_id="10001")).context.compaction_window_tokens == 70000
    assert (await runtime.snapshot(user_id="10001")).context.compaction_window_tokens == 80000


@pytest.mark.asyncio
async def test_snapshot_under_wal_writer_reads_committed_config_without_dml(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    owner, _ = await runtime._owner_match(user_id="10001", group_id=None)
    await _override(database, owner, 70000)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(RuntimeConfigOverrideModel)
            .where(RuntimeConfigOverrideModel.canonical_person_id == owner)
            .values(value_json="80000")
        )
        sql = []

        def capture(_connection, _cursor, statement, *_args):
            sql.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            result = await asyncio.wait_for(runtime.snapshot(user_id="10001"), timeout=1)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert result.context.compaction_window_tokens == 70000
        assert all(statement.startswith(("SELECT", "BEGIN")) for statement in sql)
    assert (await runtime.snapshot(user_id="10001")).context.compaction_window_tokens == 80000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["person_disabled", "identity_disabled", "space_disabled", "missing", "presence_kind"],
)
async def test_snapshot_preserves_live_identity_rejections(database, invalid):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    kwargs = {"user_id": "10001"}
    async with database.immediate_session() as session:
        if invalid == "person_disabled":
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == "10001"
                )
            )
            await session.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id == binding.person_id)
                .values(enabled=False)
            )
        elif invalid == "identity_disabled":
            await session.execute(
                update(IdentityBindingModel)
                .where(IdentityBindingModel.external_account_id == "10001")
                .values(status="disabled")
            )
        elif invalid == "space_disabled":
            await session.execute(
                update(SpaceBindingModel)
                .where(SpaceBindingModel.external_space_id == "2001")
                .values(status="disabled")
            )
            kwargs = {"group_id": "2001"}
        elif invalid == "missing":
            kwargs = {"user_id": "no-such-synthetic-owner"}
        else:
            kwargs = {"user_id": "8000"}
    with pytest.raises(CanonicalIdentityError):
        await runtime.snapshot(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "group"])
async def test_scoped_null_owner_override_is_rejected_by_current_schema(database, scope):
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with database.immediate_session() as writer:
            writer.add(
                RuntimeConfigOverrideModel(
                    config_key="context.compaction_window_tokens",
                    scope_type=scope,
                    value_json="70000",
                    value_type="integer",
                    apply_mode="hot",
                    version=1,
                    created_at=now,
                    updated_at=now,
                    updated_by="synthetic-test",
                )
            )
            await writer.flush()
