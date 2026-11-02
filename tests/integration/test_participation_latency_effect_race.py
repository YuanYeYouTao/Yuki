"""In-flight writer must recheck the actual effect behind an expression association."""

from __future__ import annotations

import pytest
from tests.conftest import build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.persistence.database import write_failure_committed
from yuki.services.participation_feedback import admission_unit_binding, sync_scope_effects
from yuki.social.db_models import SocialOperationModel


@pytest.mark.parametrize("change", ["receipt_replayed", "effect_removed"])
async def test_writer_reprepares_when_associated_effect_changes(
    database, tmp_path, latency_evidence, change
):
    host, item, original, _own, receipt = await ordinary_expression(database, tmp_path)
    try:
        await sync_scope_effects(host, item)
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        effect_id, association = next(iter(checkpoint.expressions.items()))
        assert effect_id in item.controller.state.effects
        admission = await host.ordinary.get(original.id)
        source = item.controller.state.events[f"event:{original.id}"]
        assert item.controller.observe_unit_hint(
            admission_unit_binding(admission),
            SelfReport(
                run_ref=admission.activation_id,
                sequence=1,
                response_id="synthetic-stay",
                at=source.at,
                source=source.ref,
                delta=SelfDelta(engage="stay"),
            ),
        )
        focus = await _event_and_route(database, host.ledger, content="synthetic next continuation")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        if change == "receipt_replayed":
            item.controller._pop("effects", effect_id)
        continuation = await host.prepare_continuation(binding)
        if change == "receipt_replayed":
            await sync_scope_effects(host, item)
            assert effect_id in item.controller.state.effects
        else:
            item.controller._pop("effects", effect_id)
        after = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert after.expressions[effect_id] == association
        processor = build_harness(
            database, make_settings(database.url, conversation_semantic_participation_enabled=True)
        ).processor
        processor.set_participation(host)

        async def allowed(writer):
            return await processor._ordinary_admission_allowed(
                prepared, reason, writer, continuation=continuation
            )

        with pytest.raises(
            OrdinaryAdmissionConflict, match="ordinary_participation_preparation_changed"
        ) as caught:
            await host.ordinary.commit(prepared, admission_allowed=allowed)
        assert write_failure_committed(caught.value) is False
        assert await host.ordinary.get(focus.id) is None
        continuation = await host.prepare_continuation(binding)
        assert await host.ordinary.commit(prepared, admission_allowed=allowed)
        current = await host.ordinary.get(focus.id)
        async with database.read_session() as reader:
            saved = await reader.get(SocialOperationModel, receipt.id)
        assert current.activation_id == prepared.admission.activation_id
        assert saved.status == "succeeded" and saved.event_id == receipt.event_id
        latency_evidence.update(
            boundary=(
                "real Host/Processor/SQLite and original simulated receipt; "
                "controlled derived-effect mutation"
            ),
            change=change,
            original_event_id=original.id,
            focus_event_id=focus.id,
            original_receipt_id=receipt.id,
            association_unchanged=True,
            rollback_confirmed=True,
            same_prepared_activation_id=True,
            original_receipt_preserved=True,
        )
    finally:
        await host.close()
