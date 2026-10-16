"""Current plugin ownership follows the human; original rows and opaque KV remain."""

import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from qq_ai_bot.identity.canonical_repository import find_identity_binding
from qq_ai_bot.identity.person_identity import merge_persons
from qq_ai_bot.plugin_host.configuration_service import PluginConfigurationService
from qq_ai_bot.plugin_host.db_models import PluginConfigValueModel
from qq_ai_bot.plugin_host.ownership import PluginOwnershipError
from qq_ai_bot.plugin_host.repository import (
    PluginConfigRepository,
    PluginInstallationRepository,
    PluginStateRepository,
)
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository


async def setup(database):
    plugin = "test.person-merge"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin,
        name=plugin,
        version="1",
        plugin_api="4.0",
        yuki_requires="*",
        manifest_hash="original-plugin-manifest",
        entrypoint="plugin:register",
        requested_permissions=(),
    )
    async with database.sessions() as reader:
        source = await find_identity_binding(reader, "1001")
        target = await find_identity_binding(reader, "1002")
        foreign = await find_identity_binding(reader, "1003")
    return plugin, source.person_id, target.person_id, foreign.person_id


async def test_plugin_config_merge_retains_effective_rows_and_never_falls_back(database):
    plugin, source, target, _ = await setup(database)
    repository = PluginConfigRepository(database)
    for person, key, value in (
        (source, "source_key", "source-only"),
        (source, "conflict", "source-history"),
        (target, "conflict", "target-effective"),
    ):
        await repository.compare_and_set(
            plugin_id=plugin,
            scope_type="user",
            scope_id=person,
            key=key,
            expected_version=0,
            value=value,
        )
    async with database.sessions() as reader:
        raw = list(
            await reader.scalars(
                select(PluginConfigValueModel).where(
                    PluginConfigValueModel.plugin_id == plugin,
                    PluginConfigValueModel.canonical_person_id == source,
                )
            )
        )
        original = {row.key: (row.id, row.value_json, row.version) for row in raw}
    async with database.immediate_session() as writer:
        assert await merge_persons(writer, source, target) == target

    class Schema(BaseModel):
        source_key: str = ""
        conflict: str = ""

    service = PluginConfigurationService(database, lambda _: Schema)
    old_scope = await service.read(plugin, scope_type="user", owner_id=source)
    root_scope = await service.read(plugin, scope_type="user", owner_id=target)
    assert old_scope == root_scope
    assert root_scope["owner_id"] == target
    assert root_scope["values"] == {"source_key": "source-only", "conflict": "target-effective"}
    assert await repository.delete(
        plugin_id=plugin, scope_type="user", scope_id=source, key="conflict"
    )
    async with database.immediate_session() as writer:
        await merge_persons(writer, source, target)
    assert (
        await repository.get(plugin_id=plugin, scope_type="user", scope_id=target, key="conflict")
        is None
    )
    current = await service.read(plugin, scope_type="user", owner_id=source)
    await service.save(
        plugin,
        current["revision"],
        {
            "scope_type": "user",
            "owner_id": source,
            "values": {"source_key": "explicit-new-value", "conflict": "new-current-value"},
        },
    )
    async with database.sessions() as reader:
        historical = await reader.get(PluginConfigValueModel, original["conflict"][0])
        inherited = await reader.get(PluginConfigValueModel, original["source_key"][0])
        assert (historical.id, historical.value_json, historical.version) == original["conflict"]
        assert historical.canonical_person_id == source
        assert inherited.canonical_person_id == target
        assert json.loads(inherited.value_json) == "explicit-new-value"


async def test_person_owned_plugin_cas_accepts_same_human_without_rewriting_global_json(database):
    plugin, source, target, foreign = await setup(database)
    state = PluginStateRepository(database)
    opaque = {"person_id": source, "platform_account": "1001", "nested": [source]}
    await state.compare_and_set(
        plugin_id=plugin,
        namespace="opaque",
        key="global",
        expected_version=0,
        value=opaque,
    )
    original = await state.compare_and_set(
        plugin_id=plugin,
        namespace="owned",
        key="value",
        expected_version=0,
        value={"source": source},
        subject_user_id=source,
    )
    async with database.immediate_session() as writer:
        await merge_persons(writer, source, target)
    updated = await state.compare_and_set(
        plugin_id=plugin,
        namespace="owned",
        key="value",
        expected_version=original.version,
        value={"source": source, "updated": True},
        subject_user_id=target,
    )
    assert updated.canonical_person_id == source and updated.subject_user_id == target
    assert (await state.get(plugin_id=plugin, namespace="opaque", key="global")).value == opaque
    with pytest.raises(PluginOwnershipError, match="canonical_owner_mismatch"):
        await state.compare_and_set(
            plugin_id=plugin,
            namespace="owned",
            key="value",
            expected_version=updated.version,
            value={},
            subject_user_id=foreign,
        )


async def test_original_plugin_session_accepts_current_same_human_but_not_foreign_actor(database):
    plugin, source, target, foreign = await setup(database)
    sessions = PluginAgentSessionRepository(database)
    original = await sessions.create(
        plugin_id=plugin, owner_user_id=source, scope_type="user", scope_id=source
    )
    async with database.immediate_session() as writer:
        await merge_persons(writer, source, target)
    current = await sessions.get_for_actor(
        plugin_id=plugin,
        session_id=original.session_id,
        actor_user_id=target,
        current_group_id=None,
    )
    assert current.session_id == original.session_id
    assert current.owner_user_id == target and current.canonical_owner_person_id == source
    assert (
        await sessions.get_for_actor(
            plugin_id=plugin,
            session_id=original.session_id,
            actor_user_id=foreign,
            current_group_id=None,
        )
        is None
    )
    appended = await sessions.append_message(
        plugin_id=plugin,
        session_id=original.session_id,
        role="user",
        content="same human new input",
        sender_user_id=target,
    )
    assert appended.canonical_sender_person_id == target
