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
from yuki.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing import FakeMemoryFacade


async def host_scene(database, tmp_path):
    env, harness, _chat, source, host = await source_host(
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
    return env, harness, host, invocation, facts, admin


@pytest.mark.parametrize("implementation", ["fake", "host"])
async def test_add_list_correct_delete_share_shapes_and_version_identity(
    database, tmp_path, implementation
):
    env, _, host, invocation, facts, _ = await host_scene(database, tmp_path)
    facade = FakeMemoryFacade() if implementation == "fake" else host.memory
    try:
        with host.bind(invocation):
            added = await facade.add(
                scope_type="person",
                subject_id=env.person,
                content="likes astronomy",
                source_type="automatic",
                confidence=0.37,
                source_event_ids=(str(invocation.source_event_id),),
            )
            original = added.data["memory"]
            original_id = original["memory_id"]
            assert original_id == f"person:{original['fact_id']}"
            assert original["user_id"] == original["subject_user_id"] == env.person
            assert original["confidence"] == 0.37
            assert original["source_type"] == "automatic"
            assert original["authority"] == "self_report"
            assert original["status"] == "active"
            assert (await facade.list_person(env.person))[0]["memory_id"] == original_id
            original_evidence = (
                await facts.list_evidence(original["fact_id"]) if implementation == "host" else ()
            )
            updated = await facade.update(original_id, content="likes physics", confidence=0.62)
            new_id = updated.data["memory_id"]
            assert new_id != original_id and new_id.startswith("person:")
            latest = (await facade.list_person(env.person))[0]
            assert latest["memory_id"] == new_id and latest["confidence"] == 0.62
            assert latest["source_type"] == latest["authority"] == "explicit"
            if implementation == "host":
                old = await facts.get_fact(original["fact_id"])
                assert old.content == "likes astronomy" and old.status.value == "superseded"
                assert await facts.list_evidence(original["fact_id"]) == original_evidence
                evidence = await facts.list_evidence(int(new_id.split(":")[1]))
                correction = next(item for item in evidence if item.relation.value == "correction")
                assert correction.confidence == 0.62
            else:
                assert facade.records[original_id]["content"] == "likes astronomy"
                assert facade.records[original_id]["status"] == "superseded"
            deleted = await facade.delete(new_id)
            assert deleted.ok and deleted.data == {"memory_id": new_id}
            assert await facade.list_person(env.person) == ()
    finally:
        await host.close_host_resources()


async def test_two_calls_and_cold_replay_keep_each_original_identity(database, tmp_path):
    env, _, host, invocation, facts, _ = await host_scene(database, tmp_path)

    async def two_adds():
        ids = []
        with host.bind(invocation):
            for content in ("first independent fact", "second independent fact"):
                result = await host.memory.add(
                    scope_type="person",
                    subject_id=env.person,
                    content=content,
                    source_type="plugin",
                    confidence=0.4,
                )
                ids.append(result.data["memory"]["memory_id"])
        return ids

    try:
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
    finally:
        await host.close_host_resources()


async def test_plugin_producer_is_not_user_intent_and_none_confidence_is_retained(
    database, tmp_path
):
    env, _, host, invocation, facts, _ = await host_scene(database, tmp_path)
    try:
        with host.bind(invocation):
            result = await host.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="passive observed fact",
                source_type="plugin",
                confidence=0.37,
            )
            original = result.data["memory"]
            assert original["source_type"] == "automatic"
            assert original["authority"] == "self_report"
            corrected = await host.memory.update(
                original["memory_id"], content="explicit correction", confidence=None
            )
            current = await facts.get_fact(int(corrected.data["memory_id"].split(":")[1]))
            assert current.confidence == 0.37 and current.source_type.value == "explicit"
            evidence = await facts.list_evidence(current.id)
            assert evidence[-1].confidence == 0.37
    finally:
        await host.close_host_resources()


async def test_source_event_validation_precedes_any_memory_effect(database, tmp_path):
    env, _, host, invocation, _, _ = await host_scene(database, tmp_path)
    try:
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
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("revocation", ["approval", "person"])
async def test_direct_plugin_new_memory_write_observes_current_revocation(
    database, tmp_path, revocation
):
    env, _, host, invocation, facts, _ = await host_scene(database, tmp_path)
    try:
        with host.bind(invocation):
            first = await host.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="original confirmed fact",
                source_type="automatic",
                confidence=0.4,
            )
            if revocation == "approval":
                host._approved_permissions = frozenset({PluginPermission.MEMORY_PERSON_READ})
                expected = PluginPermissionError
            else:
                async with database.immediate_session() as writer:
                    (await writer.get(CanonicalPersonModel, env.person)).enabled = False
                expected = CanonicalIdentityError
            with pytest.raises(expected):
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
            assert (
                await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 1
            )
    finally:
        await host.close_host_resources()


async def test_old_event_receipt_does_not_grant_a_new_plugin_call_replay(database, tmp_path):
    env, _, host, invocation, facts, admin = await host_scene(database, tmp_path)
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
    try:
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
            assert [(row.id, row.idempotency_key, row.claim_fingerprint) for row in rows] == [
                frozen
            ]
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("prior_actor", ["command", "different_plugin"])
async def test_other_original_actor_receipt_does_not_absorb_new_plugin_call(
    database, tmp_path, prior_actor
):
    env, _, host, invocation, facts, admin = await host_scene(database, tmp_path)
    old = MemoryPreferenceTrigger(
        user_id=invocation.actor_user_id,
        person_id=env.person,
        bot_user_id=invocation.bot_user_id,
        trigger_message_id=invocation.inbound.message_id,
        trigger_event_id=invocation.source_event_id,
        conversation_key=invocation.conversation_key,
        decision_actor_type="command" if prior_actor == "command" else "plugin",
        decision_actor_id=invocation.actor_user_id if prior_actor == "command" else "other-plugin",
    )
    try:
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
        async with database.read_session() as reader:
            assert (
                await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 2
            )
    finally:
        await host.close_host_resources()


async def test_command_and_background_origins_keep_their_actual_provenance(database, tmp_path):
    env, _, host, invocation, facts, admin = await host_scene(database, tmp_path)
    try:
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
        corrected = await admin.correct_fact(
            trigger, explicit.id, "background result", confidence=0.62
        )
        assert corrected is not None
        assert corrected.source_type.value == "automatic"
        assert corrected.authority.value == "self_report"
        assert corrected.confidence == 0.62
        assert any(
            item.relation.value == "correction" and item.confidence == 0.62
            for item in await facts.list_evidence(corrected.id)
        )
    finally:
        await host.close_host_resources()
