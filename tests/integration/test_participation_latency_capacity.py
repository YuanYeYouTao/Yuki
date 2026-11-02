"""Processor admissions retain live relationship consumers beyond old count limits."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.commands_and_chat_helpers import inbound
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route, _host, _item
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.domain.conversations import ScopeType
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import sync_scope_effects
from yuki.social.db_models import SocialOperationModel


async def test_65_actual_mention_admissions_keep_original_live_receipt(
    database, tmp_path, latency_evidence
):
    provider = FakeLLMProvider(lambda _request: "NO_REPLY")
    harness = build_harness(
        database,
        make_settings(
            database.url,
            conversation_semantic_participation_enabled=True,
            per_group_requests_per_minute=100,
        ),
        provider,
    )
    host, _ = await _host(database, tmp_path, observer=False)
    await _event_and_route(database, host.ledger)
    harness.processor.set_participation(host)
    sender = MemorySender()
    admissions = []
    item = None
    receipt_id = None
    try:
        for index in range(65):
            message_id = f"capacity-direct-{index}"
            source = replace(
                inbound(
                    "@Yuki synthetic ordinary question",
                    message_id=message_id,
                    user_id=str(3000 + index),
                    group_id="2001",
                    mentions_bot=True,
                ),
                bot_user_id="8000",
            )
            result = await harness.processor.handle(source, sender)
            async with database.read_session() as reader:
                row = await reader.scalar(
                    select(ChatEventModel).where(ChatEventModel.origin_key == message_id)
                )
            assert row is not None and result.handled
            admission = await host.ordinary.get(row.id)
            latency_evidence.update(
                last_index=index,
                handler_reason=result.reason,
                provider_requests=len(provider.requests),
                last_admission_present=admission is not None,
            )
            assert admission is not None and admission.actor_person_id == row.author_person_id
            assert admission.reason == "group_triggered"
            admissions.append((row.id, admission.activation_id, row.author_person_id))
            if index == 0:
                first = await host.ledger.get_event(row.id)
                item = await _item(host, first)
                own, _ = await seed_event(
                    host.ledger,
                    bot_user_id="8000",
                    origin_key=str(uuid4()),
                    scope_type=ScopeType.GROUP,
                    sender_user_id="8000",
                    direction="outbound",
                    content="synthetic original confirmed effect",
                    group_id="2001",
                    occurred_at=datetime.now(UTC),
                    origin="social_tool",
                )
                receipt_id = str(uuid4())
                async with database.immediate_session() as writer:
                    writer.add(
                        SocialOperationModel(
                            id=receipt_id,
                            source_turn_id=f"{item.scene.conversation_id}:event:{row.id}",
                            tool_call_id="synthetic-capacity-first-send",
                            source_conversation_id=item.scene.conversation_id,
                            action="send_message",
                            payload_hash="0" * 64,
                            target_kind="space",
                            target_id=item.scene.space_id,
                            presence_id=row.ingress_presence_id,
                            status="succeeded",
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                            event_id=own.id,
                        )
                    )
                await sync_scope_effects(host, item)

        assert item is not None and receipt_id is not None
        await host._hydrate(item)
        checkpoint = item.controller._participation_checkpoint()
        first_id, first_activation, first_person = admissions[0]
        first_units = [
            unit
            for unit in checkpoint.units.values()
            if unit.binding.actor == first_person and unit.input_ref.event_id == f"event:{first_id}"
        ]
        latency_evidence.update(
            boundary=(
                "65 real local Processor mention admissions, distinct canonical Persons, "
                "one simulated successful effect receipt; fake model/sender, no QQ"
            ),
            actual_admissions=len(admissions),
            configured_group_rate_per_minute=100,
            distinct_persons=len({person for _, _, person in admissions}),
            live_units_after_65=len(checkpoint.units),
            original_unit_present=bool(first_units),
            stored_expressions=len(checkpoint.expressions),
        )
        assert len(provider.requests) == 65 and sender.calls == 0
        assert len(checkpoint.units) == 65 and len(first_units) == 1
        assert checkpoint.expressions
        first_unit = first_units[0]
        first_ref = first_unit.input_ref
        first_event = item.controller.state.events[first_ref.event_id]
        assert item.controller.observe_unit_hint(
            first_unit.binding,
            SelfReport(
                run_ref=first_activation,
                sequence=2,
                response_id="synthetic-capacity-late-quiet",
                at=first_event.at,
                source=first_ref,
                delta=SelfDelta(engage="quiet"),
            ),
        )
        await host._save(item)
        host._sessions.clear()
        reopened = await host._session(item.scene)
        await host._hydrate(reopened)
        recovered = reopened.controller._participation_checkpoint()
        assert len(recovered.units) == 65
        restored = next(
            unit for unit in recovered.units.values() if unit.binding.actor == first_person
        )
        assert restored.hint is not None and restored.hint.delta.engage == "quiet"
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == first_id)
                .values(suppression_status="duplicate", utterance_fingerprint="f" * 64)
            )
        assert not (await host._sources_current(reopened, (first_ref,)))[first_ref]
        assert (await host.ordinary.get(first_id)).activation_id == first_activation
        async with database.read_session() as reader:
            receipt = await reader.get(SocialOperationModel, receipt_id)
        assert receipt is not None and receipt.status == "succeeded"
        latency_evidence.update(
            original_activation_preserved_after_source_withdrawal=True,
            original_simulated_receipt_preserved=True,
            quiet_survived_restore=True,
        )
    finally:
        await host.close()
