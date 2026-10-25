"""Current identity and overrides share one real SQLite read view, without caching."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from tests.conftest import make_settings

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.memory_config import MemoryConfigScope
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.models import RuntimeConfigOverrideModel


async def _owners(database):
    # Fixture transport bindings are resolved once at this test boundary.
    async with database.read_session() as reader:
        people = {}
        for external in ("10001", "10002"):
            people[external] = await reader.scalar(
                select(IdentityBindingModel.person_id).where(
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.external_account_id == external,
                )
            )
        space = await reader.scalar(
            select(SpaceBindingModel.space_id).where(
                SpaceBindingModel.platform == "qq", SpaceBindingModel.external_space_id == "2001"
            )
        )
        presence = await reader.scalar(select(PresenceModel.id).limit(1))
    return people["10001"], people["10002"], space, presence


async def _override(
    database,
    owner,
    value,
    scope="user",
    *,
    key="context.compact_trigger_tokens",
    value_type="integer",
):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            RuntimeConfigOverrideModel(
                config_key=key,
                scope_type=scope,
                canonical_person_id=owner if scope == "user" else None,
                canonical_space_id=owner if scope == "group" else None,
                value_json=json.dumps(value),
                value_type=value_type,
                apply_mode="hot",
                version=1,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-test",
            )
        )


async def test_zero_person_temperature_and_group_context_keep_original_scopes(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    person, _, space, _ = await _owners(database)
    await _override(database, person, 0.0, key="llm.temperature", value_type="number")
    await _override(database, space, 0.8, "group", key="llm.temperature", value_type="number")
    await _override(database, space, 80000, "group")
    snapshot = await runtime.snapshot(user_id=person, group_id=space)
    assert snapshot.llm.temperature == 0.0 and snapshot.context.compact_trigger_tokens == 80000


@pytest.mark.asyncio
@pytest.mark.parametrize("group_override", [False, True])
async def test_group_history_uses_space_config_while_private_history_keeps_person_config(
    database, group_override
):
    settings = make_settings(
        database.url,
        context_compact_trigger_tokens=60000,
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    person_a, person_b, space, _ = await _owners(database)
    values_a = {
        "compact_trigger_tokens": 100000,
    }
    values_b = {
        "compact_trigger_tokens": 200000,
    }
    space_values = {
        "compact_trigger_tokens": 80000,
    }
    for name in values_a:
        value_type = "number" if name.endswith("ratio") else "integer"
        for owner, value in ((person_a, values_a[name]), (person_b, values_b[name])):
            await _override(database, owner, value, key=f"context.{name}", value_type=value_type)
        if group_override:
            await _override(
                database,
                space,
                space_values[name],
                "group",
                key=f"context.{name}",
                value_type=value_type,
            )
    for owner, temperature in ((person_a, 0.2), (person_b, 0.8)):
        await _override(database, owner, temperature, key="llm.temperature", value_type="number")
    group_a = await runtime.snapshot(user_id=person_a, group_id=space)
    group_b = await runtime.snapshot(user_id=person_b, group_id=space)
    assert group_a.context == group_b.context
    expected = (
        space_values
        if group_override
        else {
            "compact_trigger_tokens": 60000,
        }
    )
    for name, value in expected.items():
        assert getattr(group_a.context, name) == value
    assert group_a.llm.temperature == 0.2 and group_b.llm.temperature == 0.8
    canonical = await runtime.snapshot(memory_scope=MemoryConfigScope(person_b, space))
    assert canonical.context == group_a.context
    private_a = await runtime.snapshot(user_id=person_a)
    private_b = await runtime.snapshot(user_id=person_b)
    for name in values_a:
        assert getattr(private_a.context, name) == values_a[name]
        assert getattr(private_b.context, name) == values_b[name]


@pytest.mark.asyncio
async def test_rebind_and_override_commit_between_queries_cannot_mix_snapshot_owners(
    database, monkeypatch
):
    from yuki.admin import config_service as module

    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    old, new, _, _ = await _owners(database)
    await _override(database, old, 70000)
    await _override(database, new, 80000)
    resolve = module.current_person_id
    changed = False

    async def concurrent_rebind(session, raw):
        nonlocal changed
        owner = await resolve(session, raw)
        if not changed:
            changed = True
            async with database.immediate_session() as writer:
                from yuki.identity.canonical_repository import (
                    find_identity_binding,
                    replace_assignment,
                )

                original = await find_identity_binding(writer, "10001")
                target = await writer.get(CanonicalPersonModel, new)
                await replace_assignment(
                    writer, original.account, original.id, target, now=datetime.now(UTC)
                )
                await writer.execute(
                    update(RuntimeConfigOverrideModel)
                    .where(RuntimeConfigOverrideModel.canonical_person_id == old)
                    .values(value_json="71000", version=RuntimeConfigOverrideModel.version + 1)
                )
        return owner

    monkeypatch.setattr(module, "current_person_id", concurrent_rebind)
    snapshot = await runtime.snapshot(user_id=old)
    assert changed and snapshot.context.compact_trigger_tokens == 70000
    assert (await runtime.snapshot(user_id=old)).context.compact_trigger_tokens == 71000
    assert (await runtime.snapshot(user_id=new)).context.compact_trigger_tokens == 80000
    with pytest.raises(CanonicalIdentityError):
        await runtime.snapshot(user_id="10001")


@pytest.mark.asyncio
async def test_snapshot_under_wal_writer_reads_committed_config_without_dml(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    owner, _, _, _ = await _owners(database)
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
            result = await runtime.snapshot(user_id=owner)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert all(statement.startswith(("SELECT", "BEGIN")) for statement in sql)
        assert result.context.compact_trigger_tokens == 70000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["person_disabled", "space_disabled", "missing", "presence_kind"],
)
async def test_snapshot_reads_disabled_material_owners_and_rejects_missing_types(database, invalid):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    person, _, space, presence = await _owners(database)
    kwargs = {"user_id": person}
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
        elif invalid == "space_disabled":
            await session.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == space)
                .values(enabled=False)
            )
            kwargs = {"group_id": space}
        elif invalid == "missing":
            kwargs = {"user_id": "no-such-synthetic-owner"}
        else:
            kwargs = {"user_id": presence}
    if invalid in {"person_disabled", "space_disabled"}:
        assert await runtime.snapshot(**kwargs)
    else:
        with pytest.raises(CanonicalIdentityError):
            await runtime.snapshot(**kwargs)


@pytest.mark.asyncio
async def test_known_canonical_config_owners_do_not_require_an_active_qq_binding(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    person, _, space, _ = await _owners(database)
    await _override(database, person, 70000)
    await _override(database, space, 80000, "group")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.person_id == person)
            .values(status="disabled")
        )
        await writer.execute(
            update(SpaceBindingModel)
            .where(SpaceBindingModel.space_id == space)
            .values(status="disabled")
        )
    assert (await runtime.snapshot(user_id=person)).context.compact_trigger_tokens == 70000
    assert (await runtime.snapshot(group_id=space)).context.compact_trigger_tokens == 80000


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "group"])
async def test_scoped_null_owner_override_is_rejected_by_current_schema(database, scope):
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with database.immediate_session() as writer:
            writer.add(
                RuntimeConfigOverrideModel(
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


async def test_participation_policy_matches_full_owner_resolution_after_override_delete(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    _, _, space, _ = await _owners(database)
    key = "conversation.auto_reply_enabled"
    await _override(database, None, False, "global", key=key, value_type="boolean")
    await _override(database, space, True, "group", key=key, value_type="boolean")
    await _override(database, None, 0.7, "global", key="llm.temperature", value_type="number")
    for expected in (True, False):
        narrow = await runtime.participation_policy(group_id=space)
        full = await runtime.snapshot(group_id=space)
        assert narrow == full.conversation_policy()
        assert narrow.auto_reply_enabled is expected
        async with database.immediate_session() as writer:
            await writer.execute(
                delete(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == key,
                    RuntimeConfigOverrideModel.scope_type == "group",
                )
            )
    assert (await runtime.participation_policy(group_id=None)).auto_reply_enabled is False
