"""Completed Memory mutations do not acquire a second administrative writer."""

from dataclasses import replace
from unittest.mock import AsyncMock

from sqlalchemy import select
from tests.conftest import make_settings
from tests.unit.test_memory_mutation import _event, _service

from yuki.admin.audit import AdminAuditService
from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.authority.targets import PersonControlTarget
from yuki.admin.models import AdminActor, ControlAuditRef
from yuki.domain.control import DecisionContext
from yuki.domain.identity import PersonId, PrincipalId, RequestId
from yuki.persistence.models import MemoryMutationReceiptModel
from yuki.services.admin.memory_admin import MemoryAdminService
from yuki.services.admin.preference_admin import PreferenceAdminService


async def test_memory_and_preference_mutations_keep_their_original_receipts(database, monkeypatch):
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="admin-original-receipt",
        sender_user_id="1001",
        content="explicit memory",
    )
    audit = AdminAuditService(database)
    duplicate_audit = AsyncMock(side_effect=OSError("secondary audit unavailable"))
    monkeypatch.setattr(audit, "record", duplicate_audit)
    admin = MemoryAdminService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        memories=facts,
        audit=audit,
        mutations=service,
        ledger=ledger,
    )
    actor = AdminActor(
        user_id="1001",
        is_superuser=False,
        trigger_message_id=event.origin_key,
        trigger_event_id=event.id,
        conversation_key="private:1001",
        bot_user_id=event.bot_user_id,
    )
    row = await admin.add_memory(actor, "1001", "我喜欢天文")
    event = await _event(
        ledger, message_id="admin-correct", sender_user_id="1001", content="correct"
    )
    actor = replace(actor, trigger_event_id=event.id, trigger_message_id=event.origin_key)
    assert await admin.update_memory(actor, "1001", row.id, "我喜欢物理")
    async with database.read_session() as reader:
        receipts = list(
            await reader.scalars(
                select(MemoryMutationReceiptModel).order_by(MemoryMutationReceiptModel.id)
            )
        )
    updated_id = receipts[-1].new_fact_id
    assert updated_id is not None
    event = await _event(ledger, message_id="admin-delete", sender_user_id="1001", content="delete")
    actor = replace(actor, trigger_event_id=event.id, trigger_message_id=event.origin_key)
    assert await admin.delete_memory(actor, "1001", updated_id)

    person = PersonId.parse(event.author_person_id)
    principal = ControlPrincipal(
        principal_id=PrincipalId.parse(person.text),
        person_id=person,
        source=PrincipalSource.PLATFORM,
        authenticated=True,
        active=True,
    )
    context = DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=PrincipalSource.PLATFORM,
        canonical_target=PersonControlTarget(person_id=person, storage_user_id="1001"),
    )
    event = await _event(ledger, message_id="preference-set", sender_user_id="1001", content="set")
    correlation = ControlAuditRef(
        user_id="1001",
        trigger_message_id=event.origin_key,
        trigger_event_id=event.id,
        conversation_key="private:1001",
        bot_user_id=event.bot_user_id,
    )
    preferences = PreferenceAdminService(memory_mutations=admin)
    preference = await preferences.set_preference(context, "format", "短一点", audit=correlation)
    assert preference.content == "短一点"
    event = await _event(
        ledger, message_id="preference-delete", sender_user_id="1001", content="delete"
    )
    correlation = replace(
        correlation, trigger_event_id=event.id, trigger_message_id=event.origin_key
    )
    assert await preferences.delete_preference(context, "format", audit=correlation)
    duplicate_audit.assert_not_awaited()
    async with database.read_session() as reader:
        receipts = list(await reader.scalars(select(MemoryMutationReceiptModel)))
    assert len(receipts) == 5
    assert len({receipt.trigger_event_id for receipt in receipts}) == 5
