"""A newly merged, quiet competing Person participates in writer selection."""

from __future__ import annotations

import time

from tests.conftest import build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_participation_person_identity import append, merge, report
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_pipeline_retirement import _relationship

from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.services.participation_feedback import admission_unit_binding


async def test_new_quiet_competitor_after_preparation_is_rechecked_by_current_family(
    database, tmp_path, latency_evidence
):
    host, item, prior, original, _unit = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic queued input")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        continuation = await host.prepare_continuation(binding)
        other = await append(host, prior, "1002", reply=prior.id)
        other_binding = await host.binding_for_event(other.id)
        other_admission = host.ordinary.prepare(
            other, item.scene.generation, 3, other_binding, reason="mention"
        )
        assert await host.ordinary.commit(other_admission)
        await host.on_ordinary_admitted(other_binding, other_admission.admission)
        other_source = item.controller.state.events[f"event:{other.id}"]
        assert item.controller.observe_unit_hint(
            admission_unit_binding(other_admission.admission),
            report(other_admission.admission, other_source, "quiet"),
        )
        await merge(database, prior.author_person_id, other.author_person_id)
        processor = build_harness(
            database, make_settings(database.url, conversation_semantic_participation_enabled=True)
        ).processor
        processor.set_participation(host)

        async def allowed(writer):
            return await processor._ordinary_admission_allowed(
                prepared, reason, writer, continuation=continuation
            )

        conflict = None
        try:
            selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        except OrdinaryAdmissionConflict as error:
            assert str(error) == "ordinary_participation_preparation_changed"
            conflict = str(error)
            continuation = await host.prepare_continuation(binding)
            selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        assert not selected and await host.ordinary.get(focus.id) is None
        assert (await host.ordinary.get(prior.id)).activation_id == original.admission.activation_id
        assert (
            await host.ordinary.get(other.id)
        ).activation_id == other_admission.admission.activation_id
        latency_evidence.update(
            boundary="real Host/Person merge/Processor writer/SQLite; synthetic inputs",
            original_event_id=prior.id,
            new_competitor_event_id=other.id,
            focus_event_id=focus.id,
            first_commit_conflict=conflict,
            selected=selected,
            original_and_competitor_admissions_preserved=True,
            evaluated_at=time.time(),
        )
    finally:
        await host.close()
