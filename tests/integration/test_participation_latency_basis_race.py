"""A real admitted relationship cannot select a stale non-focus source basis."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from sqlalchemy import event, update
from tests.conftest import build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.self_initiative_memory_quality_helpers import reflection_fact
from tests.support.semantic_participation_host_helpers import _event_and_route, _host, _item
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.controller import State
from yuki_participation.models import SourceRef
from yuki_participation.self_report import SelfDelta, SelfReport
from yuki_participation.snapshot import freeze

from yuki.conversation.initiative_sources import memory_revision, source_revision
from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel
from yuki.services.participation_feedback import admission_unit_binding


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "body",
        "mention",
        "withdrawal",
        "source_equivalent",
        "null_equivalent",
        "bool_difference",
        "large_integer_difference",
        "signed_zero_difference",
        "escaped_equivalent",
    ],
)
async def test_writer_rechecks_nonfocus_basis_before_selecting_continuation(
    database, tmp_path, latency_evidence, change
):
    special = change in {
        "source_equivalent",
        "null_equivalent",
        "bool_difference",
        "large_integer_difference",
        "signed_zero_difference",
        "escaped_equivalent",
    }
    initial_segment = {
        "type": "mention",
        "data": {
            "kind": "member",
            "display_name": "合成名称",
            "person_id": None,
            "text_start": True,
            "text_end": 2,
            "source": {"native": "original"},
        },
    }
    if change == "large_integer_difference":
        initial_segment["data"]["text_start"] = 2**63
    elif change == "signed_zero_difference":
        initial_segment["data"]["text_start"] = -0.0
    if special:
        host, _ = await _host(database, tmp_path)
        prior = await _event_and_route(database, host.ledger, content="synthetic original exchange")
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == prior.id)
                .values(segments_json=json.dumps([initial_segment], ensure_ascii=False))
            )
        prior = await host.ledger.get_event(prior.id)
        item = await _item(host, prior)
        first_binding = await host.binding_for_event(prior.id)
        original = host.ordinary.prepare(
            prior, item.scene.generation, 1, first_binding, reason="mention"
        )
        assert await host.ordinary.commit(original)
        await host.on_ordinary_admitted(first_binding, original.admission)
        initial_unit = admission_unit_binding(original.admission)
        initial_event = item.controller.state.events[f"event:{prior.id}"]
        assert item.controller.observe_unit_hint(
            initial_unit,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=1,
                response_id="synthetic-stay",
                at=initial_event.at,
                delta=SelfDelta(engage="stay"),
            ),
        )
        await host._save(item)
    else:
        host, item, prior, original, _ = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic continuing input")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation"
        assert (f"event:{prior.id}", 1) in binding.basis and (
            f"event:{focus.id}",
            1,
        ) in binding.basis
        initial_focus_revision = source_revision(await host.ledger.get_event(focus.id))
        prepared = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        prepared_continuation = await host.prepare_continuation(binding)
        prior_revision = source_revision(await host.ledger.get_event(prior.id))
        processor = build_harness(
            database, make_settings(database.url, conversation_semantic_participation_enabled=True)
        ).processor
        processor.set_participation(host)

        # This is the real interval after the source/basis preparation and
        # before the original writer's selection of this new input.
        if change != "none":
            if special:
                updated = {"type": "mention", "data": dict(initial_segment["data"])}
                if change == "source_equivalent":
                    updated["data"]["source"] = {
                        "native": "changed but not a semantic mention field"
                    }
                elif change == "null_equivalent":
                    del updated["data"]["person_id"]
                elif change == "large_integer_difference":
                    updated["data"]["text_start"] = 2**63 + 1
                elif change == "signed_zero_difference":
                    updated["data"]["text_start"] = 0.0
                elif change == "escaped_equivalent":
                    pass
                else:
                    updated["data"]["text_start"] = 1
                values = {
                    "segments_json": json.dumps(
                        [updated], ensure_ascii=change == "escaped_equivalent"
                    )
                }
            else:
                values = (
                    {"content": "synthetically corrected earlier source"}
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
                                        "person_id": prior.author_person_id,
                                    },
                                }
                            ]
                        )
                    }
                    if change == "mention"
                    else {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
                )
            async with database.immediate_session() as session:
                await session.execute(
                    update(ChatEventModel).where(ChatEventModel.id == prior.id).values(**values)
                )

        async def allowed(writer):
            return await processor._ordinary_admission_allowed(
                prepared, reason, writer, continuation=prepared_continuation
            )

        conflict = None
        try:
            selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        except OrdinaryAdmissionConflict as error:
            assert str(error) == "ordinary_participation_preparation_changed"
            conflict = str(error)
            selected = False
            # Re-run the original selection outside the rolled-back writer.
            current = await host.continuation_for_event(focus.id, prepared=prepared_continuation)
            if current is not None:
                binding, reason = current
                prepared = host.ordinary.reprepare(prepared, binding, reason=reason)
                prepared_continuation = await host.prepare_continuation(binding)
                selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        persisted = await host.ordinary.get(focus.id)
        old = await host.ordinary.get(prior.id)
        focus_unchanged = initial_focus_revision == source_revision(
            await host.ledger.get_event(focus.id)
        )
        prior_revision_unchanged = prior_revision == source_revision(
            await host.ledger.get_event(prior.id)
        )
        equivalent = change in {
            "none",
            "source_equivalent",
            "null_equivalent",
            "escaped_equivalent",
        }
        latency_evidence.update(
            boundary=(
                "real Host/Processor qualification callback and SQLite admission; synthetic events"
            ),
            change=change,
            prior_event_id=prior.id,
            focus_event_id=focus.id,
            prior_activation_id=original.admission.activation_id,
            new_activation_id=persisted.activation_id if persisted else None,
            focus_revision_unchanged=focus_unchanged,
            prior_admission_preserved=old is not None,
            prior_revision_unchanged=prior_revision_unchanged,
            stale_new_admission_selected=selected if not equivalent else False,
            normal_new_admission_selected=selected if equivalent else None,
            first_commit_conflict=conflict,
        )
        assert focus_unchanged and old.activation_id == original.admission.activation_id
        assert prior_revision_unchanged == equivalent
        if equivalent:
            assert selected and persisted is not None
        else:
            assert not selected and persisted is None, "writer accepted a changed non-focus basis"
    finally:
        await host.close()


@pytest.mark.parametrize("restore_before_writer", [True, False])
async def test_writer_rechecks_basis_temporarily_invalid_at_preparation(
    database, tmp_path, latency_evidence, restore_before_writer
):
    host, item, prior, original, _ = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic continuing input")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation" and (f"event:{prior.id}", 1) in binding.basis
        prepared = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        original_content = prior.content
        original_revision = source_revision(await host.ledger.get_event(prior.id))
        state_before = item.controller.state.model_dump_json()
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == prior.id)
                .values(content="synthetic transient source change")
            )
        assert source_revision(await host.ledger.get_event(prior.id)) != original_revision
        continuation = await host.prepare_continuation(binding)
        state_after_prepare = item.controller.state.model_dump_json()
        assert state_after_prepare == state_before
        if restore_before_writer:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == prior.id)
                    .values(content=original_content)
                )
            assert source_revision(await host.ledger.get_event(prior.id)) == original_revision
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
            selected = False
            current = await host.continuation_for_event(focus.id, prepared=continuation)
            if current is not None:
                binding, reason = current
                prepared = host.ordinary.reprepare(prepared, binding, reason=reason)
                continuation = await host.prepare_continuation(binding)
                selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        persisted = await host.ordinary.get(focus.id)
        original_after = await host.ordinary.get(prior.id)
        latency_evidence.update(
            boundary="real Host/Processor/SQLite; synthetic canonical events; no provider",
            restore_before_writer=restore_before_writer,
            controller_unchanged_during_preparation=True,
            focus_event_id=focus.id,
            requested_activation_id=prepared.admission.activation_id,
            persisted_activation_id=persisted.activation_id if persisted else None,
            original_activation_id=original.admission.activation_id,
            selected=selected,
            first_commit_conflict=conflict,
        )
        assert original_after.activation_id == original.admission.activation_id
        if restore_before_writer:
            assert selected and persisted is not None
            assert persisted.activation_id == prepared.admission.activation_id
        else:
            assert not selected and persisted is None
    finally:
        await host.close()


async def test_continuation_current_ends_when_basis_stays_invalid(database, tmp_path):
    host, _item, prior, _original, _ = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic continuing input")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation" and (f"event:{prior.id}", 1) in binding.basis
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == prior.id)
                .values(content="synthetic persisted source change")
            )
        assert not await asyncio.wait_for(host.continuation_current(binding), timeout=5)
    finally:
        await host.close()


async def test_continuation_current_keeps_valid_original_basis_after_reselection(
    database, tmp_path, monkeypatch, latency_evidence
):
    from yuki.services import participation_ordinary

    host, item, prior, original, _ = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic original focus")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation"
        other = await _event_and_route(database, host.ledger, content="synthetic direct input")
        direct_binding = await host.binding_for_event(other.id)
        direct = host.ordinary.prepare(
            other, item.scene.generation, 2, direct_binding, reason="mention"
        )
        assert await host.ordinary.commit(direct)
        prepare = participation_ordinary.prepare_continuation
        matches = participation_ordinary.continuation_matches
        changed, conflicts = [], []

        async def inject_accepted_input(service, current):
            result = await prepare(service, current)
            if not changed:
                changed.append(True)
                await host.on_ordinary_admitted(direct_binding, direct.admission)
                fresh = await host.continuation_for_event(focus.id)
                assert fresh is not None and fresh[0].basis != binding.basis
                assert all(
                    (
                        await host._sources_current(
                            item,
                            tuple(
                                SourceRef(event_id=key, revision=revision)
                                for key, revision in binding.basis
                            ),
                        )
                    ).values()
                )
            return result

        async def capture_conflict(*args, **kwargs):
            try:
                return await matches(*args, **kwargs)
            except OrdinaryAdmissionConflict as error:
                conflicts.append(str(error))
                raise

        monkeypatch.setattr(participation_ordinary, "prepare_continuation", inject_accepted_input)
        monkeypatch.setattr(participation_ordinary, "continuation_matches", capture_conflict)
        assert await asyncio.wait_for(host.continuation_current(binding), timeout=5)
        assert changed and conflicts == ["ordinary_participation_preparation_changed"]
        assert (await host.ordinary.get(prior.id)).activation_id == original.admission.activation_id
        assert (await host.ordinary.get(other.id)).activation_id == direct.admission.activation_id
        latency_evidence.update(
            boundary=(
                "real Host/SQLite original source and separately accepted direct input; no provider"
            ),
            new_basis_differs=True,
            original_basis_still_current=True,
            conflict_then_original_eligibility=True,
        )
    finally:
        await host.close()


@pytest.mark.parametrize("restore_before_writer", [True, False])
async def test_transient_nonbasis_quiet_source_does_not_rewrite_current_facts(
    database, tmp_path, latency_evidence, restore_before_writer
):
    host, item, _prior, original, unit = await _relationship(database, tmp_path)
    try:
        focus = await _event_and_route(database, host.ledger, content="synthetic waiting input")
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(focus, item.scene.generation, 2, binding, reason=reason)
        quiet_source = await _event_and_route(
            database, host.ledger, content="synthetic quiet basis"
        )
        await host.binding_for_event(quiet_source.id)
        event = item.controller.state.events[f"event:{quiet_source.id}"]
        assert (event.ref.event_id, event.ref.revision) not in binding.basis
        quiet_binding = unit.model_copy(update={"basis": (*unit.basis, event.ref)})
        assert item.controller.observe_unit_hint(
            quiet_binding,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=2,
                response_id="synthetic-current-quiet",
                at=event.at,
                source=event.ref,
                delta=SelfDelta(engage="quiet"),
            ),
        )
        assert await host.continuation_for_event(focus.id) is None
        before = item.controller.state.model_dump_json()
        original_revision = source_revision(await host.ledger.get_event(quiet_source.id))
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == quiet_source.id)
                .values(content="synthetic temporary quiet-source change")
            )
        continuation = await host.prepare_continuation(binding)
        after_prepare = item.controller.state.model_dump_json()
        if restore_before_writer:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == quiet_source.id)
                    .values(content=quiet_source.content)
                )
            assert (
                source_revision(await host.ledger.get_event(quiet_source.id)) == original_revision
            )
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
            selected = False
            current = await host.continuation_for_event(focus.id, prepared=continuation)
            if current is not None:
                binding, reason = current
                prepared = host.ordinary.reprepare(prepared, binding, reason=reason)
                continuation = await host.prepare_continuation(binding)
                selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        persisted = await host.ordinary.get(focus.id)
        latency_evidence.update(
            boundary="real Host/Processor/SQLite; quiet source changed by test SQL; no provider",
            restore_before_writer=restore_before_writer,
            quiet_source_not_in_binding=True,
            state_unchanged_during_prepare=before == after_prepare,
            first_commit_conflict=conflict,
            selected=selected,
            new_activation_id=persisted.activation_id if persisted else None,
        )
        assert not selected and persisted is None
        assert before == after_prepare
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["none", "fact_revision", "last_evidence"])
async def test_writer_rechecks_legacy_memory_basis_after_preparation(
    database, tmp_path, latency_evidence, change
):
    # Reopen the real legacy wire shape: its old accepted relationship had a
    # memory basis backed by an actual authorized reflection fact/evidence.
    facts, source, _run, _batch, fact_id = await reflection_fact(database)
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        prior = await _event_and_route(database, host.ledger, group=source.group_id)
        item = await _item(host, prior)
        original_event = item.controller.state.events[f"event:{prior.id}"]
        digest = memory_revision(await facts.get_fact(fact_id))
        memory = SourceRef(event_id=f"memory:{fact_id}", revision=1)
        original_binding = replace(
            await host.binding_for_event(prior.id),
            basis=(
                (original_event.ref.event_id, original_event.ref.revision),
                (memory.event_id, memory.revision),
            ),
        )
        accepted = host.ordinary.prepare(prior, item.scene.generation, 1, original_binding)
        assert await host.ordinary.commit(accepted)
        unit = admission_unit_binding(accepted.admission)
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
                item.controller._unit_key(
                    original_binding.unit_key, original_binding.target_hint
                ): {
                    "binding": unit.model_dump(mode="json"),
                    "last_at": original_event.at,
                    "input_ref": original_event.ref.model_dump(mode="json"),
                    "hint": SelfReport(
                        run_ref=accepted.admission.activation_id,
                        sequence=1,
                        response_id="synthetic-legacy-hint",
                        at=original_event.at,
                        delta=SelfDelta(engage="stay"),
                    ).model_dump(mode="json"),
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
        restored = await host._session(item.scene)
        await host._hydrate(restored)
        await host._validate_boundaries(restored)
        assert (await host._sources_current(restored, (memory,)))[memory]
        focus = await _event_and_route(
            database, host.ledger, group=source.group_id, content="synthetic later continuation"
        )
        binding, reason = await host.continuation_for_event(focus.id)
        assert reason == "continuation" and (memory.event_id, memory.revision) in binding.basis
        prepared = host.ordinary.prepare(
            focus, restored.scene.generation, 2, binding, reason=reason
        )
        continuation = await host.prepare_continuation(binding)
        processor = build_harness(
            database, make_settings(database.url, conversation_semantic_participation_enabled=True)
        ).processor
        processor.set_participation(host)
        if change != "none":
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == fact_id)
                    .values(content="synthetic changed fact")
                    if change == "fact_revision"
                    else update(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact_id)
                    .values(excerpt="synthetic excerpt absent from the original receipt")
                )
        fact_digest_unchanged = memory_revision(await facts.get_fact(fact_id)) == digest
        if change == "last_evidence":
            assert fact_digest_unchanged

        async def allowed(writer):
            return await processor._ordinary_admission_allowed(
                prepared, reason, writer, continuation=continuation
            )

        conflict = None
        statements = []

        def capture(_connection, _cursor, sql, parameters, _context, _many):
            if "memory_evidence" in sql.lower():
                statements.append((sql, parameters))

        if change == "none":
            event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            try:
                selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
            except OrdinaryAdmissionConflict as error:
                assert str(error) == "ordinary_participation_preparation_changed"
                conflict = str(error)
                selected = False
                current = await host.continuation_for_event(focus.id, prepared=continuation)
                if current is not None:
                    binding, reason = current
                    prepared = host.ordinary.reprepare(prepared, binding, reason=reason)
                    continuation = await host.prepare_continuation(binding)
                    selected = await host.ordinary.commit(prepared, admission_allowed=allowed)
        finally:
            if change == "none":
                event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        plans = []
        if change == "none":
            async with database.read_session() as reader:
                connection = await reader.connection()
                for sql, parameters in statements:
                    plans.extend(
                        row[3]
                        for row in (
                            await connection.exec_driver_sql(
                                "EXPLAIN QUERY PLAN " + sql, parameters
                            )
                        ).all()
                    )
        current = await host.ordinary.get(focus.id)
        original = await host.ordinary.get(prior.id)
        latency_evidence.update(
            boundary=(
                "real legacy wire, authorized Memory fact/evidence, "
                "Host/Processor writer and SQLite"
            ),
            change=change,
            focus_event_id=focus.id,
            prior_event_id=prior.id,
            memory_fact_id=fact_id,
            prior_activation_id=accepted.admission.activation_id,
            new_activation_id=current.activation_id if current else None,
            fact_digest_unchanged=fact_digest_unchanged,
            selected=selected,
            first_commit_conflict=conflict,
            memory_writer_query_plan=plans,
        )
        assert original.activation_id == accepted.admission.activation_id
        if change == "none":
            assert statements, "writer did not verify its legacy memory basis"
            assert not any(
                "SCAN memory_evidence" in entry and "USING" not in entry for entry in plans
            )
            assert selected and current is not None
        else:
            assert not selected and current is None, "writer accepted invalid legacy memory basis"
    finally:
        await host.close()
