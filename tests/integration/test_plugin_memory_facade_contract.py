"""Direct SDK Memory calls agree with SQLite Host effects and real provenance."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.unit.test_memory_mutation import _event, _service

from yuki.admin.audit import AdminAuditService
from yuki.admin.models import AdminActor
from yuki.automation.models import TurnOrigin
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.mutation.models import MemoryMutationRequestBasis
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.plugin_host.facades import HostPluginContext
from yuki.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing import FakeMemoryFacade


@pytest.fixture
async def memory_host(database, tmp_path):
    env, _, _chat, source, host = await source_host(
        database,
        tmp_path,
        FakeLLMProvider(),
        (
            PluginPermission.MEMORY_WRITE,
            PluginPermission.MEMORY_DELETE,
            PluginPermission.MEMORY_PERSON_READ,
        ),
    )
    mutations, facts, ledger, _ = _service(database)
    admin = MemoryAdminService(
        settings=make_settings(database.url),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=mutations,
        ledger=ledger,
    )
    host._services.memories = facts
    host._services.memory_admin = admin
    with host.invocation_scope(host.plugin_id, source):
        invocation = replace(host._invocation(), delivery_identity="original-memory-callback")
    try:
        yield env, host, invocation, facts, admin
    finally:
        await host.close_host_resources()


async def test_host_and_fake_share_memory_versions_confidence_and_shapes(database, memory_host):
    env, host, invocation, facts, _ = memory_host
    fake, originals, revised_ids = FakeMemoryFacade(), {}, {}
    with host.bind(invocation):
        for name, facade in (("host", host.memory), ("fake", fake)):
            added = await facade.add(
                scope_type="person",
                subject_id=env.person,
                content="likes astronomy",
                source_type="plugin",
                confidence=0.37,
                source_event_ids=(str(invocation.source_event_id),),
            )
            original = originals[name] = added.data["memory"]
            original_id = original["memory_id"]
            assert original_id == f"person:{original['fact_id']}"
            assert original["user_id"] == original["subject_user_id"] == env.person
            assert (original["confidence"], original["source_type"], original["authority"]) == (
                0.37,
                "automatic",
                "self_report",
            )
            assert (await facade.list_person(env.person))[0]["memory_id"] == original_id
            if name == "host":
                original_evidence = await facts.list_evidence(original["fact_id"])
            updated = await facade.update(original_id, content="likes physics", confidence=0.62)
            new_id = updated.data["memory_id"]
            assert new_id != original_id and new_id.startswith("person:")
            retained = await facade.update(new_id, content="likes physics too", confidence=None)
            latest = (await facade.list_person(env.person))[0]
            revised_ids[name] = retained.data["memory_id"]
            assert latest["memory_id"] == revised_ids[name] != new_id
            assert (latest["confidence"], latest["source_type"], latest["authority"]) == (
                0.62,
                "explicit",
                "explicit",
            )
            deleted = await facade.delete(revised_ids[name])
            assert deleted.ok and deleted.data == {"memory_id": revised_ids[name]}
            assert await facade.list_person(env.person) == ()
    original = originals["host"]
    old = await facts.get_fact(original["fact_id"])
    assert old.content == "likes astronomy" and old.status.value == "superseded"
    assert await facts.list_evidence(old.id) == original_evidence
    evidence = await facts.list_evidence(int(revised_ids["host"].split(":")[1]))
    assert next(item for item in evidence if item.relation.value == "correction").confidence == 0.62
    assert fake.records[originals["fake"]["memory_id"]]["content"] == "likes astronomy"
    assert fake.records[originals["fake"]["memory_id"]]["status"] == "superseded"


async def test_two_calls_and_cold_replay_keep_each_original_identity(database, memory_host):
    env, host, invocation, facts, _ = memory_host

    async def two_adds():
        ids = []
        with host.bind(invocation):
            for content, source in (
                ("first independent fact", "plugin"),
                ("second independent fact", "automatic"),
            ):
                result = await host.memory.add(
                    scope_type="person",
                    subject_id=env.person,
                    content=content,
                    source_type=source,
                    confidence=0.4,
                )
                ids.append(result.data["memory"]["memory_id"])
                assert result.data["memory"]["source_type"] == "automatic"
        return ids

    first = await two_adds()
    assert first[0] != first[1]
    await database.engine.dispose()
    assert await two_adds() == first
    assert len(await facts.list_person(person_id=env.person)) == 2
    with host.bind(invocation), pytest.raises(ValueError, match="payload_conflict"):
        await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="changed original argument",
            source_type="plugin",
            confidence=0.4,
        )


@pytest.mark.parametrize("invalidate_latest", [False, True])
async def test_cold_callback_replays_each_correction_after_later_versions(
    database, memory_host, invalidate_latest
):
    env, host, invocation, _, _ = memory_host

    async def sequence(context):
        with context.bind(invocation):
            added = await context.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="original fact",
                source_type="plugin",
                confidence=0.4,
            )
            original = added.data["memory"]["memory_id"]
            first = await context.memory.update(original, content="first correction")
            second = await context.memory.update(
                first.data["memory_id"], content="second correction"
            )
            assert first.ok and second.ok
            if invalidate_latest:
                assert (await context.memory.delete(second.data["memory_id"])).ok
            return original, first, second

    original = await sequence(host)
    async with database.read_session() as reader:
        receipts = tuple(await reader.scalars(select(MemoryMutationReceiptModel.mutation_id)))
        fact_ids = tuple(await reader.scalars(select(MemoryFactModel.id)))
    await database.engine.dispose()
    mutations, facts, ledger, _ = _service(database)
    rebuilt = HostPluginContext(
        plugin_id=host.plugin_id,
        approved_permissions=host._approved_permissions,
        services=replace(
            host._services,
            memories=facts,
            memory_admin=MemoryAdminService(
                settings=make_settings(database.url),
                memories=facts,
                audit=AdminAuditService(database),
                mutations=mutations,
                ledger=ledger,
            ),
        ),
    )
    try:
        assert await sequence(rebuilt) == original
        assert (await facts.get_fact(int(original[1].data["memory_id"].split(":")[1]))).content == (
            "first correction"
        )
        async with database.read_session() as reader:
            assert tuple(await reader.scalars(select(MemoryFactModel.id))) == fact_ids
            assert (
                tuple(await reader.scalars(select(MemoryMutationReceiptModel.mutation_id)))
                == receipts
            )
        rebuilt._approved_permissions = frozenset()
        with rebuilt.bind(invocation), pytest.raises(PluginPermissionError):
            await rebuilt.memory.update(original[0], content="first correction")
    finally:
        await rebuilt.close_host_resources()


async def test_correction_replay_does_not_restore_forgotten_person_material(database, memory_host):
    env, host, invocation, facts, _ = memory_host
    with host.bind(invocation):
        added = await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="private original fact",
            source_type="plugin",
            confidence=0.4,
        )
        original_id = added.data["memory"]["memory_id"]
        corrected = await host.memory.update(original_id, content="private correction")
    assert corrected.ok
    assert await PeopleRepository(database).delete_person_id(env.person)
    with host.bind(invocation):
        result = await host.memory.update(original_id, content="private correction")
    assert not result.ok and result.error_code == "memory.not_found"
    assert "private" not in str(result.data)
    assert await facts.get_fact(int(corrected.data["memory_id"].split(":")[1])) is None
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 0


async def test_source_event_validation_precedes_any_memory_effect(database, memory_host):
    env, host, invocation, _, _ = memory_host
    with host.bind(invocation), pytest.raises(PluginPermissionError, match="current real turn"):
        await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="unproven source",
            source_type="automatic",
            confidence=0.4,
            source_event_ids=(str(invocation.source_event_id + 1),),
        )
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 0


async def test_direct_plugin_new_memory_write_rejects_disabled_original_person(
    database, memory_host
):
    env, host, invocation, facts, _ = memory_host
    with host.bind(invocation):
        first = await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="original confirmed fact",
            source_type="automatic",
            confidence=0.4,
        )
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, env.person)).enabled = False
        with pytest.raises(CanonicalIdentityError):
            await host.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="not a new authorized effect",
                source_type="plugin",
                confidence=0.8,
            )
    assert (
        await facts.get_fact(first.data["memory"]["fact_id"])
    ).content == "original confirmed fact"
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 1


async def test_old_event_receipt_does_not_grant_a_new_plugin_call_replay(database, memory_host):
    env, host, invocation, facts, admin = memory_host
    scheduler = SimpleNamespace(schedule=AsyncMock())
    facts.set_embedding_scheduler(scheduler)
    trigger = MemoryPreferenceTrigger(
        user_id=invocation.actor_user_id,
        person_id=env.person,
        bot_user_id=invocation.bot_user_id,
        trigger_message_id=invocation.inbound.message_id,
        trigger_event_id=invocation.source_event_id,
        canonical_conversation_id=invocation.conversation_id,
        conversation_key=invocation.conversation_key,
        decision_actor_type="plugin",
        decision_actor_id=host.plugin_id,
    )
    original = await admin.add_memory(
        trigger,
        env.person,
        "paid old event-wide fact",
        target_person_id=env.person,
        confidence=0.4,
        request_basis=MemoryMutationRequestBasis.AGENT_INITIATED,
    )
    scheduler.schedule.assert_awaited_once_with(original.id)
    async with database.read_session() as reader:
        receipt = await reader.scalar(select(MemoryMutationReceiptModel))
        frozen = (receipt.id, receipt.idempotency_key, receipt.claim_fingerprint)
    with host.bind(invocation), pytest.raises(ValueError, match="legacy_identity_unknown"):
        await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="paid old event-wide fact",
            source_type="automatic",
            confidence=0.4,
        )
    assert scheduler.schedule.await_count == 1
    assert len(await facts.list_person(person_id=env.person)) == 1
    async with database.read_session() as reader:
        rows = list(await reader.scalars(select(MemoryMutationReceiptModel)))
        assert [(row.id, row.idempotency_key, row.claim_fingerprint) for row in rows] == [frozen]


async def test_other_plugin_receipt_does_not_absorb_new_plugin_call(database, memory_host):
    env, host, invocation, facts, admin = memory_host
    old = MemoryPreferenceTrigger(
        user_id=invocation.actor_user_id,
        person_id=env.person,
        bot_user_id=invocation.bot_user_id,
        trigger_message_id=invocation.inbound.message_id,
        trigger_event_id=invocation.source_event_id,
        conversation_key=invocation.conversation_key,
        decision_actor_type="plugin",
        decision_actor_id="other-plugin",
    )
    await admin.add_memory(old, env.person, "other original call", target_person_id=env.person)
    with host.bind(invocation):
        added = await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="new independently owned call",
            source_type="automatic",
            confidence=0.37,
        )
    assert added.ok
    assert len(await facts.list_person(person_id=env.person)) == 2


async def test_command_and_background_origins_keep_their_actual_provenance(database, memory_host):
    env, host, invocation, facts, admin = memory_host
    command = AdminActor(
        invocation.actor_user_id,
        False,
        "command",
        invocation.conversation_key,
        bot_user_id=invocation.bot_user_id,
        trigger_event_id=invocation.source_event_id,
        person_id=env.person,
        decision_actor_type="command",
    )
    explicit = await admin.add_memory(
        command, env.person, "real command fact", target_person_id=env.person
    )
    assert explicit.source_type.value == explicit.authority.value == "explicit"
    async with database.read_session() as reader:
        receipt = await reader.scalar(select(MemoryMutationReceiptModel))
        assert receipt.decision_actor_type == "command" and receipt.new_fact_id == explicit.id
    with host.bind(invocation):
        independent = await host.memory.add(
            scope_type="person",
            subject_id=env.person,
            content="independent plugin fact",
            source_type="automatic",
            confidence=0.37,
        )
    assert independent.ok and independent.data["memory"]["fact_id"] != explicit.id
    background_event = await _event(
        admin._ledger,
        message_id="background-original-correction",
        sender_user_id=invocation.actor_user_id,
        content="background original source",
    )
    trigger = MemoryPreferenceTrigger(
        user_id=background_event.sender_user_id,
        person_id=env.person,
        bot_user_id=background_event.bot_user_id,
        trigger_message_id=background_event.platform_message_id,
        trigger_event_id=background_event.id,
        conversation_key="private:10001",
        decision_actor_type="plugin",
        decision_actor_id=host.plugin_id,
        operation_id="background:original-correction",
        turn_origin=TurnOrigin.PLUGIN_BACKGROUND.value,
    )
    corrected = await admin.correct_fact(trigger, explicit.id, "background result", confidence=0.62)
    assert corrected is not None
    assert corrected.source_type.value == "automatic"
    assert corrected.authority.value == "self_report"
    assert corrected.confidence == 0.62
    assert any(
        item.relation.value == "correction" and item.confidence == 0.62
        for item in await facts.list_evidence(corrected.id)
    )
