"""Person merge preserves actual configuration rows and independent Memory streams."""

import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_person_plugin_merge import setup

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons
from yuki.memory.enums import MemoryScopeType, MemorySourceType, MemoryStatus, SelfMemoryVisibility
from yuki.memory.models import MemoryFactCreate, MemoryFactQuery
from yuki.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.persistence.models import AdminOperationEventModel, MemoryMutationReceiptModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.plugin_host.config import BoundConfigFacade
from yuki.plugin_host.configuration_service import PluginConfigurationService
from yuki.plugin_host.repository import (
    PluginConfigRepository,
    PluginStateRepository,
    PluginVersionConflictError,
)
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.asyncio
async def test_sdk_write_after_merge_rejects_different_row_with_equal_version(
    database, monkeypatch
):
    plugin, source, target, _ = await setup(database)
    repository = PluginConfigRepository(database)
    source_row = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=source,
        key="mode",
        expected_version=0,
        value="source-original",
    )
    target_row = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=target,
        key="mode",
        expected_version=0,
        value="target-explicit-choice",
    )
    assert source_row.id != target_row.id and source_row.version == target_row.version == 1
    original_get = repository.get
    merged = False

    async def read_then_merge(**kwargs):
        nonlocal merged
        result = await original_get(**kwargs)
        if not merged:
            merged = True
            await database.apply_snapshot_write(
                lambda writer: merge_persons(writer, source, target)
            )
        return result

    monkeypatch.setattr(repository, "get", read_then_merge)
    facade = BoundConfigFacade(
        repository=repository,
        plugin_id=plugin,
        approved_permissions=(PluginPermission.PLUGIN_CONFIG_WRITE,),
        current_user_id=source,
    )
    with pytest.raises(PluginVersionConflictError):
        await facade.set("mode", "stale-source-overwrites-target", scope_type="user")
    current = await original_get(plugin_id=plugin, scope_type="user", scope_id=target, key="mode")
    assert current.id == target_row.id
    assert current.value == "target-explicit-choice"
    assert current.version == 1


@pytest.mark.asyncio
async def test_original_scope_cas_delete_after_merge_preserves_target_equal_version(database):
    plugin, source, target, _ = await setup(database)
    repository = PluginConfigRepository(database)
    source_row = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=source,
        key="mode",
        expected_version=0,
        value="source-original",
    )
    target_row = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=target,
        key="mode",
        expected_version=0,
        value="target-explicit-choice",
    )
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    with pytest.raises(PluginVersionConflictError):
        await repository.delete(
            plugin_id=plugin,
            scope_type="user",
            scope_id=source,
            key="mode",
            expected_version=source_row.version,
            expected_row_id=source_row.id,
        )
    current = await repository.get(plugin_id=plugin, scope_type="user", scope_id=target, key="mode")
    assert current.id == target_row.id and current.value == "target-explicit-choice"


@pytest.mark.asyncio
async def test_one_private_entry_reset_preserves_other_entry_self_reflection(database):
    _, _, ledger, _ = _service(database)
    first = await _event(
        ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="first private entry baseline",
        bot_user_id="8000",
    )
    second = await _event(
        ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="second private entry baseline",
        bot_user_id="8001",
    )
    assert first.canonical_conversation_id != second.canonical_conversation_id
    assert first.author_person_id == second.author_person_id
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    events = {}
    for bot in ("8000", "8001"):
        events[bot] = await _event(
            ledger,
            message_id=str(uuid4()),
            sender_user_id="1001",
            content="real pending private experience " + bot,
            bot_user_id=bot,
        )
    await repository.scan_new_events()
    reset_bot = "8001"
    picked_id = events[reset_bot].canonical_conversation_id
    live = events["8000"]
    boundary = await _event(
        ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="only this entry is reset now",
        bot_user_id=reset_bot,
    )
    await repository.scan_new_events()
    async with database.immediate_session() as writer:
        resetting = await writer.get(CanonicalConversationModel, picked_id)
        resetting.generation += 1
        resetting.last_generation_change_event_id = boundary.id
        untouched = await writer.get(CanonicalConversationModel, live.canonical_conversation_id)
        assert untouched.last_generation_change_event_id < live.id
    claimed = await repository.claim_due(
        scheduled_slot="private-entry-reset-probe",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=100,
        max_characters=16000,
        context_events=0,
    )
    assert len(claimed) == 1
    assert [item.id for item in claimed[0].events] == [live.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("private_self", [False, True])
async def test_merge_memory_duplicate_keys_remain_independent_and_fact_id_is_editable(
    database, private_self
):
    _, source, target, _ = await setup(database)
    _, facts, _, _ = _service(database)
    originals = []
    for person, value in ((source, "source independent fact"), (target, "target independent fact")):
        kwargs = (
            {
                "scope_type": MemoryScopeType.SELF,
                "visibility_type": SelfMemoryVisibility.PRIVATE,
                "canonical_visibility_person_id": person,
            }
            if private_self
            else {"scope_type": MemoryScopeType.PERSON, "canonical_subject_person_id": person}
        )
        originals.append(
            await facts.remember(
                MemoryFactCreate(
                    **kwargs,
                    memory_key="same-key",
                    category="probe",
                    content=value,
                    source_type=MemorySourceType.EXPLICIT,
                )
            )
        )
    async with database.immediate_session() as writer:
        old_source_person = await writer.get(CanonicalPersonModel, source)
        old_source_person.enabled = False
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    for reference in (source, target):
        query = (
            MemoryFactQuery(
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.PRIVATE,
                canonical_visibility_person_id=reference,
            )
            if private_self
            else MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                canonical_subject_person_id=reference,
            )
        )
        rows = await facts.repository.list_facts(query, limit=None)
        assert {row.id for row in rows} == {row.id for row in originals}
    original_source = originals[0]
    corrected = await facts.correct_fact(
        original_source.id, content="only source fact explicitly corrected", actor_user_id="1002"
    )
    assert corrected is not None and corrected.supersedes_id == original_source.id
    old_source = await facts.get_fact(original_source.id)
    unchanged_target = await facts.get_fact(originals[1].id)
    assert old_source.status is MemoryStatus.SUPERSEDED
    assert unchanged_target.status is MemoryStatus.ACTIVE
    assert unchanged_target.content == "target independent fact"
    if private_self:
        assert old_source.canonical_visibility_person_id == source
    else:
        assert old_source.canonical_subject_person_id == source


