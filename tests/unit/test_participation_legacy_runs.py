"""Retired memory proposals recover real Host admission without creating new work."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import update
from tests.support.event_seed import seed_event
from tests.support.self_initiative_memory_quality_helpers import reflection_fact
from tests.support.semantic_participation_host_helpers import (
    _event_and_route,
    _host,
    _item,
    _observation,
)
from tests.unit.test_participation_feedback import social
from yuki_participation.models import CandidateKind, Proposal, Snapshot, SourceRef, Support
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
from yuki.conversation.initiative_sources import memory_revision
from yuki.domain.conversations import ScopeType
from yuki.memory.self_origin import read_self_initiative_facts
from yuki.persistence.models import MemoryEvidenceModel, MemoryFactModel
from yuki.runtime.work_schema_v1 import work
from yuki.services.participation_feedback import (
    admission_unit_binding,
    reconcile_page,
    sync_scope_effects,
)
from yuki.social.db_models import SocialOperationModel


@pytest.mark.parametrize("accepted", [False, True])
async def test_expired_legacy_pending_queries_original_admission_before_retirement(
    database, tmp_path, monkeypatch, accepted
):
    _facts, source, _reflection_run, _batch, fact_id = await reflection_fact(database)
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        event = await _event_and_route(database, host.ledger, group=source.group_id)
        item = await _item(host, event)
        binding = await host._binding(item)
        (fact,) = await read_self_initiative_facts(
            database, canonical_conversation_id=item.scene.conversation_id, fact_ids=(fact_id,)
        )
        now = time.time()
        ref = SourceRef(event_id=f"memory:{fact_id}", revision=1)
        proposal = Proposal(
            proposal_id="synthetic-legacy-recall",
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.RECALL,
            thread=ref.event_id,
            target_hint="group",
            sources=(ref,),
            support=Support(
                kind="observed",
                scope=item.scene.scope,
                thread=ref.event_id,
                target="group",
                observation_id="synthetic-legacy-score",
                covered=(ref,),
                basis=ref,
                issued_at=now - 120,
                valid_until=now - 60,
            ),
            created_at=now - 120,
            expires_at=now - 60,
        )
        run = None
        task = None
        if accepted:
            result = await host.repository.accept_host_proposal(
                proposal_id=proposal.proposal_id,
                binding=binding,
                owner=binding.effective_owner,
                space_id=item.scene.space_id,
                presence_id=item.scene.presence_id,
                sources=(
                    InitiativeSource(
                        InitiativeSourceKind.MEMORY, str(fact_id), memory_revision(fact)
                    ),
                ),
                thread_key=proposal.thread,
            )
            run = result.run
            await host._dispatch(run)
            task = await host.work.by_source(f"initiative:{run.run_id}")
            assert task is not None
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work)
                    .where(work.c.id == task["id"])
                    .values(state="waiting_user", model_requests=2)
                )
            receipt = await social(database, run, "synthetic-old-send")
            own, _ = await seed_event(
                host.ledger,
                bot_user_id=event.bot_user_id,
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id=event.bot_user_id,
                direction="outbound",
                content="synthetic original public expression",
                group_id=source.group_id,
                occurred_at=datetime.now(UTC),
            )
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(SocialOperationModel)
                    .where(SocialOperationModel.id == receipt.id)
                    .values(event_id=own.id)
                )

        # A serialized old reader payload, including the retired event body and
        # fingerprint. No provider is called to generate or upgrade this fixture.
        payload = item.controller.state.model_dump(mode="json")
        payload["proposals"][proposal.proposal_id] = {
            **proposal.model_dump(mode="json"),
            "source_fingerprints": ["synthetic-retired-fingerprint"],
        }
        payload["pending"] = proposal.proposal_id
        payload["events"][ref.event_id] = {
            "scope": item.scene.scope.model_dump(mode="json"),
            "ref": ref.model_dump(mode="json"),
            "thread": proposal.thread,
            "author": "SELF",
            "target": "group",
            "text": "synthetic retired seed content",
            "at": now - 120,
            "kind": "seed",
        }
        payload["host_checkpoint"]["source_versions"][ref.event_id] = [1, memory_revision(fact)]
        await host._store.save(json.dumps(payload), expected_revision=item.revision)
        host._sessions.clear()

        restored = await host._session(item.scene)
        assert restored.controller.state.pending == proposal.proposal_id
        assert ref.event_id not in restored.controller.state.events
        monkeypatch.setattr(restored.controller, "advance", lambda *_args, **_kwargs: None)
        await host._advance_scene(restored)
        assert restored.controller.state.pending is None
        if accepted:
            assert restored.controller.state.proposal_runs[proposal.proposal_id] == run.run_id
            await reconcile_page(host, (run,))
            original = await host.work.by_source(f"initiative:{run.run_id}")
            assert original["id"] == task["id"]
            assert original["state"] == "waiting_user" and original["model_requests"] == 2
            state = restored.controller.state
            assert state.feedback[run.run_id].outcome == "accepted"
            assert len(state.effects) == 3  # Two original charges and one real logical send.
            assert state.work_pulses[run.run_id].public_at is not None
            await reconcile_page(host, (run,))
            assert len(state.effects) == 3

            reply, _ = await seed_event(
                host.ledger,
                bot_user_id=event.bot_user_id,
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id=event.sender_user_id,
                direction="inbound",
                content="synthetic reply to original public expression",
                group_id=source.group_id,
                occurred_at=datetime.now(UTC),
                reply_to_event_id=own.id,
            )
            await sync_scope_effects(host, restored)
            await host._hydrate(restored)
            state = restored.controller.state
            snapshot = restored.controller.freeze_observation(
                Snapshot(
                    scope=item.scene.scope,
                    focus=state.events[f"event:{reply.id}"],
                    context=(state.events[f"event:{own.id}"],),
                    sequence=1,
                    issued_at=time.time(),
                )
            )
            assert restored.controller.apply_semantic_observation(
                _observation(snapshot, act="extend_yuki")
            )
            assert any(reception.run_ref == run.run_id for reception in state.receptions.values())

            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == task["id"]).values(state="completed")
                )
            await reconcile_page(host, (run,))
            await social(database, run, "synthetic-late-confirmed-send")
            await reconcile_page(host, (run,))
            assert restored.controller.state.feedback[run.run_id].outcome == "completed"
            assert len(restored.controller.state.effects) == 4
            assert (await host.work.by_source(f"initiative:{run.run_id}"))["model_requests"] == 2
        else:
            assert (
                await host.repository.query_proposal(
                    conversation_id=item.scene.conversation_id,
                    generation=item.scene.generation,
                    owner=binding.effective_owner,
                    controller_epoch=binding.controller_epoch,
                    proposal_id=proposal.proposal_id,
                )
                is None
            )
            assert not restored.controller.state.proposal_runs
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["fact_revision", "last_evidence"])
async def test_legacy_ordinary_memory_basis_uses_current_exact_authorized_source(
    database, tmp_path, change
):
    facts, source, _run, _batch, fact_id = await reflection_fact(database)
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        a = await _event_and_route(database, host.ledger, group=source.group_id)
        item = await _item(host, a)
        event = item.controller.state.events[f"event:{a.id}"]
        fact = await facts.get_fact(fact_id)
        digest = memory_revision(fact)
        ref = SourceRef(event_id=f"memory:{fact_id}", revision=1)
        binding = replace(
            await host.binding_for_event(a.id),
            basis=((event.ref.event_id, event.ref.revision), (ref.event_id, ref.revision)),
        )
        prepared = host.ordinary.prepare(a, item.scene.generation, 1, binding)
        assert await host.ordinary.commit(prepared)
        unit = admission_unit_binding(prepared.admission)
        payload = item.controller.state.model_dump(mode="json")
        payload["events"][ref.event_id] = {
            "scope": item.scene.scope.model_dump(mode="json"),
            "ref": ref.model_dump(mode="json"),
            "thread": ref.event_id,
            "author": "SELF",
            "target": "group",
            "text": "synthetic legacy memory dependency",
            "at": time.time(),
            "kind": "seed",
        }
        payload["host_checkpoint"]["source_versions"][ref.event_id] = [1, digest]
        payload["host_checkpoint"]["participation_v1"] = {
            "version": 1,
            "units": {
                item.controller._unit_key(binding.unit_key, binding.target_hint): {
                    "binding": unit.model_dump(mode="json"),
                    "last_at": event.at,
                    "input_ref": event.ref.model_dump(mode="json"),
                    "hint": SelfReport(
                        run_ref=prepared.admission.activation_id,
                        sequence=1,
                        response_id="synthetic-legacy-ordinary-hint",
                        at=event.at,
                        delta=SelfDelta(engage="stay"),
                    ).model_dump(mode="json"),
                    "hint_basis": [event.ref.model_dump(mode="json"), ref.model_dump(mode="json")],
                }
            },
            "expressions": {},
        }
        await host._store.save(json.dumps(payload), expected_revision=item.revision)
        host._sessions.clear()
        restored = await host._session(item.scene)
        await host._hydrate(restored)
        await host._validate_boundaries(restored)
        assert (await host._sources_current(restored, (ref,)))[ref]
        assert any(
            current.binding == unit
            for current in restored.controller.participating_units(time.time())
        )
        assert ref.event_id not in restored.controller.state.events
        assert (await host.ordinary.current(a.id)).binding == binding

        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == fact_id)
                .values(content="synthetic memory source changed")
                if change == "fact_revision"
                else update(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.fact_id == fact_id)
                .values(excerpt="synthetic excerpt absent from the original receipt")
            )
        if change == "last_evidence":
            assert memory_revision(await facts.get_fact(fact_id)) == digest
        await host._validate_boundaries(restored)
        assert restored.controller.state.invalidated[ref.event_id] >= ref.revision
        assert all(
            current.binding != unit
            for current in restored.controller.participating_units(time.time())
        )
        # The old admission is a real receipt, even when its derived unit exits.
        assert (await host.ordinary.get(a.id)).activation_id == prepared.admission.activation_id
    finally:
        await host.close()
