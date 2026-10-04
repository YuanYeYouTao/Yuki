"""Read-phase batching keeps the original source and admission authority checks."""

import pytest
from sqlalchemy import event, text, update
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from tests.unit.test_self_initiative_memory_quality import reflection_fact
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item
from yuki_participation.models import SourceRef

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.initiative_sources import memory_revision
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel
from qq_ai_bot.services.participation_feedback import _ordinary_bindings

pytestmark = pytest.mark.asyncio


async def test_duplicate_event_refs_have_one_session_and_two_selects(
    database, tmp_path, monkeypatch
):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path)
    try:
        ref = item.controller.state.events[f"event:{source.id}"].ref
        original = database.sessions
        sessions = 0
        selects = 0

        def count_session(*args, **kwargs):
            nonlocal sessions
            sessions += 1
            return original(*args, **kwargs)

        def count_sql(conn, cursor, statement, parameters, context, executemany):
            nonlocal selects
            selects += statement.lstrip().startswith("SELECT")

        monkeypatch.setattr(database, "sessions", count_session)
        event.listen(database.engine.sync_engine, "before_cursor_execute", count_sql)
        try:
            assert (await host._sources_current(item, (ref,) * 90)) == {ref: True}
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", count_sql)
        assert sessions == 1 and selects == 2
        # SourceRef.revision is still the controller counter, never the content digest.
        wrong = SourceRef(event_id=ref.event_id, revision=ref.revision + 1)
        assert not (await host._sources_current(item, (wrong,)))[wrong]
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["hide", "edit", "rebind", "presence", "reset"])
async def test_ordinary_batch_keeps_current_join_and_does_not_cache_authorization(
    database, tmp_path, change
):
    host, item, source, _, receipt = await ordinary_expression(database, tmp_path)
    try:
        initial = await _ordinary_bindings(host, item, [receipt] * 32)
        assert tuple(initial) == (receipt.source_turn_id,)
        async with database.immediate_session() as session:
            if change == "hide":
                await session.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(suppression_status="duplicate", utterance_fingerprint="0" * 64)
                )
            elif change == "edit":
                await session.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(content="new source content")
                )
            elif change == "rebind":
                await session.execute(
                    update(IdentityBindingModel)
                    .where(IdentityBindingModel.person_id == source.author_person_id)
                    .values(status="disabled")
                )
            elif change == "presence":
                await session.execute(
                    update(PresenceModel)
                    .where(PresenceModel.id == source.ingress_presence_id)
                    .values(enabled=False)
                )
            else:
                await session.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                    .values(generation=2)
                )
        assert await _ordinary_bindings(host, item, [receipt] * 32) == {}
    finally:
        await host.close()


async def test_phase_snapshot_does_not_turn_into_next_phase_source_authorization(
    database, tmp_path
):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path)
    try:
        ref = item.controller.state.events[f"event:{source.id}"].ref
        async with database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            assert (await host._sources_current(item, (ref,), session=reader))[ref]
            # A genuine other connection changes visibility while this phase is frozen.
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(suppression_status="duplicate", utterance_fingerprint="0" * 64)
                )
            assert (await host._sources_current(item, (ref,), session=reader))[ref]
        assert not (await host._sources_current(item, (ref,)))[ref]
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["quarantine", "evidence"])
async def test_memory_batch_preserves_review_and_real_evidence_lineage(database, tmp_path, change):
    _, _, _, _, fact_id = await reflection_fact(database)
    host, _ = await _host(database, tmp_path)
    try:
        source = await _event_and_route(database, host.app.ledger, group="3001")
        item = await _item(host, source)
        fact = await MemoryFactRepository(database).get_fact(fact_id)
        ref = SourceRef(event_id=f"memory:{fact_id}", revision=17)
        item.controller.state.host_checkpoint.setdefault("source_versions", {})[ref.event_id] = [
            ref.revision,
            memory_revision(fact),
        ]
        assert (await host._sources_current(item, (ref,) * 32))[ref]
        async with database.immediate_session() as session:
            if change == "quarantine":
                await session.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == fact_id)
                    .values(review_state="quarantined")
                )
            else:
                # Corrupt the real receipt lineage without changing the fact's own digest.
                await session.execute(
                    update(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact_id)
                    .values(excerpt="unrelated evidence")
                )
        assert not (await host._sources_current(item, (ref,)))[ref]
    finally:
        await host.close()


@pytest.mark.parametrize("send", [False, True])
async def test_cold_hydration_of_durable_ordinary_admission_does_not_invalidate_unseen_source(
    database, tmp_path, send
):
    from yuki_participation.controller import Controller
    from yuki_participation.participation import ParticipationCheckpoint

    from qq_ai_bot.services.participation_feedback import sync_scope_effects

    host, item, source, _, _ = await ordinary_expression(database, tmp_path, send=send)
    try:
        item.controller = Controller(item.scene.scope, item.controller.state.now)
        if send:
            # Match the real tick's receipt-before-hydration order.
            await sync_scope_effects(host, item)
        await host._hydrate(item)
        await host._hydrate(item)
        key = f"event:{source.id}"
        assert key in item.controller.state.events and key not in item.controller.state.invalidated
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert len(checkpoint.units) == 1
        if send:
            await sync_scope_effects(host, item)
            checkpoint = ParticipationCheckpoint.model_validate(
                item.controller.state.host_checkpoint["participation_v1"]
            )
            assert len(checkpoint.expressions) == 1
    finally:
        await host.close()


async def test_known_retained_unit_with_missing_source_proof_still_invalidates(database, tmp_path):
    host, item, source, _, receipt = await ordinary_expression(database, tmp_path)
    try:
        key = f"event:{source.id}"
        item.controller.state.events.clear()
        item.controller.state.host_checkpoint["source_versions"].clear()
        assert await _ordinary_bindings(host, item, [receipt]) == {}
        assert key in item.controller.state.invalidated
    finally:
        await host.close()