async def test_memory_prepare_then_same_human_merge_commits_original_actor(database, monkeypatch):
    service, facts, ledger, _ = _service(database)
    _, source, target, _ = await setup(database)
    event = await _event(
        ledger, message_id="same-person-merge", sender_user_id="1001", content="记住我住在北京"
    )
    assert event.author_person_id == source
    original_commit = service._commit_prepared
    actors = []

    async def merge_then_commit(prepared, **kwargs):
        actors.append(prepared.actor_person_id)
        await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
        return await original_commit(prepared, **kwargs)

    monkeypatch.setattr(service, "_commit_prepared", merge_then_commit)
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
            ),
            new_content="住在北京",
            memory_key="location:home",
            category="location",
            reason="original_self_report",
        ),
        _context(event),
    )
    assert result.ok and result.new_fact_id is not None
    assert actors == [source]
    saved = await facts.get_fact(result.new_fact_id)
    # The prepared fact keeps its original owner; current reads use the family.
    assert saved.canonical_subject_person_id == source
    async with database.sessions() as reader:
        receipt = await reader.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.new_fact_id == result.new_fact_id
            )
        )
        assert receipt.trigger_actor_user_id == event.sender_user_id
        assert receipt.trigger_event_id == event.id


async def test_disabled_source_material_is_readable_and_explicit_scope_matches(
    database,
):
    plugin, source, target, _ = await setup(database)
    states = PluginStateRepository(database)
    original = await states.compare_and_set(
        plugin_id=plugin,
        namespace="people",
        key="source",
        expected_version=0,
        value={"note": "source material"},
        subject_user_id=source,
    )
    configs = PluginConfigRepository(database)
    await configs.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=source,
        key="favorite",
        expected_version=0,
        value="old favorite",
    )
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, source)).enabled = False
        await merge_persons(writer, source, target)
    current = await states.get(plugin_id=plugin, namespace="people", key="source")
    assert current.canonical_person_id == source and current.subject_user_id == target
    assert (await states.list_namespace(plugin_id=plugin, namespace="people")) == (current,)
    changed = await states.compare_and_set(
        plugin_id=plugin,
        namespace="people",
        key="source",
        expected_version=original.version,
        value={"note": "edited material"},
        subject_user_id=target,
    )
    assert changed.canonical_person_id == source and changed.subject_user_id == target
    facade = BoundConfigFacade(
        repository=configs,
        plugin_id=plugin,
        current_user_id=target,
        approved_permissions=(
            PluginPermission.PLUGIN_CONFIG_READ,
            PluginPermission.PLUGIN_CONFIG_WRITE,
        ),
    )
    assert await facade.get("favorite", scope_type="user", scope_id=source) == "old favorite"
    await facade.set("favorite", "new favorite", scope_type="user", scope_id=source)
    assert await facade.get("favorite", scope_type="user") == "new favorite"


