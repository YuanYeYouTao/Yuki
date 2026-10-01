"""Global storage admission changes preserve protocol evidence and semantic windows."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tests.conftest import make_settings
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_control_config_consistency import set_value, setup
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.admin.action_service import ActionRegistry
from qq_ai_bot.admin.capabilities import AdminCapabilityService
from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.persistence import PersistenceModule
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize(
    "values",
    [
        {"work_protocol_total_max_bytes": 0},
        {"work_protocol_object_max_bytes": 0},
        {"work_protocol_disk_reserve_bytes": 0},
        {"work_protocol_total_max_bytes": 8, "work_protocol_object_max_bytes": 9},
    ],
)
def test_startup_policy_rejects_invalid_capacity(values):
    with pytest.raises(ValidationError):
        make_settings("sqlite+aiosqlite:///:memory:", **values)


@pytest.mark.asyncio
async def test_policy_is_global_hot_and_wired_to_object_admission(database, tmp_path, monkeypatch):
    runtime, person, space = await setup(database)
    initial = await runtime.snapshot()
    assert initial.work_storage == WorkStorageRuntimeConfig()
    assert (await set_value(runtime, "storage.protocol_object_max_bytes", 8)).success
    assert (await set_value(runtime, "storage.protocol_total_max_bytes", 16)).success
    assert (await set_value(runtime, "storage.protocol_disk_reserve_bytes", 8)).success
    for key, value, scope, owner in (
        ("storage.protocol_object_max_bytes", 0, "global", ""),
        ("storage.protocol_total_max_bytes", 7, "global", ""),
        ("storage.protocol_object_max_bytes", 17, "global", ""),
        ("storage.protocol_disk_reserve_bytes", 1, "group", space.text),
        ("storage.protocol_object_max_bytes", 1, "user", person.text),
    ):
        assert not (await set_value(runtime, key, value, scope, owner)).success
    actual = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert actual.work_storage == WorkStorageRuntimeConfig(16, 8, 8)
    assert actual.context == initial.context
    PersistenceModule(
        make_settings(database.url),
        database=database,
        runtime_config=runtime,
        lifecycle=LifecycleRegistry(),
    ).build()
    store = ProtocolStore(database)
    await store.refresh_policy()
    with pytest.raises(ValueError, match="object_capacity"):
        await store.put_bytes(b"ninebytes")
    monkeypatch.setattr(
        "qq_ai_bot.runtime.protocol_store.shutil.disk_usage", lambda _: SimpleNamespace(free=10)
    )
    with pytest.raises(ValueError, match="storage_capacity"):
        await store.put_bytes(b"new")
    assert (await set_value(runtime, "storage.protocol_disk_reserve_bytes", 2)).success
    await store.refresh_policy()
    assert await store.get_bytes(await store.put_bytes(b"new")) == b"new"


@pytest.mark.asyncio
async def test_lowered_capacity_keeps_existing_objects_readable_and_shareable(database, tmp_path):
    control = await _control(database, tmp_path)
    policy = WorkStorageRuntimeConfig(32, 16, 1)

    async def resolve():
        return policy

    database.protocol_storage_policy = resolve
    store = ProtocolStore(database)
    await store.refresh_policy()

    async def publish(owner):
        async with store.publication(owner) as prepared:
            async with database.immediate_session() as writer:
                await store.publish_refs(writer, owner, prepared)

    original = await store.put_bytes(b"evidence")
    await publish(control.current["id"])
    policy = WorkStorageRuntimeConfig(2, 2, 1)
    await store.refresh_policy()
    # Recovery may retain existing evidence after lowering admission capacity.
    assert await store.put_bytes(b"evidence") == original
    await publish(control.current["id"])
    other = await control.repository.accept(
        control.lease, source_key="shared", source={}, goal="reuse"
    )
    assert await store.put_bytes(b"evidence") == original
    await publish(other["id"])
    await store.put_bytes(b"x")
    with pytest.raises(ValueError, match="storage_capacity"):
        await publish(other["id"])
    assert await store.get_bytes(original) == b"evidence"


@pytest.mark.asyncio
async def test_checkpoint_resolves_storage_policy_once_for_all_objects(database, tmp_path):
    control = await _control(database, tmp_path)
    reads = 0

    async def resolve():
        nonlocal reads
        reads += 1
        return WorkStorageRuntimeConfig()

    database.protocol_storage_policy = resolve
    session = WorkSession(control, "fixed")
    await session.restore(
        TurnTranscript((ChatMessage("system", "contract"), ChatMessage("user", "goal")))
    )
    await session.save("paired")
    assert reads == 1
    await session.save("paired")
    assert reads == 2


@pytest.mark.asyncio
async def test_large_integer_capacity_relation_remains_exact(database):
    runtime, _, _ = await setup(database)
    total = 2**53
    assert (await set_value(runtime, "storage.protocol_total_max_bytes", total)).success
    rejected = await set_value(runtime, "storage.protocol_object_max_bytes", total + 1)
    assert not rejected.success and rejected.error_category == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("storage.protocol_total_max_bytes", 2 * 1024**3 + 1),
        ("storage.protocol_object_max_bytes", 64 * 1024**2 + 1),
        ("storage.protocol_disk_reserve_bytes", 64 * 1024**2 + 1),
    ],
)
async def test_global_storage_writes_require_actual_superuser(database, key, value):
    runtime, _, _ = await setup(database)
    service = AdminCapabilityService(
        settings=make_settings(database.url),
        runtime_config=runtime,
        actions=SimpleNamespace(registry=ActionRegistry()),
    )
    arguments = json.dumps({"key": key, "value": value, "scope_type": "global", "scope_id": ""})

    async def write(user, authority):
        source = replace(
            inbound("configure storage", user_id=user, message_id="boundary"), source_event_id=1
        )
        return json.loads(
            await service.execute(
                "admin_set_config",
                arguments,
                ToolRuntime(
                    inbound=source,
                    gateway=None,
                    allow_generic_onebot=False,
                    actor_is_superuser=authority,
                ),
            )
        )

    for asserted_authority in (False, True):
        result = await write("1001", asserted_authority)
        assert not result["ok"] and result["error"] == "permission_denied"
    result = await write("9000", True)
    assert result["ok"] and result["data"]["after"] == value
