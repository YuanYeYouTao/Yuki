"""Formal MemoryAdmin preserves evidence, rollback and missing-fact results."""

from dataclasses import replace

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_memory_mutation import _event, _service
from tests.unit.test_memory_rebuild import _admin

from yuki.admin.audit import AdminAuditService
from yuki.admin.models import AdminActor
from yuki.memory.mutation.models import MemoryMutationRequestBasis
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger


def trigger(source, operation_id):
    return MemoryPreferenceTrigger(
        user_id=source.sender_user_id,
        person_id=source.author_person_id,
        bot_user_id=source.bot_user_id,
        trigger_message_id=source.platform_message_id,
        trigger_event_id=source.id,
        conversation_key="private:1001",
        decision_actor_type="plugin",
        decision_actor_id="original-plugin",
        operation_id=operation_id,
    )


def admin_for(database):
    mutations, facts, ledger, _ = _service(database)
    return (
        MemoryAdminService(
            settings=make_settings(database.url),
            memories=facts,
            audit=AdminAuditService(database),
            mutations=mutations,
            ledger=ledger,
        ),
        facts,
        ledger,
    )


@pytest.mark.parametrize("operation", ["correct", "invalidate", "restore", "merge"])
async def test_missing_fact_keeps_original_noop_without_secondary_write(database, operation):
    admin, _, _ = admin_for(database)
    actor = AdminActor("9000", True, "missing", "private:9000", person_id=await _admin(database))
    if operation == "correct":
        assert await admin.correct_fact(actor, 999999, "correction") is None
    elif operation == "invalidate":
        assert await admin.invalidate_fact(actor, 999999) is False
    elif operation == "restore":
        assert await admin.restore_fact(actor, 999999) is None
    else:
        assert await admin.merge_facts(actor, 999999, 999998) is None
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 0
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )


@pytest.mark.parametrize("rollback", [False, True])
async def test_correction_copies_other_event_evidence_and_rolls_back_as_one_unit(
    database, rollback
):
    admin, facts, ledger = admin_for(database)
    source = await _event(
        ledger, message_id="first-fact", sender_user_id="1001", content="source one"
    )
    original = await admin.add_memory(
        trigger(source, "original:add"),
        "1001",
        "old fact",
        target_person_id=source.author_person_id,
        confidence=0.37,
        request_basis=MemoryMutationRequestBasis.AGENT_INITIATED,
    )
    original_evidence = await facts.list_evidence(original.id)
    correction = await _event(
        ledger, message_id="correct-fact", sender_user_id="1001", content="source correction"
    )

    def fail_after_evidence(_conn, _cursor, sql, *_args):
        if sql.startswith("INSERT INTO memory_evidence "):
            raise RuntimeError("original evidence commit failure")

    if rollback:
        sql_event.listen(database.engine.sync_engine, "after_cursor_execute", fail_after_evidence)
        try:
            with pytest.raises(RuntimeError, match="original evidence commit failure"):
                await admin.correct_fact(
                    trigger(correction, "original:correct"),
                    original.id,
                    "new fact",
                    confidence=0.62,
                )
        finally:
            sql_event.remove(
                database.engine.sync_engine, "after_cursor_execute", fail_after_evidence
            )
        assert await facts.get_fact(original.id) == original
        async with database.read_session() as reader:
            assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 1
            assert (
                await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 1
            )
    else:
        revised = await admin.correct_fact(
            trigger(correction, "original:correct"), original.id, "new fact", confidence=0.62
        )
        assert revised.id != original.id and revised.confidence == 0.62
        evidence = {item.event_id: item for item in await facts.list_evidence(revised.id)}
        assert set(evidence) == {source.id, correction.id}
        assert evidence[source.id].confidence == 0.37
        assert evidence[correction.id].confidence == 0.62
        assert evidence[correction.id].relation.value == "correction"
    assert await facts.list_evidence(original.id) == original_evidence


async def test_legacy_receipt_arriving_after_prepare_is_read_in_original_writer(
    database, monkeypatch
):
    admin, facts, ledger = admin_for(database)
    source = await _event(
        ledger, message_id="late-old-receipt", sender_user_id="1001", content="old"
    )
    prepare = admin._mutations._prepare
    injected = False

    async def prepare_then_finish_old_call(*args, **kwargs):
        nonlocal injected
        prepared = await prepare(*args, **kwargs)
        if not injected:
            injected = True
            await admin.add_memory(
                replace(trigger(source, "unused"), operation_id=None),
                "1001",
                "old confirmed fact",
                target_person_id=source.author_person_id,
            )
        return prepared

    monkeypatch.setattr(admin._mutations, "_prepare", prepare_then_finish_old_call)
    with pytest.raises(ValueError, match="legacy_identity_unknown"):
        await admin.add_memory(
            trigger(source, "new:original-call"),
            "1001",
            "must not replay old effect",
            target_person_id=source.author_person_id,
        )
    rows = await facts.list_person(person_id=source.author_person_id)
    assert len(rows) == 1 and rows[0].content == "old confirmed fact"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 1
        )
