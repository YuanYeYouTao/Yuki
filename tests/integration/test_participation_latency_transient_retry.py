"""Real Processor retries temporarily invalid participation sources by original ID."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_participation_parallel_review import _disable_intrinsic_draw
from tests.support.event_seed import seed_scoped_event
from tests.support.semantic_participation_host_helpers import _host, _item
from tests.unit import test_memory_mutation as memory_case
from tests.unit.test_auto_reply_merge import _wait_for_worker
from tests.unit.test_ordinary_input_handoff import A, _scene
from yuki_participation.controller import State
from yuki_participation.models import SourceRef
from yuki_participation.self_report import SelfDelta, SelfReport
from yuki_participation.snapshot import freeze

from yuki.conversation.initiative_sources import memory_revision
from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.enums import MemoryKind, MemoryScopeType
from yuki.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
    SelfMemoryVisibilityMode,
)
from yuki.persistence.database import write_failure_committed
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel
from yuki.services.participation_feedback import admission_unit_binding


@pytest.mark.parametrize("source_kind", ["event", "memory"])
@pytest.mark.parametrize("restore_before_writer", [True, False])
async def test_processor_reselects_temporarily_invalid_basis(
    database, tmp_path, monkeypatch, latency_evidence, source_kind, restore_before_writer
):
    provider = FakeLLMProvider(lambda _request: "NO_REPLY")
    env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    host, _ = await _host(database, tmp_path)
    await _disable_intrinsic_draw(host)
    processor = harness.processor
    processor.set_participation(host)
    sender = MemorySender()
    attempts, conflicts, prepared_once = [], [], []
    try:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(autonomous_enabled=True)
            )
        memory = None
        memory_source = None
        digest = None
        if source_kind == "memory":
            mutations, facts, ledger, _ = memory_case._service(database, self_memory_enabled=True)
            memory_source = await memory_case._event(
                ledger,
                message_id="transient-worker-memory-source",
                sender_user_id="10001",
                content="我觉得你回答复杂问题时更喜欢先想清楚再说",
                group_id="20001",
                bot_user_id="80001",
            )
            result = await mutations.mutate(
                MemoryMutationRequest(
                    operation=MemoryMutationOperation.CREATE,
                    target=MemoryMutationTarget(
                        subject_ref="self", scope_type=MemoryScopeType.SELF
                    ),
                    visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
                    new_content="面对复杂问题时，我偏好先想清楚再回答",
                    memory_key="preference:worker-transient",
                    category="self_preference",
                    kind=MemoryKind.PREFERENCE,
                    reason="根据当前用户反馈形成自我判断",
                    confidence=0.82,
                ),
                memory_case._context(memory_source),
            )
            assert result.ok and result.new_fact_id is not None
            memory = SourceRef(event_id=f"memory:{result.new_fact_id}", revision=1)
            digest = memory_revision(await facts.get_fact(result.new_fact_id))
            async with database.read_session() as reader:
                evidence = await reader.scalar(
                    select(MemoryEvidenceModel).where(
                        MemoryEvidenceModel.fact_id == result.new_fact_id
                    )
                )
                assert evidence.event_id == memory_source.id

        prior = await seed_scoped_event(
            env.service.writer,
            scope=message(0, "original").scope(),
            origin_key="synthetic-transient-worker-source",
            sender_user_id="10001",
            direction="inbound",
            content="synthetic original",
        )
        item = await _item(host, prior.event)
        binding = await host.binding_for_event(prior.event.id)
        original_event = item.controller.state.events[f"event:{prior.event.id}"]
        if memory is not None:
            binding = replace(
                binding,
                basis=(
                    (original_event.ref.event_id, original_event.ref.revision),
                    (memory.event_id, memory.revision),
                ),
            )
        admitted = host.ordinary.prepare(
            prior.event, item.scene.generation, 1, binding, reason="mention"
        )
        assert await host.ordinary.commit(admitted)
        if memory is None:
            await host.on_ordinary_admitted(binding, admitted.admission)
        unit = admission_unit_binding(admitted.admission)
        hint = SelfReport(
            run_ref=admitted.admission.activation_id,
            sequence=1,
            response_id="synthetic-stay",
            at=original_event.at,
            source=original_event.ref if memory is None else None,
            delta=SelfDelta(engage="stay"),
        )
        if memory is None:
            assert item.controller.observe_unit_hint(unit, hint)
            await host._save(item)
        else:
            payload = item.controller.state.model_dump(mode="json")
            payload["events"][memory.event_id] = {
                "scope": item.scene.scope.model_dump(mode="json"),
                "ref": memory.model_dump(mode="json"),
                "thread": memory.event_id,
                "author": "SELF",
                "target": "group",
                "text": "synthetic legacy memory dependency",
                "at": time.time(),
                "kind": "seed",
            }
            payload["host_checkpoint"]["source_versions"][memory.event_id] = [1, digest]
            payload["host_checkpoint"]["participation_v1"] = {
                "version": 1,
                "units": {
                    item.controller._unit_key(binding.unit_key, binding.target_hint): {
                        "binding": unit.model_dump(mode="json"),
                        "last_at": original_event.at,
                        "input_ref": original_event.ref.model_dump(mode="json"),
                        "hint": hint.model_dump(mode="json"),
                        "hint_basis": [
                            original_event.ref.model_dump(mode="json"),
                            memory.model_dump(mode="json"),
                        ],
                    }
                },
                "expressions": {},
            }
            await host._store.save(
                freeze(State.model_validate(payload)), expected_revision=item.revision
            )
            host._sessions.clear()
            item = await host._session(item.scene)
            await host._hydrate(item)
            await host._validate_boundaries(item)

        changed_event = prior.event if memory_source is None else memory_source
        async with database.read_session() as reader:
            row = await reader.get(ChatEventModel, changed_event.id)
            original_values = {
                "content": row.content,
                "suppression_status": row.suppression_status,
                "utterance_fingerprint": row.utterance_fingerprint,
            }
        prepare = host.prepare_continuation

        async def transient_prepare(new_binding):
            if prepared_once:
                return await prepare(new_binding)
            assert (f"event:{prior.event.id}", original_event.ref.revision) in new_binding.basis
            if memory is not None:
                assert (memory.event_id, memory.revision) in new_binding.basis
            before = item.controller.state.model_dump_json()
            values = (
                {"content": "synthetic transient source change"}
                if source_kind == "event"
                else {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
            )
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == changed_event.id)
                    .values(**values)
                )
            result = await prepare(new_binding)
            assert item.controller.state.model_dump_json() == before
            prepared_once.append(True)
            if restore_before_writer:
                async with database.immediate_session() as writer:
                    await writer.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == changed_event.id)
                        .values(**original_values)
                    )
            return result

        monkeypatch.setattr(host, "prepare_continuation", transient_prepare)
        commit = processor.ordinary_admissions.commit

        async def traced_commit(prepared, *args, **kwargs):
            if kwargs.get("session") is not None:
                return await commit(prepared, *args, **kwargs)
            attempts.append((prepared.admission.event_id, prepared.admission.activation_id))
            try:
                return await commit(prepared, *args, **kwargs)
            except OrdinaryAdmissionConflict as error:
                conflicts.append((str(error), write_failure_committed(error)))
                raise

        monkeypatch.setattr(processor.ordinary_admissions, "commit", traced_commit)
        outcome = await processor.handle(message(1, A), sender)
        assert outcome.reason == "ordinary_deferred"
        key = next(iter(worker._states))
        await asyncio.wait_for(_wait_for_worker(worker, key), 30)
        async with database.read_session() as reader:
            focus = await reader.scalar(
                select(ChatEventModel).where(ChatEventModel.origin_key == "open-1")
            )
        persisted = await host.ordinary.get(focus.id)
        old = await host.ordinary.get(prior.event.id)
        latency_evidence.update(
            boundary=(
                "real Processor worker/Host/SQLite with fake model and sender; "
                "test SQL changes canonical event fields, no QQ/provider"
            ),
            source_kind=source_kind,
            restore_before_writer=restore_before_writer,
            focus_event_id=focus.id,
            attempts=attempts,
            conflicts=conflicts,
            admitted=persisted is not None,
            provider_requests=len(provider.requests),
            simulated_sends=sender.calls,
        )
        assert prepared_once and old.activation_id == admitted.admission.activation_id
        assert conflicts == [("ordinary_participation_preparation_changed", False)]
        assert attempts and len(set(attempts)) == 1 and attempts[0][0] == focus.id
        if restore_before_writer:
            assert persisted is not None and persisted.activation_id == attempts[0][1]
            assert len(provider.requests) == 1 and sender.calls == 0
        else:
            assert persisted is None and not provider.requests and sender.calls == 0
    finally:
        await host.close()
