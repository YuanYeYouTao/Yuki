"""Current material owners are independent of the executing source's qualification."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.conftest import make_settings
from tests.unit.test_memory_mutation import _service
from tests.unit.test_runtime_config_read_snapshot import _override, _owners

from yuki.admin.audit import AdminAuditService
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import AdminActor
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import merge_persons
from yuki.memory.enums import MemoryScopeType, MemorySourceType
from yuki.memory.models import MemoryFactCreate
from yuki.services.admin.memory_admin import MemoryAdminService


async def test_effective_configuration_keeps_owner_and_value_in_one_read_snapshot(
    database, monkeypatch
):
    from yuki.admin import config_service as module

    source, target, _, _ = await _owners(database)
    await _override(database, source, 70000)
    await _override(database, target, 80000)
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    resolve = module.current_person_id
    merged = False

    async def merge_during_resolution(session, raw):
        nonlocal merged
        current = await resolve(session, raw)
        if not merged:
            merged = True
            await database.apply_snapshot_write(
                lambda writer: merge_persons(writer, source, target)
            )
        return current

    monkeypatch.setattr(module, "current_person_id", merge_during_resolution)
    first = await runtime.get_effective("context.compact_trigger_tokens", user_id=source)
    assert merged and first.value == 70000
    assert (
        await runtime.get_effective("context.compact_trigger_tokens", user_id=source)
    ).value == 80000


@pytest.mark.parametrize("root_enabled", [False, True])
async def test_runtime_configuration_resolves_disabled_material_alias_without_enabling_it(
    database, root_enabled
):
    source, target, _, _ = await _owners(database)
    await _override(database, source, 70000)
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, source)).enabled = False
        (await writer.get(CanonicalPersonModel, target)).enabled = root_enabled
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    assert (await runtime.snapshot(user_id=source)).context.compact_trigger_tokens == 70000
    assert (
        await runtime.get_effective("context.compact_trigger_tokens", user_id=source)
    ).value == 70000
    changed = await runtime.set_override(
        "context.compact_trigger_tokens",
        71000,
        scope_type="user",
        scope_id=source,
        actor_user_id="authorized-operator",
        trigger_message_id="material-update",
        expected_version=1,
    )
    assert changed.success, changed
    assert (await runtime.snapshot(user_id=target)).context.compact_trigger_tokens == 71000
    async with database.sessions() as reader:
        assert not (await reader.get(CanonicalPersonModel, source)).enabled
        assert (await reader.get(CanonicalPersonModel, target)).enabled is root_enabled


async def test_memory_admin_reads_same_person_material_but_still_rejects_disabled_actor(database):
    source, target, _, _ = await _owners(database)
    _, facts, _, _ = _service(database)
    original = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            canonical_subject_person_id=source,
            memory_key="retained",
            category="probe",
            content="retained original source material",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, source)).enabled = False
    service = MemoryAdminService(
        settings=make_settings(database.url), memories=facts, audit=AdminAuditService(database)
    )
    actor = AdminActor("10002", False, "read", "", person_id=target)
    rows = await service.list_memories(actor, "10001", target_person_id=source)
    assert [row.id for row in rows] == [original.id]
    disabled_actor = AdminActor("10001", False, "old-source", "", person_id=source)
    with pytest.raises(CanonicalIdentityError, match="canonical") as problem:
        await service.list_memories(disabled_actor, "10002", target_person_id=target)
    assert problem.value.category == "canonical_owner_disabled"


async def test_superuser_reads_disabled_person_material_without_any_account(database):
    _, facts, _, _ = _service(database)
    now = datetime.now(UTC)
    subject = str(uuid4())
    async with database.immediate_session() as writer:
        writer.add(
            CanonicalPersonModel(
                id=subject,
                display_name="Person without account",
                enabled=False,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        admin = await find_identity_binding(writer, "9000")
        actor_id = admin.person_id
    original = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            canonical_subject_person_id=subject,
            memory_key="retained",
            category="probe",
            content="retained no-account person material",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    service = MemoryAdminService(
        settings=make_settings(database.url), memories=facts, audit=AdminAuditService(database)
    )
    actor = AdminActor("9000", False, "read", "", person_id=actor_id)
    rows = await service.list_memories(actor, subject, target_person_id=subject)
    assert [row.id for row in rows] == [original.id]
