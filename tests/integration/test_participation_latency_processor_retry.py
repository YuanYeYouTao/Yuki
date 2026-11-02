"""Original Processor retries a rolled-back continuation preparation by original ID."""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_participation_parallel_review import _disable_intrinsic_draw
from tests.support.event_seed import seed_scoped_event
from tests.support.semantic_participation_host_helpers import _host, _item
from tests.unit.test_auto_reply_merge import _wait_for_worker
from tests.unit.test_ordinary_input_handoff import A, _scene
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.database import write_failure_committed
from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import admission_unit_binding


async def test_processor_reprepares_same_continuation_after_basis_conflict(
    database, tmp_path, monkeypatch, latency_evidence
):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    host, _ = await _host(database, tmp_path)
    await _disable_intrinsic_draw(host)
    processor = harness.processor
    processor.set_participation(host)
    sender = MemorySender()
    attempts, conflicts = [], []
    modified = False
    try:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(autonomous_enabled=True)
            )
        prior = await seed_scoped_event(
            env.service.writer,
            scope=message(0, "original").scope(),
            origin_key="synthetic-original-processor-source",
            sender_user_id="10001",
            direction="inbound",
            content="synthetic original",
        )
        item = await _item(host, prior.event)
        binding = await host.binding_for_event(prior.event.id)
        admitted = host.ordinary.prepare(
            prior.event, item.scene.generation, 1, binding, reason="mention"
        )
        assert await host.ordinary.commit(admitted)
        await host.on_ordinary_admitted(binding, admitted.admission)
        unit = admission_unit_binding(admitted.admission)
        source = item.controller.state.events[f"event:{prior.event.id}"]
        assert item.controller.observe_unit_hint(
            unit,
            SelfReport(
                run_ref=admitted.admission.activation_id,
                sequence=1,
                response_id="synthetic-stay",
                at=source.at,
                source=source.ref,
                delta=SelfDelta(engage="stay"),
            ),
        )
        await host._save(item)
        original_commit = processor.ordinary_admissions.commit

        async def inject_after_preparation(prepared, *args, **kwargs):
            nonlocal modified
            # The repository's owned-session implementation calls itself once
            # recursively; count the Processor's outer attempts only.
            if kwargs.get("session") is not None:
                return await original_commit(prepared, *args, **kwargs)
            attempts.append((prepared.admission.event_id, prepared.admission.activation_id))
            if not modified:
                assert prepared.admission.reason == "continuation"
                assert (
                    f"event:{prior.event.id}",
                    source.ref.revision,
                ) in prepared.admission.binding.basis
                modified = True
                async with database.immediate_session() as writer:
                    await writer.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == prior.event.id)
                        .values(content="synthetic source corrected after preparation")
                    )
            try:
                return await original_commit(prepared, *args, **kwargs)
            except OrdinaryAdmissionConflict as error:
                conflicts.append((str(error), write_failure_committed(error)))
                raise

        monkeypatch.setattr(processor.ordinary_admissions, "commit", inject_after_preparation)
        assert (await processor.handle(message(1, A), sender)).reason == "ordinary_deferred"
        key = next(iter(worker._states))
        await asyncio.wait_for(_wait_for_worker(worker, key), 30)
        async with database.read_session() as reader:
            focus = await reader.scalar(
                select(ChatEventModel).where(ChatEventModel.origin_key == "open-1")
            )
        assert focus is not None
        new_admission = await host.ordinary.get(focus.id)
        old = await host.ordinary.get(prior.event.id)
        latency_evidence.update(
            boundary="real Processor/Host/SQLite and async worker; fake provider/sender",
            original_source_event_id=prior.event.id,
            focus_event_id=focus.id,
            original_activation_id=admitted.admission.activation_id,
            attempt_ids=attempts,
            conflict_results=conflicts,
            new_admission=new_admission is not None,
            provider_requests=len(provider.requests),
            simulated_sends=sender.calls,
        )
        assert modified and old.activation_id == admitted.admission.activation_id
        # A fresh selection may discover the source is still invalid before
        # another commit is attempted; any attempts retain the original IDs.
        assert attempts and len(set(attempts)) == 1
        assert conflicts == [("ordinary_participation_preparation_changed", False)]
        assert new_admission is None and not provider.requests and sender.calls == 0
    finally:
        await host.close()
