"""Real SQLite current-person ownership without rewriting original account facts."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from tests.conftest import make_settings
from tests.unit.test_runtime_config_read_snapshot import _override, _owners

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.control_resolution import ControlAccess
from qq_ai_bot.conversation.canonical_db_models import PersonActiveRouteModel
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.identity.canonical_repository import create_person_binding, find_identity_binding
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.event_author import project_event_author
from qq_ai_bot.identity.person_identity import (
    current_person_id,
    merge_persons,
    person_family_ids,
    rename_person,
)
from qq_ai_bot.identity.person_state import (
    load_canonical_aliases,
    load_canonical_profile,
    observe_canonical_person,
    set_canonical_person_enabled,
)
from qq_ai_bot.persistence.models import AdminOperationEventModel, RuntimeConfigOverrideModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.services.canonical_owners import require_live_person

NOW = datetime(2026, 10, 17, tzinfo=UTC)


async def merge(database, source, target):
    async def apply(session):
        return await merge_persons(session, source, target, now=NOW)

    return await database.apply_snapshot_write(apply)


async def test_merge_chain_keeps_raw_bindings_names_and_accountless_person(database):
    a, b, _, _ = await _owners(database)
    c = str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            CanonicalPersonModel(id=c, display_name="Final", created_at=NOW, updated_at=NOW)
        )
        before = [
            (row.id, row.person_id) for row in await session.scalars(select(IdentityBindingModel))
        ]
        a_name = (await session.get(CanonicalPersonModel, a)).display_name
    assert await merge(database, a, b) == b
    assert await merge(database, b, c) == c
    assert await merge(database, c, a) == c
    async with database.sessions() as session:
        assert await current_person_id(session, a) == c
        assert set(await person_family_ids(session, a)) == {a, b, c}
        assert [
            (row.id, row.person_id) for row in await session.scalars(select(IdentityBindingModel))
        ] == before
        for raw in (a, b):
            assert (await session.get(CanonicalPersonModel, raw)).merged_into_person_id == c
        assert await load_canonical_profile(session, person_id=c) == ("Final", "")
        assert a_name in await load_canonical_aliases(session, c, limit=50)
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()


async def test_mutual_concurrent_merges_do_not_form_cycle(database):
    a, b, _, _ = await _owners(database)
    await asyncio.gather(merge(database, a, b), merge(database, b, a))
    async with database.sessions() as session:
        root = await current_person_id(session, a)
        assert root == await current_person_id(session, b)
        assert (await session.get(CanonicalPersonModel, root)).merged_into_person_id is None
        assert set(await person_family_ids(session, root)) == {a, b}


async def test_forget_reprepares_actual_family_when_merge_commits_after_preparation(
    database, monkeypatch
):
    a, b, _, _ = await _owners(database)
    unrelated = str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            CanonicalPersonModel(id=unrelated, display_name="Other", created_at=NOW, updated_at=NOW)
        )
    people = PeopleRepository(database)
    prepared = asyncio.Event()
    release = asyncio.Event()
    original = people.prepare_person_forget
    families = []

    async def prepare(person_id, *, session, marker="[已删除用户]"):
        result = await original(person_id, session=session, marker=marker)
        families.append(set(await person_family_ids(session, person_id)))
        if len(families) == 1:
            prepared.set()
            await release.wait()
        return result

    monkeypatch.setattr(people, "prepare_person_forget", prepare)
    pending = asyncio.create_task(people.delete_person_id(a))
    try:
        await asyncio.wait_for(prepared.wait(), timeout=10)
        await merge(database, a, b)
    finally:
        release.set()
    assert await asyncio.wait_for(pending, timeout=10)
    assert families[0] == {a}
    assert families[-1] == {a, b}
    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, a) is None
        assert await session.get(CanonicalPersonModel, b) is None
        assert (await session.get(CanonicalPersonModel, unrelated)).display_name == "Other"
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()


async def test_configuration_target_wins_unique_moves_and_unset_never_falls_back(database):
    a, b, _, _ = await _owners(database)
    await _override(database, a, 70000)
    await _override(database, b, 80000)
    await _override(database, a, 0.3, key="llm.temperature", value_type="number")
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    await merge(database, a, b)
    assert (await runtime.snapshot(user_id=a)).context.window_tokens == 80000
    assert (await runtime.snapshot(memory_scope=MemoryConfigScope(a))).llm.temperature == 0.3
    inspected = await runtime.inspect_configs(
        ("context.window_tokens",), scope=MemoryConfigScope(a)
    )
    assert inspected[0].effective.value == 80000
    removed = await runtime.delete_override(
        "context.window_tokens",
        scope_type="user",
        scope_id=a,
        actor_user_id="operator",
        trigger_message_id="",
    )
    assert removed.success
    await merge(database, a, b)
    assert (await runtime.snapshot(user_id=a)).context.window_tokens == make_settings(
        database.url
    ).context_window_tokens
    async with database.sessions() as session:
        original = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.canonical_person_id == a,
                RuntimeConfigOverrideModel.config_key == "context.window_tokens",
            )
        )
        assert json.loads(original.value_json) == 70000


@pytest.mark.parametrize("old_format", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
async def test_rollback_requires_original_configuration_row_not_equal_value_version(
    database,
    old_format,
    conflict,
):
    a, b, _, _ = await _owners(database)
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    change = await runtime.set_override(
        "context.window_tokens",
        70000,
        scope_type="user",
        scope_id=a,
        actor_user_id="operator",
        trigger_message_id="",
    )
    assert change.success
    async with database.immediate_session() as session:
        audit = await session.scalar(
            select(AdminOperationEventModel).where(
                AdminOperationEventModel.capability == "runtime_config",
                AdminOperationEventModel.operation == "set_override",
            )
        )
        change_id = audit.id
        if old_format:
            after = json.loads(audit.after_json)
            after.pop("row_id")
            audit.after_json = json.dumps(after)
    if conflict:
        await _override(database, b, 70000)
    await merge(database, a, b)
    # The public Admin command also records a normal command audit, distinct
    # from the domain's configuration movement facts.
    from qq_ai_bot.client_api.identity import fingerprint
    from qq_ai_bot.persistence.command_receipts import CommandReceiptRepository

    async with database.immediate_session() as session:
        await CommandReceiptRepository(database).record_personal_command(
            session,
            principal_id=str(uuid4()),
            principal_kind="control",
            person_id=b,
            request_id=str(uuid4()),
            operation="identity.person.merge",
            operation_ref=b,
            payload_hash=fingerprint("identity.person.merge", a, {"to_person": b}),
            resource_id=b,
            result={"person_id": b},
        )
    result = await runtime.rollback(change_id, actor_user_id="operator")
    assert result.success is not conflict
    if conflict:
        assert result.error_category == "rollback_conflict"
        assert (await runtime.snapshot(user_id=b)).context.window_tokens == 70000
    else:
        assert (await runtime.snapshot(user_id=b)).context.window_tokens == make_settings(
            database.url
        ).context_window_tokens


async def test_route_trigger_accepts_root_to_original_binding_and_keeps_target_route(database):
    a, b, _, presence = await _owners(database)
    async with database.immediate_session() as session:
        presence = await session.scalar(
            select(PresenceModel.id).where(PresenceModel.platform == "qq").limit(1)
        )
        bindings = {
            row.person_id: row.id for row in await session.scalars(select(IdentityBindingModel))
        }
        session.add(
            PersonActiveRouteModel(
                person_id=a,
                identity_binding_id=bindings[a],
                presence_id=presence,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    await merge(database, a, b)
    async with database.immediate_session() as session:
        route = await session.get(PersonActiveRouteModel, b)
        assert route.identity_binding_id == bindings[a]
        route.revision += 1  # Real UPDATE trigger also accepts source-owned binding.
        route.identity_binding_id = bindings[b]
    c = str(uuid4())
    async with database.immediate_session() as session:
        session.add(CanonicalPersonModel(id=c, created_at=NOW, updated_at=NOW))
    await merge(database, c, b)
    async with database.sessions() as session:
        assert (await session.get(PersonActiveRouteModel, b)).identity_binding_id == bindings[b]


async def test_initial_name_is_person_owned_and_rename_survives_platform_observation(database):
    async with database.immediate_session() as session:
        binding = await create_person_binding(
            session, external_account_id="name-test", display_name="", platform="telegram", now=NOW
        )
        person = binding.person_id
        for nickname in ("First", "Platform rename"):
            await observe_canonical_person(
                session,
                binding=binding,
                nickname=nickname,
                nickname_known=True,
                group_id=None,
                group_card="",
                group_name="",
                group_card_known=False,
                now=NOW,
            )
        assert (await session.get(CanonicalPersonModel, person)).display_name == "First"
        await rename_person(session, person, "Chosen", now=NOW)
        binding.status = "disabled"
    async with database.sessions() as session:
        assert await load_canonical_profile(session, person_id=person) == ("Chosen", "")
        assert {"First", "Platform rename"} <= set(
            await load_canonical_aliases(session, person, limit=20)
        )


async def test_disable_source_does_not_revoke_target_but_disable_root_blocks_every_entry(database):
    a, b, _, _ = await _owners(database)
    async with database.immediate_session() as session:
        await set_canonical_person_enabled(session, a, False, now=NOW)
    await merge(database, a, b)
    async with database.sessions() as session:
        with pytest.raises(CanonicalIdentityError) as error:
            await require_live_person(session, a)
        assert error.value.category == "canonical_owner_disabled"
        assert await require_live_person(session, b) == b
    async with database.immediate_session() as session:
        await set_canonical_person_enabled(session, b, False, now=NOW)
    async with database.sessions() as session:
        with pytest.raises(CanonicalIdentityError) as error:
            await require_live_person(session, b)
        assert error.value.category == "canonical_owner_disabled"


async def test_root_disable_rejects_new_original_actor_ingress_and_qq_grant_stays_real(database):
    a, b, _, _ = await _owners(database)
    await merge(database, a, b)
    access = ControlAccess(database, superuser_ids={"10001"})
    assert "superuser" in (await access.principal_for_person(b)).roles
    async with database.sessions() as session:
        author = await project_event_author(
            session,
            sender_user_id="10001",
            sender_is_bot=False,
            event_kind="message",
            direction="inbound",
        )
        assert author.author_person_id == a
    async with database.immediate_session() as session:
        await set_canonical_person_enabled(session, b, False, now=NOW)
    async with database.sessions() as session:
        with pytest.raises(CanonicalIdentityError) as error:
            await project_event_author(
                session,
                sender_user_id="10001",
                sender_is_bot=False,
                event_kind="message",
                direction="inbound",
            )
        assert error.value.category == "canonical_owner_disabled"
        assert (await find_identity_binding(session, "10001")).person_id == a


@pytest.mark.parametrize("control_actor", [False, True])
async def test_rollback_human_follows_merge_but_independent_operator_does_not(
    database, control_actor
):
    from qq_ai_bot.admin.models import ControlAuditRef

    a, b, _, _ = await _owners(database)
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    original = (
        ControlAuditRef(user_id=a, principal_kind="control", principal_id=str(uuid4()))
        if control_actor
        else None
    )
    change = await runtime.set_override(
        "context.window_tokens",
        70000,
        scope_type="user",
        scope_id=a,
        actor_user_id=a,
        trigger_message_id="",
        audit_ref=original,
    )
    assert change.success
    await merge(database, a, b)
    current = (
        ControlAuditRef(user_id=b, principal_kind="control", principal_id=str(uuid4()))
        if control_actor
        else None
    )
    result = await runtime.rollback(change.change_id, actor_user_id=b, audit_ref=current)
    assert result.success is not control_actor
    if control_actor:
        assert result.error_category == "permission_denied"
    else:
        assert (await runtime.snapshot(user_id=b)).context.window_tokens == make_settings(
            database.url
        ).context_window_tokens


async def test_legacy_opaque_operator_rollback_uses_original_principal_exactly(database):
    from qq_ai_bot.admin.models import ControlAuditRef

    a, b, _, _ = await _owners(database)
    operator_id = str(uuid4())
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    change = await runtime.set_override(
        "context.window_tokens",
        70000,
        scope_type="user",
        scope_id=a,
        actor_user_id=operator_id,
        trigger_message_id="",
    )
    await merge(database, a, b)
    result = await runtime.rollback(
        change.change_id,
        actor_user_id=b,
        audit_ref=ControlAuditRef(user_id=b, principal_kind="control", principal_id=operator_id),
    )
    assert result.success
