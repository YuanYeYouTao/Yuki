"""Receipt filtering preserves source invalidation and original send lifecycles."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import update
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_pipeline_retirement import _relationship

from yuki.domain.conversations import ScopeType
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel


@pytest.mark.parametrize("status", ["prepared", "executing", "failed", "uncertain"])
async def test_unconfirmed_receipt_preserves_nonfocus_source_correction_before_projection(
    database, tmp_path, monkeypatch, latency_evidence, status
):
    host, item, prior, original, _ = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(
            database, host.ledger, content="synthetic accepted continuation"
        )
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation" and (f"event:{prior.id}", 1) in binding.basis
        admitted = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(admitted)
        await host.on_ordinary_admitted(binding, admitted.admission)
        prior_ref = item.controller.state.events[f"event:{prior.id}"].ref
        receipt_id = str(uuid4())
        async with database.immediate_session() as writer:
            writer.add(
                SocialOperationModel(
                    id=receipt_id,
                    source_turn_id=f"{item.scene.conversation_id}:event:{focus.id}",
                    tool_call_id="synthetic-unconfirmed-send",
                    source_conversation_id=item.scene.conversation_id,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="space",
                    target_id=item.scene.space_id,
                    presence_id=focus.ingress_presence_id,
                    status=status,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    event_id=None,
                )
            )
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == prior.id)
                .values(content="synthetic corrected nonfocus source")
            )
        for index in range(70):
            await seed_event(
                host.ledger,
                bot_user_id="8000",
                origin_key=f"synthetic-later-context-{index}-{uuid4()}",
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content="synthetic unrelated later context",
                group_id=prior.group_id,
                occurred_at=datetime.now(UTC),
            )
        probe = await _event_and_route(database, host.ledger, content="synthetic current question")
        observed_before_hydrate = []
        hydrate = host._hydrate

        async def capture_before(current_item, *args, **kwargs):
            observed_before_hydrate.append(
                current_item.controller.state.invalidated.get(prior_ref.event_id, 0)
            )
            return await hydrate(current_item, *args, **kwargs)

        monkeypatch.setattr(host, "_hydrate", capture_before)
        selected = await host.continuation_for_event(probe.id)
        projected = item.controller.state.events[f"event:{probe.id}"]
        options = tuple(
            (option.thread, option.target, option.self_anchor) for option in projected.unit_options
        )
        pending = (
            tuple(sorted(item.observation.queue.pending)) if item.observation is not None else ()
        )
        saved = await host.ordinary.get(focus.id)
        async with database.read_session() as reader:
            receipt = await reader.get(SocialOperationModel, receipt_id)
        latency_evidence.update(
            boundary="real Host event_scope/SQLite, synthetic unconfirmed receipt, no QQ/provider",
            status=status,
            old_source_invalid_before_hydrate=bool(
                observed_before_hydrate and observed_before_hydrate[0] >= prior_ref.revision
            ),
            old_source_invalid_after_projection=item.controller.state.invalidated.get(
                prior_ref.event_id, 0
            )
            >= prior_ref.revision,
            old_thread_in_options=any(thread == f"event:{prior.id}" for thread, _, _ in options),
            projected_options_count=len(options),
            observation_queue_count=len(pending),
            selected=selected is not None,
            receipt_status=receipt.status,
            original_activation_preserved=(
                (await host.ordinary.get(prior.id)).activation_id
                == original.admission.activation_id
            ),
            continuation_activation_preserved=saved.activation_id
            == admitted.admission.activation_id,
        )
        assert item.controller.state.invalidated.get(prior_ref.event_id, 0) >= prior_ref.revision
        assert selected is None
        assert receipt.status == status and receipt.event_id is None
        assert saved.activation_id == admitted.admission.activation_id
    finally:
        await host.close()