async def test_original_config_revision_survives_owner_move_and_new_row_never_reuses_id(
    database,
):
    plugin, source, target, _ = await setup(database)
    repository = PluginConfigRepository(database)
    original = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=source,
        key="value",
        expected_version=0,
        value="before merge",
    )

    class Schema(BaseModel):
        value: str = ""

    service = PluginConfigurationService(database, lambda _: Schema)
    document = await service.read(plugin, scope_type="user", owner_id=source)
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    assert (await service.read(plugin, scope_type="user", owner_id=source))["revision"] == document[
        "revision"
    ]
    await service.save(
        plugin,
        document["revision"],
        {"scope_type": "user", "owner_id": source, "values": {"value": "legal move"}},
    )
    moved = await repository.get(plugin_id=plugin, scope_type="user", scope_id=target, key="value")
    assert moved.id == original.id and moved.value == "legal move"
    await repository.delete(plugin_id=plugin, scope_type="user", scope_id=target, key="value")
    replacement = await repository.compare_and_set(
        plugin_id=plugin,
        scope_type="user",
        scope_id=target,
        key="value",
        expected_version=0,
        value="new row",
    )
    assert replacement.id != original.id and replacement.version == original.version
    with pytest.raises(PluginVersionConflictError):
        await repository.compare_and_set(
            plugin_id=plugin,
            scope_type="user",
            scope_id=source,
            key="value",
            expected_row_id=original.id,
            expected_version=original.version,
            value="stale overwrite",
        )


async def test_plugin_merge_selects_complete_object_and_sdk_preserves_field_metadata(database):
    plugin, source, target, _ = await setup(database)

    class Schema(BaseModel):
        minimum: int = Field(default=0, ge=0, le=9)
        maximum: int = Field(default=10, ge=0, le=10)

        @model_validator(mode="after")
        def ordered(self):
            if self.minimum > self.maximum:
                raise ValueError("minimum exceeds maximum")
            return self

    repository = PluginConfigRepository(database)
    for person, key, value in ((source, "minimum", 8), (target, "maximum", 4)):
        await repository.compare_and_set(
            plugin_id=plugin,
            scope_type="user",
            scope_id=person,
            key=key,
            expected_version=0,
            value=value,
        )
    await database.apply_snapshot_write(lambda writer: merge_persons(writer, source, target))
    document = await PluginConfigurationService(database, lambda _: Schema).read(
        plugin, scope_type="user", owner_id=source
    )
    assert document["valid"] and document["values"] == {"minimum": 0, "maximum": 4}
    facade = BoundConfigFacade(
        repository=repository,
        plugin_id=plugin,
        schema=Schema,
        current_user_id=target,
        approved_permissions=(PluginPermission.PLUGIN_CONFIG_WRITE,),
    )
    with pytest.raises(ValueError, match="invalid plugin config value"):
        await facade.set("minimum", 11, scope_type="user")
    # SDK keeps its per-field contract; it does not run cross-field object validators.
    await facade.set("minimum", 8, scope_type="user")
    assert (
        await repository.get(plugin_id=plugin, scope_type="user", scope_id=target, key="minimum")
    ).value == 8


async def test_merge_archived_config_is_redacted_and_person_forget_removes_its_content(database):
    plugin, source, target, foreign = await setup(database)
    repository = PluginConfigRepository(database)
    for person, key, value in (
        (source, "api_key", "secret-that-must-never-reach-audit"),
        (source, "profile", {"label": "PRIVATE PROFILE BODY", "password": "nested-secret"}),
        (target, "profile", "current explicit object"),
    ):
        await repository.compare_and_set(
            plugin_id=plugin,
            scope_type="user",
            scope_id=person,
            key=key,
            expected_version=0,
            value=value,
        )
    await database.apply_snapshot_write(
        lambda writer: merge_persons(writer, source, target, actor_id=foreign)
    )
    async with database.sessions() as reader:
        audit = await reader.scalar(
            select(AdminOperationEventModel).where(
                AdminOperationEventModel.operation == "identity.person.merge"
            )
        )
        audit_id = audit.id
        assert "secret-that-must-never-reach-audit" not in audit.before_json
        assert "nested-secret" not in audit.before_json
        assert "PRIVATE PROFILE BODY" in audit.before_json
        snapshots = json.loads(audit.before_json)["discarded_configurations"]
        assert len(snapshots) == 2
        assert {snapshot["row"]["canonical_person_id"] for snapshot in snapshots} == {source}
    assert await PeopleRepository(database).delete_person_id(target)
    async with database.sessions() as reader:
        retained = await reader.get(AdminOperationEventModel, audit_id)
        assert retained is not None
        assert "PRIVATE PROFILE BODY" not in retained.before_json
        assert source not in retained.before_json and target not in retained.after_json
        assert all(
            "row" not in item
            for item in json.loads(retained.before_json)["discarded_configurations"]
        )
        assert await reader.get(CanonicalPersonModel, source) is None
        assert await reader.get(CanonicalPersonModel, target) is None
        assert await reader.get(CanonicalPersonModel, foreign) is not None
