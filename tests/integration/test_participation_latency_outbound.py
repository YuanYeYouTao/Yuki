"""Host/SQLite outbound-thread consumers; synthetic filler keys, no QQ/provider."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.support.event_seed import seed_event
from tests.unit.test_participation_pipeline_retirement import _relationship

from yuki.domain.conversations import ScopeType
from yuki.services.participation_feedback import sync_scope_effects
from yuki.social.db_models import SocialOperationModel

pytestmark = pytest.mark.asyncio


class MappingProbe(dict):
    """Observe a full dict() copy of the existing Host-owned map."""

    copies = 0

    def keys(self):
        self.copies += 1
        return super().keys()

    def __iter__(self):
        return super().__iter__()


async def test_unchanged_sync_does_not_copy_outbound_mapping(database, tmp_path):
    host, item, _source, _admission, _unit = await _relationship(database, tmp_path)
    try:
        existing = MappingProbe({"event:90000001": "original-thread"})
        item.controller.set_state_entry("host_checkpoint", "outbound_threads", existing)
        item.controller.take_changes()
        await sync_scope_effects(host, item, rows=[], apply_committed=False)
        assert existing.copies == 0, "unchanged sync copied the entire mapping"
        assert item.controller.state.host_checkpoint["outbound_threads"] is existing
        assert item.controller.take_changes() == (False, False)
    finally:
        await host.close()


async def test_retained_real_receipt_thread_survives_synthetic_1024_other_keys(database, tmp_path):
    host, item, source, _admission, unit = await _relationship(database, tmp_path)
    try:
        output, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            origin_key=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="synthetic confirmed original output",
            group_id="2001",
            occurred_at=datetime.now(UTC),
            origin="social_tool",
        )
        receipt_id = str(uuid4())
        async with database.immediate_session() as writer:
            writer.add(
                SocialOperationModel(
                    id=receipt_id,
                    source_turn_id=f"{item.scene.conversation_id}:event:{source.id}",
                    tool_call_id="synthetic-original-confirmed-send",
                    source_conversation_id=item.scene.conversation_id,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="space",
                    target_id=item.scene.space_id,
                    presence_id=source.ingress_presence_id,
                    status="succeeded",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    event_id=output.id,
                )
            )
        await sync_scope_effects(host, item)
        key = f"event:{output.id}"
        assert item.controller.state.host_checkpoint["outbound_threads"][key] == unit.unit.thread
        before = host._event(output, item)
        assert before is not None and before.thread == unit.unit.thread
        assert key not in item.controller.state.events  # Next call still projects the real row.

        # The 1024 other keys are synthetic; the original key above came from
        # the real Host/SQLite ordinary admission and simulated success receipt.
        for index in range(1024):
            item.controller.set_host_mapping_entry(
                "outbound_threads", f"event:{90000000 + index}", f"synthetic-thread-{index}"
            )
        await sync_scope_effects(host, item, rows=[], apply_committed=False)
        after = host._event(output, item)
        assert after is not None and after.thread == unit.unit.thread
        assert item.controller.state.host_checkpoint["outbound_threads"][key] == unit.unit.thread
        assert (
            await host.ordinary.get(source.id)
        ).activation_id == _admission.admission.activation_id
    finally:
        await host.close()


async def test_same_output_last_original_receipt_wins_without_false_dirty(database, tmp_path):
    host, item, first, _admission, first_unit = await _relationship(database, tmp_path)
    try:
        second, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            origin_key=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            direction="inbound",
            content="synthetic independent source",
            group_id="2001",
            occurred_at=datetime.now(UTC),
        )
        binding = await host.binding_for_event(second.id)
        prepared = host.ordinary.prepare(
            second, item.scene.generation, 2, binding, reason="mention"
        )
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(binding, prepared.admission)
        assert binding.unit_key != first_unit.unit.thread
        output, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            origin_key=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="synthetic output referenced by two receipts",
            group_id="2001",
            occurred_at=datetime.now(UTC),
            origin="social_tool",
        )
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            for ordinal, source in enumerate((first, second)):
                at = now - timedelta(seconds=ordinal)
                writer.add(
                    SocialOperationModel(
                        id=str(uuid4()),
                        source_turn_id=f"{item.scene.conversation_id}:event:{source.id}",
                        tool_call_id=f"synthetic-conflicting-receipt-{ordinal}",
                        source_conversation_id=item.scene.conversation_id,
                        action="send_message",
                        payload_hash="0" * 64,
                        target_kind="space",
                        target_id=item.scene.space_id,
                        presence_id=source.ingress_presence_id,
                        status="succeeded",
                        created_at=at,
                        updated_at=at,
                        event_id=output.id,
                    )
                )
        await sync_scope_effects(host, item)
        key = f"event:{output.id}"
        assert item.controller.state.host_checkpoint["outbound_threads"][key] == binding.unit_key
        await host._save(item)
        item.controller.take_changes()
        await sync_scope_effects(host, item)
        assert item.controller.state.host_checkpoint["outbound_threads"][key] == binding.unit_key
        assert item.controller.take_changes() == (False, False)
        host._sessions.clear()
        reopened = await host._session(item.scene)
        assert (
            reopened.controller.state.host_checkpoint["outbound_threads"][key] == binding.unit_key
        )
        projected = host._event(output, reopened)
        assert projected is not None and projected.thread == binding.unit_key
        assert (
            await host.ordinary.get(first.id)
        ).activation_id == _admission.admission.activation_id
        assert (
            await host.ordinary.get(second.id)
        ).activation_id == prepared.admission.activation_id
    finally:
        await host.close()
