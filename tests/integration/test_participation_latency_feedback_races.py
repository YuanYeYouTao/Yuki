"""Receipt reconciliation across real SQLite changes, with synthetic traffic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.event_seed import seed_event
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from yuki_participation.participation import ParticipationCheckpoint

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import sync_scope_effects
from yuki.social.db_models import SocialOperationModel


@pytest.mark.parametrize("change", ["none", "body", "mention", "suppression", "generation"])
async def test_two_receipts_recheck_sources_after_hydration(
    database, tmp_path, latency_evidence, change
):
    host, item, event, own, receipt = await ordinary_expression(database, tmp_path)
    try:
        other, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            origin_key=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="synthetic second receipt",
            group_id=event.group_id,
            occurred_at=datetime.now(UTC),
            origin="social_tool",
        )
        extra = SocialOperationModel(
            id=str(uuid4()),
            source_turn_id=receipt.source_turn_id,
            tool_call_id="second-confirmed-send",
            source_conversation_id=receipt.source_conversation_id,
            action="send_message",
            payload_hash="1" * 64,
            target_kind=receipt.target_kind,
            target_id=receipt.target_id,
            presence_id=receipt.presence_id,
            status="succeeded",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            event_id=other.id,
        )
        async with database.immediate_session() as session:
            session.add(extra)

        # Receipt reconciliation can establish expressions before outbound
        # hydration but must not fabricate the corresponding public anchors.
        await sync_scope_effects(host, item)
        before = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert len(before.expressions) == 2
        assert all(expression.anchor is None for expression in before.expressions.values())

        if change != "none":
            async with database.immediate_session() as session:
                if change == "generation":
                    await session.execute(
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == item.scene.conversation_id)
                        .values(generation=item.scene.generation + 1)
                    )
                else:
                    values = (
                        {"content": "corrected synthetic content"}
                        if change == "body"
                        else {
                            "segments_json": json.dumps(
                                [
                                    {
                                        "type": "mention",
                                        "data": {
                                            "kind": "member",
                                            "display_name": "synthetic",
                                            "text_start": 0,
                                            "text_end": 1,
                                            "person_id": event.author_person_id,
                                        },
                                    }
                                ]
                            )
                        }
                        if change == "mention"
                        else {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
                    )
                    await session.execute(
                        update(ChatEventModel).where(ChatEventModel.id == event.id).values(**values)
                    )

        await host._hydrate(item)
        await sync_scope_effects(host, item)
        after = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        anchors = [anchor for unit in after.units.values() for anchor in unit.anchors]
        async with database.read_session() as reader:
            saved = list(
                await reader.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.id.in_([receipt.id, extra.id])
                    )
                )
            )
        latency_evidence.update(
            boundary=(
                "real Host admission/reconciliation/SQLite; synthetic confirmed receipt records"
            ),
            change=change,
            initial_expressions=2,
            final_expressions=len(after.expressions),
            attached_anchors=len(anchors),
            original_receipts_retained=len(saved),
            hydrated_source=f"event:{event.id}" in item.controller.state.events,
            hydrated_outputs=sum(
                f"event:{source.id}" in item.controller.state.events for source in (own, other)
            ),
        )
        assert len(saved) == 2 and all(row.status == "succeeded" for row in saved)
        assert {row.event_id for row in saved} == {own.id, other.id}
        if change == "none":
            assert {anchor.event_id for anchor in anchors} == {
                f"event:{own.id}",
                f"event:{other.id}",
            }
        else:
            assert not anchors, "stale source acquired a new anchor"
    finally:
        await host._store.close()
