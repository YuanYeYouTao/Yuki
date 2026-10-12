"""Actorless SELF evidence, read scope and restart-safe receipt reflection."""

import asyncio
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_mutation import _event, _service

from qq_ai_bot.conversation.autonomy_db_models import AutonomyBindingModel, InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import SpaceBindingModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, MemoryStatus, SelfMemoryVisibility
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession
from qq_ai_bot.memory.self_reflection.control import ReflectionControlRepository
from qq_ai_bot.memory.self_reflection.db_models import InitiativeReflectionCursorModel
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionCycleModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.tool_results.recorder import ToolInvocationRepository


async def seed(database):
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="建立测试群身份",
        group_id="3001",
    )
    run_id = str(uuid4())
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        conv = await session.get(CanonicalConversationModel, event.canonical_conversation_id)
        session.add(
            AutonomyBindingModel(
                conversation_id=conv.id,
                generation=conv.generation,
                master_enabled=True,
                external_enabled=True,
                effective_owner="semantic",
                controller_epoch=1,
                revision=1,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            InitiativeRunModel(
                id=run_id,
                proposal_id=run_id,
                conversation_id=conv.id,
                generation=conv.generation,
                owner="semantic",
                controller_epoch=1,
                space_id=conv.space_id,
                presence_id=event.ingress_presence_id,
                target_person_id=event.author_person_id,
                payload_hash="0" * 64,
                sources_json="[]",
                support_refs_json="[]",
                state="running",
                feedback_sequence=0,
                created_at=now,
                updated_at=now,
            )
        )
    return service, facts, event, run_id


async def record(
    database,
    event,
    run_id,
    call="call-1",
    *,
    result_excerpt="已生成并校验曲线绘图",
    reflection_excerpt_characters=2000,
    **extra,
):
    await ToolInvocationRepository(
        database, reflection_excerpt_characters=reflection_excerpt_characters
    ).record_invocation(
        conversation_key=event.scope.key,
        provider_id="core",
        tool_name="terminal_exec",
        success=True,
        latency_seconds=0,
        result_size=10,
        artifact_created=False,
        error_category=None,
        initiative_run_id=run_id,
        execution_id="execution-1",
        tool_call_id=call,
        canonical_conversation_id=event.canonical_conversation_id,
        result_excerpt=result_excerpt,
        **extra,
    )


async def finish(database, run_id):
    async with database.sessions() as session, session.begin():
        run = await session.get(InitiativeRunModel, run_id)
        run.state = "no_reply"


async def claim(database, cycle="cycle-1", *, max_characters=16000):
    return await SelfReflectionRepository(database).claim_due(
        scheduled_slot="synthetic",
        local_date="2026-09-22",
        event_threshold=20,
        character_threshold=16000,
        max_wait_seconds=3600,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=max_characters,
        cycle_id=cycle,
    )


@contextmanager
def unrelated_wal_commit_before_write(database, initiative_id):
    """Commit another connection after source reads and before the first writer SQL."""
    committed = False

    def compete(_connection, _cursor, statement, *_args):
        nonlocal committed
        sql = statement.lstrip().upper()
        if committed or not sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")):
            return
        with closing(sqlite3.connect(database.engine.url.database)) as writer:
            assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert (
                writer.execute(
                    "UPDATE autonomy_initiative_runs SET feedback_sequence = feedback_sequence + 1 "
                    "WHERE id = ?",
                    (initiative_id,),
                ).rowcount
                == 1
            )
            writer.commit()
        committed = True

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", compete)
    try:
        yield
    except OperationalError as error:
        error.add_note(f"sqlite_errorcode={getattr(error.orig, 'sqlite_errorcode', None)}")
        raise
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", compete)
    assert committed


async def test_reflection_scan_keeps_all_owner_counters_after_unrelated_wal_commit(database):
    mutations, _, initial, initiative_id = await seed(database)
    repository = SelfReflectionRepository(database)
    assert await repository.scan_new_events() == 0
    first = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="已有 owner 的待处理来源",
        group_id="3001",
    )
    assert await repository.scan_new_events() == 1
    old = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1002",
        content="旧 generation 的真实历史",
        group_id="3002",
    )
    async with database.immediate_session() as session:
        conversation = await session.get(CanonicalConversationModel, old.canonical_conversation_id)
        conversation.generation += 1
        conversation.last_generation_change_event_id = old.id
    events = [old]
    for owner, group, content, bot in (
        ("1001", "3001", "同一页的已有 owner", False),
        ("1001", None, "独立私人 owner 的真实来源", False),
        ("1002", "3002", "新 generation 的真实来源", False),
        ("8000", "3001", "Yuki 的实际回复", True),
        ("1001", "3001", "　", False),
    ):
        events.append(
            await _event(
                mutations._ledger,
                message_id=str(uuid4()),
                sender_user_id=owner,
                content=content,
                group_id=group,
                direction="outbound" if bot else "inbound",
                sender_is_bot=bot,
            )
        )
    with unrelated_wal_commit_before_write(database, initiative_id):
        scanned = 0
        while count := await repository.scan_new_events(limit=2):
            scanned += count
        assert scanned == len(events)
    assert await SelfReflectionRepository(database).scan_new_events(limit=2) == 0
    async with database.sessions() as session:
        runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
        states = tuple(await session.scalars(select(MemorySelfReflectionStateModel)))
        sources = tuple(
            await session.scalars(
                select(ChatEventModel)
                .where(
                    ChatEventModel.id.in_(
                        [first.id, *(item.id for item in events if item.id != old.id)]
                    )
                )
                .order_by(ChatEventModel.id)
            )
        )
        conversations = {
            row.id: row for row in await session.scalars(select(CanonicalConversationModel))
        }
        expected = {}
        for source in sources:
            conversation = conversations[source.canonical_conversation_id]
            expected.setdefault((conversation.person_id, conversation.space_id), []).append(source)
        assert len(states) == len(expected) == 3
        for state in states:
            owner_sources = expected[(state.canonical_person_id, state.canonical_space_id)]
            texts = [
                row.evidence_content.strip()
                for row in owner_sources
                if row.evidence_content.strip()
            ]
            assert initial.id <= state.last_event_id < owner_sources[0].id
            assert state.latest_event_id == owner_sources[-1].id
            assert state.pending_events == len(texts)
            assert state.pending_characters == sum(map(len, texts))
            assert state.pending_since == owner_sources[0].occurred_at
            assert state.has_yuki_reply == any(row.direction == "outbound" for row in owner_sources)
            assert not state.has_tool_result
        assert runtime.last_scanned_event_id == events[-1].id
        assert runtime.ingress_events_total == sum(
            bool(row.evidence_content.strip()) for row in sources
        )
        assert await session.scalar(select(func.count(ChatEventModel.id))) == len(events) + 2
        assert (await session.get(InitiativeRunModel, initiative_id)).feedback_sequence == 1


@pytest.mark.parametrize("source_kind", ["initiative", "ordinary"])
@pytest.mark.parametrize("run_state", ["new", "retry"])
async def test_concurrent_reflection_claims_keep_one_original_run(
    database, monkeypatch, source_kind, run_state
):
    mutations, _, initial, initiative_id = await seed(database)
    repository = SelfReflectionRepository(database)
    options = dict(
        scheduled_slot="race",
        local_date="2026-10-12",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=16000,
        force=True,
        cycle_id="race",
    )
    if source_kind == "initiative":
        await record(database, initial, initiative_id)
        await finish(database, initiative_id)
    else:
        await repository.scan_new_events()
        await _event(
            mutations._ledger,
            message_id="ordinary-concurrent-claim",
            sender_user_id="1001",
            content="双方读取同一原始普通窗口",
            group_id="3001",
        )
        await repository.scan_new_events()
    original_id = original_fingerprint = checkpoint = None
    if run_state == "retry":
        (previous,) = await repository.claim_due(**options)
        original_id = previous.run_id
        await repository.fail(original_id, "provider_unavailable")
        checkpoint = '{"output":{"proposals":[],"episodes":[]}}'
        async with database.immediate_session() as session:
            stored = await session.get(MemorySelfReflectionRunModel, original_id)
            original_fingerprint = stored.input_fingerprint
            stored.checkpoint_json = checkpoint
        options["cycle_id"] = "retry-cycle"
    both_prepared = asyncio.Event()
    readers = 0

    async def rendezvous():
        nonlocal readers
        readers += 1
        if readers == 2:
            both_prepared.set()
        await both_prepared.wait()

    with monkeypatch.context() as patch:
        if source_kind == "initiative":
            from qq_ai_bot.memory.self_reflection import initiative

            resolve = initiative.resolve_self_origin

            async def prepared_source(*args, **kwargs):
                source = await resolve(*args, **kwargs)
                await rendezvous()
                return source

            patch.setattr(initiative, "resolve_self_origin", prepared_source)
        else:
            project = SelfReflectionRepository._state

            async def prepared_state(*args, **kwargs):
                state = await project(*args, **kwargs)
                await rendezvous()
                return state

            patch.setattr(SelfReflectionRepository, "_state", staticmethod(prepared_state))
        outcomes = await asyncio.gather(
            repository.claim_due(**options),
            SelfReflectionRepository(database).claim_due(**options),
            return_exceptions=True,
        )
    accepted = [batch for result in outcomes if isinstance(result, tuple) for batch in result]
    assert len(accepted) == 1 and sum(result == () for result in outcomes) == 1, outcomes
    original = accepted[0]
    assert await repository.claim_due(**{**options, "cycle_id": "next-cycle"}) == ()
    async with database.sessions() as session:
        stored = tuple(await session.scalars(select(MemorySelfReflectionRunModel)))
    assert len(stored) == 1 and stored[0].id == original.run_id
    assert stored[0].status == "processing"
    assert stored[0].attempt_count == (2 if run_state == "retry" else 1)
    assert stored[0].input_fingerprint
    if run_state == "retry":
        assert original.run_id == original_id
        assert stored[0].input_fingerprint == original_fingerprint
        assert stored[0].checkpoint_json == checkpoint
    assert original.initiative_run_id == (initiative_id if source_kind == "initiative" else None)
    assert bool(original.events) == (source_kind == "ordinary")


@pytest.mark.parametrize("run_state", ["new", "retry"])
async def test_initiative_claim_keeps_original_paid_window_after_unrelated_wal_commit(
    database, run_state
):
    mutations, facts, initial, initiative_id = await seed(database)
    await record(database, initial, initiative_id, "original", result_excerpt="真实的原始工具来源")
    await finish(database, initiative_id)
    repository = SelfReflectionRepository(database)
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    checkpoint = fingerprint = None
    if run_state == "retry":
        (original,) = await claim(database, "paid-original")
        assert await reflection.reflect(original) == (0, 0)
        assert len(provider.requests) == 1
        checkpoint = await repository.load_checkpoint(original.run_id)
        async with database.sessions() as session:
            fingerprint = (
                await session.get(MemorySelfReflectionRunModel, original.run_id)
            ).input_fingerprint
        await repository.fail(original.run_id, "cursor_unacknowledged")
    await record(
        database, initial, initiative_id, "late", result_excerpt="迟到的另一条真实工具来源"
    )
    async with database.immediate_session() as session:
        owner = await session.get(CanonicalConversationModel, initial.canonical_conversation_id)
        owner.generation += 1
        owner.last_generation_change_event_id = initial.id
    with unrelated_wal_commit_before_write(database, initiative_id):
        (batch,) = await claim(database, "wal-receipt", max_characters=1)
    assert batch.initiative_run_id == initiative_id and batch.events == ()
    (receipt,) = await repository.tool_receipts(batch)
    assert receipt.result_excerpt == "真实的原始工具来源"
    assert batch.first_receipt_id == batch.last_receipt_id == receipt.id
    async with database.sessions() as session:
        (stored,) = tuple(await session.scalars(select(MemorySelfReflectionRunModel)))
        initiative = await session.get(InitiativeRunModel, initiative_id)
        owner = await session.get(CanonicalConversationModel, initial.canonical_conversation_id)
        assert stored.id == batch.run_id and stored.status == "processing"
        assert stored.canonical_person_id is None
        assert stored.canonical_space_id == initiative.space_id == owner.space_id
        assert initiative.generation < owner.generation and initiative.feedback_sequence == 1
        assert stored.attempt_count == (2 if run_state == "retry" else 1)
        if run_state == "retry":
            assert stored.id == original.run_id
            assert stored.input_fingerprint == fingerprint and stored.checkpoint_json == checkpoint
    assert await reflection.reflect(batch) == (0, 0)
    assert len(provider.requests) == 1
    await repository.complete(batch, proposals=0, committed=0)
    (remaining,) = await claim(database, "late-only")
    assert remaining.run_id != batch.run_id
    assert remaining.first_receipt_id > batch.last_receipt_id
    assert [item.result_excerpt for item in await repository.tool_receipts(remaining)] == [
        "迟到的另一条真实工具来源"
    ]
    async with database.sessions() as session:
        cursor = await session.get(InitiativeReflectionCursorModel, initiative_id)
        assert cursor.last_receipt_id == batch.last_receipt_id
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.parametrize("operation", ["claim", "complete", "recover_interrupted", "recover_stale"])
async def test_reflection_transition_keeps_original_source_after_unrelated_wal_commit(
    database, operation
):
    mutations, _, initial, initiative_id = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    first = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="原批次的完整真实来源",
        group_id="3001",
    )
    await repository.scan_new_events()
    options = dict(
        scheduled_slot="wal-transition",
        local_date="2026-10-12",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=1,
        max_characters=16000,
        cycle_id="original-cycle",
    )
    if operation != "claim":
        (batch,) = await repository.claim_due(**options)
        if operation.startswith("recover"):
            await repository.save_checkpoint(batch.run_id, '{"completed_counts":[0,0]}')
    tail = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="原批次之后尚未处理的完整尾部",
        group_id="3001",
    )
    await repository.scan_new_events()
    with unrelated_wal_commit_before_write(database, initiative_id):
        if operation == "claim":
            (batch,) = await repository.claim_due(**options)
        elif operation == "complete":
            await repository.complete(batch, proposals=0, committed=0)
        elif operation == "recover_interrupted":
            assert await repository.recover_interrupted(batch.run_id, "interrupted") == "completed"
        else:
            assert await repository.recover_stale_runs(started_before=datetime.now(UTC)) == 1
    assert [item.id for item in batch.events] == [first.id]
    assert batch.events[0].content == first.content
    async with database.sessions() as session:
        (run,) = tuple(await session.scalars(select(MemorySelfReflectionRunModel)))
        state = await session.get(MemorySelfReflectionStateModel, batch.state.id)
        owner = await session.get(CanonicalConversationModel, first.canonical_conversation_id)
        runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
        assert run.id == batch.run_id and run.attempt_count == 1
        assert (run.first_event_id, run.last_event_id) == (first.id, first.id)
        assert (run.canonical_person_id, run.canonical_space_id) == (
            owner.person_id,
            owner.space_id,
        )
        assert run.cycle_id == "original-cycle" and run.input_fingerprint
        assert run.proposal_count == run.committed_count == 0
        assert run.checkpoint_json is None
        assert run.status == ("processing" if operation == "claim" else "completed")
        assert state.last_event_id == (initial.id if operation == "claim" else first.id)
        assert state.latest_event_id == tail.id
        expected = [first, tail] if operation == "claim" else [tail]
        assert state.pending_events == len(expected)
        assert state.pending_characters == sum(len(item.content) for item in expected)
        assert not state.has_yuki_reply and not state.has_tool_result
        assert runtime.last_scanned_event_id == tail.id and runtime.ingress_events_total == 2
        assert (await session.get(InitiativeRunModel, initiative_id)).feedback_sequence == 1
    if operation != "claim":
        assert await repository.completed_result(batch.run_id) == (0, 0)
        await repository.complete(batch, proposals=0, committed=0)
        assert await repository.recover_interrupted(batch.run_id, "duplicate-recovery") is None
        assert await repository.recover_stale_runs(started_before=datetime.now(UTC)) == 0
        (next_batch,) = await repository.claim_due(
            **{**options, "scheduled_slot": "tail-only", "cycle_id": "tail-cycle"}
        )
        assert [item.id for item in next_batch.events] == [tail.id]


async def test_visible_self_input_keeps_all_local_facts_and_active_only_expiry(database):
    _, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    global_facts, local_facts = [], []
    for visibility, bucket in (("global", global_facts), ("group", local_facts)):
        for index in range(21):
            bucket.append(
                await facts.remember(
                    MemoryFactCreate(
                        scope_type="self",
                        visibility_type=visibility,
                        visibility_group_id="3001" if visibility == "group" else None,
                        memory_key=f"visible:{visibility}:{index}",
                        category="test",
                        content=f"{visibility} self fact {index}",
                        source_type="explicit",
                    )
                )
            )
    expired = datetime.now(UTC) - timedelta(days=1)
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == global_facts[0].id)
            .values(valid_until=expired)
        )
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == global_facts[1].id)
            .values(status="contested", conflict_state="contested", valid_until=expired)
        )
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == local_facts[0].id)
            .values(status="invalidated", invalidated_reason="expired", valid_until=expired)
        )
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == local_facts[1].id)
            .values(status="superseded")
        )
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == global_facts[2].id)
            .values(review_state="quarantined")
        )
        await session.execute(
            update(SpaceBindingModel)
            .where(SpaceBindingModel.space_id == batch.state.canonical_space_id)
            .values(status="disabled")
        )
    reflection = object.__new__(SelfReflectionService)
    reflection._facts = facts
    visible = await reflection._visible_self_facts(batch)
    expected = {f.id for f in (*global_facts, *local_facts)} - {
        global_facts[0].id,
        global_facts[2].id,
        local_facts[1].id,
    }
    assert {f.id for f in visible} == expected
    assert global_facts[-1].id in expected and local_facts[-1].id in expected
    assert visible[-2].id == global_facts[1].id and visible[-1].id == local_facts[0].id


async def test_reflection_health_uses_original_oldest_recent_cycle_baseline(database, monkeypatch):
    mutations, _, _, _ = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for index in range(4):
        await _event(
            mutations._ledger,
            message_id=f"baseline-health-{index}",
            sender_user_id="1001",
            content=f"真实待反思消息 {index}",
            group_id="3001",
        )
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="baseline-health",
        local_date="2026-10-12",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=1,
        max_characters=16000,
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        for hours, ingress_before in ((25, 0), (10, 1), (1, 1000)):
            at = now - timedelta(hours=hours)
            session.add(
                MemorySelfReflectionCycleModel(
                    id=f"sr_baseline_{hours}",
                    source_key=f"baseline:{hours}",
                    trigger="scheduled",
                    status="completed",
                    created_at=at,
                    completed_at=at,
                    report_json=json.dumps({"before": {"ingress_events_total": ingress_before}}),
                    delivery_state="not_required",
                )
            )
        await session.execute(
            update(MemorySelfReflectionRunModel)
            .where(MemorySelfReflectionRunModel.id == batch.run_id)
            .values(
                status="failed", retry_state="waiting", next_attempt_at=now + timedelta(hours=1)
            )
        )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    from qq_ai_bot.memory.self_reflection import control

    monkeypatch.setattr(control, "datetime", FixedDateTime)
    report = await ReflectionControlRepository(
        database, make_settings(database.url, memory_self_reflection_event_threshold=1)
    ).snapshot()
    assert report["rate_window_seconds"] == 10 * 3600
    assert report["ingress_events_total"] == 4
    assert report["ingress_events_per_hour"] == 0.3
    assert report["drain_events_per_hour"] == 0
    assert report["waiting_retry"] == {"events": 1, "conversations": 1}
    assert report["actionable"] == {"events": 3, "conversations": 1}


async def test_mixed_recovery_prepares_full_tail_before_cursor_write_and_sees_staged_completion(
    database,
    monkeypatch,
):
    mutations, _, initial, initiative_id = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    await record(database, initial, initiative_id)
    await finish(database, initiative_id)
    (initiative,) = await claim(database)
    first = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="completed range",
        group_id="3001",
    )
    await repository.scan_new_events()
    from qq_ai_bot.domain import audio

    transcripts = []
    original_transcript = audio.transcript_context

    def transcript_context(value, *, include_replies=True):
        transcripts.append(value)
        return original_transcript(value, include_replies=include_replies)

    monkeypatch.setattr(audio, "transcript_context", transcript_context)
    (ordinary,) = await repository.claim_due(
        scheduled_slot="mixed-ordinary",
        local_date="2026-10-12",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=1,
        max_characters=16000,
    )
    tail = []
    for index in range(105):
        tail.append(
            await _event(
                mutations._ledger,
                message_id=str(uuid4()),
                sender_user_id="1001",
                content="　" if index == 0 else f"pending {index}",
                group_id="3001",
            )
        )
    speech = json.dumps(
        [
            {"source": "current", "segment_index": 0, "text": "真实当前语音"},
            {"source": "reply", "segment_index": 1, "text": "旧引用语音"},
        ],
        ensure_ascii=False,
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == tail[-1].id)
            .values(audio_transcript=speech)
        )
        for run_id in (initiative.run_id, ordinary.run_id):
            row = await session.get(MemorySelfReflectionRunModel, run_id)
            row.checkpoint_json = '{"completed_counts":[0,0]}'
    await repository.scan_new_events()
    transcripts.clear()
    writing = False
    source_reads_after_write = []

    def trace(_connection, _cursor, statement, *_args):
        nonlocal writing
        sql = statement.lstrip().upper()
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writing = True
        elif writing and sql.startswith("SELECT") and "CHAT_EVENTS" in sql:
            source_reads_after_write.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        assert await repository.recover_stale_runs(started_before=datetime.now(UTC)) == 2
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", trace)
    assert source_reads_after_write == []
    assert transcripts == [speech]
    async with database.sessions() as session:
        state = await session.get(MemorySelfReflectionStateModel, ordinary.state.id)
        rows = tuple(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.id.in_(tuple(item.id for item in tail)))
                .order_by(ChatEventModel.id)
            )
        )
        texts = [row.evidence_content for row in rows if row.evidence_content.strip()]
        assert state.last_event_id == first.id
        assert state.latest_event_id == tail[-1].id
        assert state.pending_events == len(texts) == 104
        assert state.pending_characters == sum(map(len, texts))
        assert state.pending_since == rows[1].occurred_at
        cursor = await session.get(InitiativeReflectionCursorModel, initiative_id)
        assert cursor.last_receipt_id == initiative.last_receipt_id
        assert (
            await session.get(MemorySelfReflectionRunModel, ordinary.run_id)
        ).status == "completed"


@pytest.mark.asyncio
async def test_run_receipt_dedup_and_no_manufactured_events(database):
    _, _, event, run_id = await seed(database)
    await record(database, event, run_id)
    await record(database, event, run_id)
    with pytest.raises(ValueError, match="actorless"):
        await record(database, event, run_id, trigger_event_id=event.id)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(MemoryToolReceiptModel)))
        assert len(rows) == 1
        assert rows[0].trigger_event_id is None
        assert rows[0].initiative_run_id == run_id
        assert rows[0].canonical_person_id is None
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.asyncio
async def test_committed_tool_receipt_does_not_need_a_live_group_binding(database):
    _, _, event, run_id = await seed(database)
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.external_space_id == "3001",
            )
        )
        binding.status = "disabled"
    await record(database, event, run_id)
    async with database.sessions() as session:
        row = await session.scalar(select(MemoryToolReceiptModel))
        assert row.initiative_run_id == run_id and row.trigger_event_id is None


@pytest.mark.asyncio
async def test_actorless_memory_scope_has_no_target_person_partition(database):
    _, _, event, run_id = await seed(database)
    turn = await TurnMemorySession.open_self_origin(
        initiative_run_id=run_id,
        canonical_conversation_id=event.canonical_conversation_id,
        identity=event.scope,
        partition_lookup=DatabaseMemoryPartitionLookup(database),
    )
    assert turn.scope.scope_type.value == "group"
    assert turn.scope.scope_id == event.group_id
    assert turn.scope.partition_key == f"group:{event.group_id}"
    assert turn.contract.persistent_write_allowed


@pytest.mark.asyncio
async def test_silent_reflection_cursor_survives_restart_and_late_receipt(database):
    _, _, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    assert batch.events == () and batch.initiative_run_id == run_id
    assert len(await SelfReflectionRepository(database).tool_receipts(batch)) == 1
    assert await claim(database, "cycle-2") == ()
    await SelfReflectionRepository(database).complete(batch, proposals=0, committed=0)
    assert await claim(database, "cycle-3") == ()
    await record(database, event, run_id, "call-2")
    (later,) = await claim(database, "cycle-4")
    assert later.first_receipt_id > batch.last_receipt_id
    assert later.run_id != batch.run_id


@pytest.mark.asyncio
async def test_silent_receipt_windows_keep_large_source_complete_and_remaining_source_pending(
    database,
):
    mutations, facts, event, run_id = await seed(database)
    for call, excerpt in (("large", "甲" * 8000), ("small", "乙" * 1000)):
        await record(
            database,
            event,
            run_id,
            call,
            result_excerpt=excerpt,
            reflection_excerpt_characters=8000,
        )
    await finish(database, run_id)
    repository = SelfReflectionRepository(database)
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    (first,) = await claim(database, "large-first", max_characters=4000)
    assert first.first_receipt_id == first.last_receipt_id
    assert [len(item.result_excerpt) for item in await repository.tool_receipts(first)] == [8000]
    assert await reflection.reflect(first) == (0, 0)
    first_payload = json.loads(provider.requests[0].messages[-1].content)
    assert [item["result_excerpt"] for item in first_payload["tool_receipts"]] == ["甲" * 8000]
    await repository.complete(first, proposals=0, committed=0)
    (second,) = await claim(database, "small-second", max_characters=4000)
    assert second.run_id != first.run_id
    assert second.first_receipt_id == second.last_receipt_id > first.last_receipt_id
    assert await reflection.reflect(second) == (0, 0)
    second_payload = json.loads(provider.requests[1].messages[-1].content)
    assert [item["result_excerpt"] for item in second_payload["tool_receipts"]] == ["乙" * 1000]
    await repository.complete(second, proposals=0, committed=0)
    assert await claim(database, "finished", max_characters=4000) == ()


@pytest.mark.asyncio
async def test_silent_original_receipt_window_retries_without_reselecting_new_budget(database):
    mutations, facts, event, run_id = await seed(database)
    for call, excerpt in (("first", "甲" * 1000), ("second", "乙" * 1000)):
        await record(database, event, run_id, call, result_excerpt=excerpt)
    await finish(database, run_id)
    repository = SelfReflectionRepository(database)
    (original,) = await claim(database, "original", max_characters=4000)
    async with database.sessions() as session:
        original_fingerprint = (
            await session.get(MemorySelfReflectionRunModel, original.run_id)
        ).input_fingerprint
    await repository.fail(original.run_id, "provider_unavailable")
    (retried,) = await claim(database, "changed-budget", max_characters=1000)
    assert retried.run_id == original.run_id
    assert (retried.first_receipt_id, retried.last_receipt_id) == (
        original.first_receipt_id,
        original.last_receipt_id,
    )
    async with database.sessions() as session:
        row = await session.get(MemorySelfReflectionRunModel, original.run_id)
        assert row.input_fingerprint == original_fingerprint and row.retry_state != "isolated"
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(retried) == (0, 0)
    payload = json.loads(provider.requests[0].messages[-1].content)
    assert [item["result_excerpt"] for item in payload["tool_receipts"]] == [
        "甲" * 1000,
        "乙" * 1000,
    ]
    assert await reflection.reflect(retried) == (0, 0)
    assert len(provider.requests) == 1
    await repository.complete(retried, proposals=0, committed=0)
    assert await claim(database, "finished", max_characters=1000) == ()


@pytest.mark.asyncio
async def test_actorless_reflection_uses_unified_mutation_and_readable_evidence(database):
    service, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    context = MemoryMutationContext(
        event=None,
        conversation_key="unused",
        turn_origin="memory_self_reflection",
        delegation_mode="self_reflection",
        trigger_actor_user_id="",
        executed_by_bot_user_id="",
        decision_actor_type=MemoryDecisionActorType.REFLECTION,
        decision_actor_id="yuki_self_reflection",
        initiative_run_id=run_id,
        evidence_tool_receipt_id=batch.first_receipt_id,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我在群里独立完成了曲线绘图，并用真实运行结果校验了产物。",
        memory_key="self_episode:synthetic",
        category="self_episode",
        kind=MemoryKind.EPISODE,
        importance=4,
        reason="一次有真实工具结果支持的独立完成经历",
        evidence_quote="已生成并校验曲线绘图",
    )
    target = ResolvedSubject(
        MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
    )
    result = await service.mutate_resolved(
        request, context, target=target, self_reflection_result=(batch.run_id, "episode", 0)
    )
    assert result.ok, result.reason_code
    evidence = await facts.list_evidence(result.new_fact_id)
    assert len(evidence) == 1 and evidence[0].event_id is None
    assert evidence[0].tool_receipt_id == batch.first_receipt_id
    duplicate = await service.mutate_resolved(request, context, target=target)
    assert duplicate.deduplicated
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
        assert receipt.trigger_source_type == "initiative_run"
        assert receipt.initiative_run_id == run_id and receipt.trigger_event_id is None
    metadata_request = request.model_copy(
        update={
            "operation": MemoryMutationOperation.UPDATE_METADATA,
            "fact_id": result.new_fact_id,
            "target": None,
            "new_content": None,
            "category": "experience_note",
            "importance": 5,
        }
    )
    metadata = await service.mutate_resolved(metadata_request, context, target=target)
    assert metadata.ok, metadata.reason_code
    updated = await facts.get_fact(metadata.new_fact_id)
    assert updated.supersedes_id == result.new_fact_id
    assert updated.category == "experience_note" and updated.importance == 5
    assert updated.content == request.new_content
    assert (await facts.list_evidence(updated.id))[0].tool_receipt_id == batch.first_receipt_id
    assert (await service.mutate_resolved(metadata_request, context, target=target)).deduplicated
    invalidate_request = metadata_request.model_copy(
        update={"operation": MemoryMutationOperation.INVALIDATE, "fact_id": updated.id}
    )
    assert (await service.mutate_resolved(invalidate_request, context, target=target)).ok
    assert (await service.mutate_resolved(invalidate_request, context, target=target)).deduplicated
    assert (await facts.get_fact(updated.id)).status is MemoryStatus.INVALIDATED
    restore_request = invalidate_request.model_copy(
        update={"operation": MemoryMutationOperation.RESTORE}
    )
    restored = await service.mutate_resolved(restore_request, context, target=target)
    assert restored.ok, restored.reason_code
    assert restored.new_fact_id == updated.id
    assert (await facts.get_fact(updated.id)).status is MemoryStatus.ACTIVE
    assert (await service.mutate_resolved(restore_request, context, target=target)).deduplicated
    bad_quote = restore_request.model_copy(update={"evidence_quote": "不存在的工具结果"})
    assert (
        await service.mutate_resolved(bad_quote, context, target=target)
    ).reason_code == "evidence_quote_not_in_tool_receipt"
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(MemoryMutationReceiptModel)))
        assert len(receipts) == 4
        assert all(
            item.initiative_run_id == run_id and item.trigger_event_id is None for item in receipts
        )
    rejected = await service.mutate_resolved(
        request,
        context,
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.PRIVATE,
            "1001",
            None,
        ),
    )
    assert not rejected.ok and rejected.reason_code == "initiative_memory_scope_forbidden"


@pytest.mark.asyncio
async def test_silent_service_episode_keeps_checkpoint_without_second_model_request(database):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    repository = SelfReflectionRepository(database)
    provider = FakeLLMProvider(
        responder=lambda _: json.dumps(
            {
                "proposals": [{"operation": "noop", "evidence_refs": ["tool_1"]}],
                "episodes": [
                    {
                        "passages": [
                            {
                                "evidence_refs": ["tool_1", "tool_1"],
                                "content": "我独立生成了曲线绘图，并校验了产物。",
                            }
                        ],
                        "importance": 4,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    reflection = SelfReflectionService(
        settings=make_settings("sqlite+aiosqlite:///:memory:", self_memory_enabled=True),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    projected, _, _, event_map, _ = await reflection._input(batch)
    assert projected.source_kind == "initiative_tools" and not projected.events and not event_map
    assert projected.tool_receipts[0].occurred_at is not None
    assert await reflection.reflect(batch) == (2, 1)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryEvidenceModel.id))) == 1
    request_count = len(provider.requests)
    # A crash between committed result/checkpoint and cursor acknowledgement is recovered.
    assert await repository.recover_interrupted(batch.run_id, "stale_processing") == "completed"
    assert await reflection.reflect(batch) == (2, 1)
    assert len(provider.requests) == request_count
    assert await claim(database, "cycle-next") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple_bindings", [False, True])
async def test_main_self_memory_write_uses_original_tool_receipt_without_human_event(
    database, tmp_path, multiple_bindings
):
    from tests.conftest import build_harness
    from tests.integration.test_self_automation_delivery import self_actor
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.runtime.origin import TurnOrigin
    from qq_ai_bot.runtime.work_activation import bind_work_activation
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.execution_sources import recover_self_source
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        binding_id = await session.scalar(
            select(SpaceBindingModel.id).where(SpaceBindingModel.external_space_id == "20001")
        )
    if multiple_bindings:
        async with database.sessions() as session, session.begin():
            session.add(
                SpaceBindingModel(
                    id=str(uuid4()),
                    space_id=env.space,
                    platform="qq",
                    external_space_id="20002",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    actor = await self_actor(database, env)
    mutations, facts, _ledger, _ = _service(database, self_memory_enabled=True)
    write = {
        "operation": "create",
        "target": {"subject_ref": "self", "scope_type": "self"},
        "new_content": "我实际查询了当前群成员列表，确认了 member 的成员资料。",
        "memory_key": "self_episode:actual-tool",
        "category": "self_episode",
        "kind": "episode",
        "evidence_quote": "member",
    }
    calls = [
        ("get_group_members", {"limit": 10, "space_binding_id": binding_id}),
        ("get_group_members", {"limit": 20, "space_binding_id": binding_id}),
        ("memory_change", write),
    ]
    results = []

    def respond(request):
        if len(requests := provider.requests) > 1:
            results.append(request.messages[-1].content)
        index = len(requests) - 1
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"original-{index}",
                    ToolFunction(name, json.dumps(arguments, ensure_ascii=False)),
                ),
            ),
        )

    provider = FakeLLMProvider(responder=respond)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            enabled_groups_csv="20001",
            runtime_work_enabled=True,
            self_memory_enabled=True,
        ),
        provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._tools._memory_mutations = mutations
    chat._tool_invocations = ToolInvocationRepository(database)
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    owner = WorkRepository(database)
    source = {
        "origin": "self_initiative",
        "principal_kind": "self",
        "actor_user_id": "",
        "initiative_run_id": actor.initiative_run_id,
        "conversation_id": actor.conversation_id,
        "generation": 1,
        "space_id": env.space,
        "presence_id": env.presence,
        "bot_user_id": "80001",
        "group_id": "20001",
        "instruction": "记录实际工具经历，然后静默结束",
        "delivery_contract": "return_to_caller",
    }
    lease = await owner.acquire(actor.conversation_id, 1)
    item = await owner.accept(
        lease, source_key=actor.source_key, source=source, goal=source["instruction"]
    )
    recovered = await recover_self_source(
        database, actor.conversation_id, source, request_id=item["id"]
    )
    identity = ConversationScope.group("80001", "20001")

    async def validate():
        assert await owner.valid(lease)

    control = WorkControl(owner, lease, item["source_key"], source, validate)
    control.current = item
    async with chat._turn_coordinator.background_turn(identity.key) as token:
        snapshot = ConversationTurnSnapshot(
            actor.conversation_id,
            identity.key,
            1,
            None,
            token.version,
            identity.key,
            initiative_run_id=actor.initiative_run_id,
        )
        async with bind_work_activation(control):
            result = await chat.generate_self_initiative(
                trigger=recovered.trigger(),
                runtime=await chat._runtime_config.snapshot(group_id="20001"),
                turn_token=token,
                turn_snapshot=snapshot,
                before_model_request=validate,
                source_runtime=ToolRuntime(
                    inbound=None,
                    actor_context=recovered.actor(item["id"]),
                    gateway=None,
                    allow_generic_onebot=False,
                    conversation_key=identity.key,
                    execution_id=item["id"],
                    origin=TurnOrigin.SELF_INITIATIVE,
                    initiative_run_id=actor.initiative_run_id,
                    conversation_id=actor.conversation_id,
                    scope_type=ScopeType.GROUP,
                    external_target_id="20001",
                    space_id=env.space,
                    sandbox_source={**source, "work_id": item["id"]},
                ),
            )
    assert result.text == "" and len(provider.requests) == 4, results
    assert all(json.loads(text).get("ok") for text in results), results[1]
    async with database.sessions() as session:
        mutation = await session.scalar(select(MemoryMutationReceiptModel))
        assert mutation is not None, results
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert (
            mutation.initiative_run_id == actor.initiative_run_id
            and mutation.trigger_event_id is None
        )
        receipts = list(
            await session.scalars(
                select(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.tool_name == "get_group_members")
                .order_by(MemoryToolReceiptModel.id)
            )
        )
        assert len(receipts) == 2
        receipt = receipts[-1]
        assert (
            receipt.initiative_run_id == actor.initiative_run_id
            and receipt.execution_id == item["id"]
        )
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
    fact = await facts.get_fact(mutation.new_fact_id)
    evidence = await facts.list_evidence(fact.id)
    assert fact.scope_type is MemoryScopeType.SELF and fact.visibility_group_id == "20001"
    assert (
        len(evidence) == 1
        and evidence[0].tool_receipt_id == receipt.id
        and evidence[0].event_id is None
    )
    assert (await owner.get(item["id"]))["state"] == "completed"
    assert not await owner.valid(lease)
    assert not any(name.startswith("send_") for name, _ in env.bot.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restore", "update_metadata"])
async def test_reflection_model_reuses_restore_and_metadata_domain_operations(database, operation):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryToolReceiptModel))
    context = MemoryMutationContext(
        event=None,
        conversation_key="self-test",
        turn_origin="memory_self_reflection",
        delegation_mode="self_reflection",
        trigger_actor_user_id="",
        executed_by_bot_user_id="",
        decision_actor_type=MemoryDecisionActorType.REFLECTION,
        decision_actor_id="yuki_self_reflection",
        initiative_run_id=run_id,
        evidence_tool_receipt_id=receipt.id,
    )
    target = ResolvedSubject(
        MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我用运行结果校验了曲线绘图。",
        memory_key="self_fact:existing",
        category="self_fact",
        kind=MemoryKind.FACT,
        evidence_quote="已生成并校验曲线绘图",
    )
    created = await mutations.mutate_resolved(request, context, target=target)
    assert created.ok
    if operation == "restore":
        invalidated = await mutations.mutate_resolved(
            request.model_copy(
                update={
                    "operation": MemoryMutationOperation.INVALIDATE,
                    "fact_id": created.new_fact_id,
                    "target": None,
                    "new_content": None,
                }
            ),
            context,
            target=target,
        )
        assert invalidated.ok
    await finish(database, run_id)
    (batch,) = await claim(database)

    def respond(request):
        payload = json.loads(request.messages[-1].content)
        fact = next(
            item for item in payload["self_facts"] if item["memory_key"] == "self_fact:existing"
        )
        if operation == "restore":
            assert fact["status"] == "invalidated"
        return json.dumps(
            {
                "proposals": [
                    {
                        "operation": operation,
                        "fact_ref": fact["ref"],
                        "evidence_refs": ["tool_1"],
                        "reason": "实际工具回执支持本次维护",
                        "category": "self_reflection",
                        "importance": 5,
                        "confidence": 0.9,
                    }
                ]
            }
        )

    provider = FakeLLMProvider(responder=respond)
    repository = SelfReflectionRepository(database)
    reflection = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(batch) == (1, 1)
    assert await reflection.reflect(batch) == (1, 1)
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        mutation = await session.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.requested_operation == operation
            )
        )
        assert (
            mutation is not None
            and mutation.trigger_event_id is None
            and mutation.initiative_run_id == run_id
        )
        assert (
            await session.scalar(
                select(func.count(MemoryMutationReceiptModel.id)).where(
                    MemoryMutationReceiptModel.requested_operation == operation
                )
            )
            == 1
        )
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
    result = await facts.get_fact(mutation.new_fact_id)
    assert result.status is MemoryStatus.ACTIVE
    if operation == "restore":
        assert result.id == created.new_fact_id
    else:
        assert result.supersedes_id == created.new_fact_id
        assert result.category == "self_reflection" and result.importance == 5
    assert all(
        item.tool_receipt_id == receipt.id and item.event_id is None
        for item in await facts.list_evidence(result.id)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_refs", [False, True])
async def test_main_self_evidence_uses_actual_receipt_with_repeated_quote_and_partial_refs(
    database, explicit_refs
):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id, "first", result_excerpt="同一句实际工具结果")
    await record(database, event, run_id, "second", result_excerpt="同一句实际工具结果")
    await record(database, event, run_id, "third", result_excerpt="另一项未使用资料")
    async with database.sessions() as session:
        receipts = list(
            await session.scalars(
                select(MemoryToolReceiptModel).order_by(MemoryToolReceiptModel.id)
            )
        )
    context = MemoryMutationContext(
        event=None,
        conversation_key=event.scope.key,
        turn_origin="self_initiative",
        delegation_mode="main_agent",
        trigger_actor_user_id="",
        decision_actor_type=MemoryDecisionActorType.AGENT,
        decision_actor_id="execution-1",
        executed_by_bot_user_id=event.bot_user_id,
        initiative_run_id=run_id,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我根据真实工具结果继续了自主工作。",
        memory_key="self_episode:repeated-receipts",
        category="self_episode",
        kind=MemoryKind.EPISODE,
        evidence_quote="同一句实际工具结果",
        evidence_refs=tuple(f"tool_{row.id}" for row in receipts) if explicit_refs else (),
    )
    result = await mutations.mutate(request, context)
    assert result.ok, result.reason_code
    evidence = await facts.list_evidence(result.new_fact_id)
    assert len(evidence) == 1 and evidence[0].tool_receipt_id == receipts[1].id
    assert evidence[0].event_id is None
    duplicate = await mutations.mutate(request, context)
    assert duplicate.deduplicated and duplicate.new_fact_id == result.new_fact_id
    missing_source = await mutations.mutate(
        request.model_copy(update={"evidence_refs": ("tool_999999",)}), context
    )
    assert (
        not missing_source.ok and missing_source.reason_code == "initiative_tool_evidence_required"
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("large_event", [False, True])
async def test_incoming_reflection_keeps_complete_source_without_own_reply(database, large_event):
    mutations, facts, _event_record, _ = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    content = "我觉得你很会倾听。" + ("这是一段完整反馈。" * 100 if large_event else "")
    source = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content=content,
        group_id="3001",
    )
    assert await repository.scan_new_events() == 1
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        self_memory_enabled=True,
        memory_self_reflection_event_threshold=1,
    )
    snapshot = await ReflectionControlRepository(database, settings).snapshot()
    assert snapshot["actionable"]["events"] == 1
    (batch,) = await repository.claim_due(
        scheduled_slot="incoming",
        local_date="2026-10-10",
        event_threshold=1,
        character_threshold=100,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=100,
        cycle_id="incoming",
    )
    assert not batch.state.has_yuki_reply and not batch.state.has_tool_result
    assert [item.id for item in batch.events] == [source.id]
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=settings,
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(batch) == (0, 0)
    payload = json.loads(provider.requests[0].messages[-1].content)
    assert len(payload["events"]) == 1
    assert content in payload["events"][0]["rendered"]
    assert await reflection.reflect(batch) == (0, 0)
    assert len(provider.requests) == 1
    await repository.complete(batch, proposals=0, committed=0)
    assert await repository.completed_result(batch.run_id) == (0, 0)
    assert (await ReflectionControlRepository(database, settings).snapshot())["actionable"][
        "events"
    ] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_state",
    ["old_waiting", "old_isolated", "live_waiting", "live_isolated", "cross_generation"],
)
async def test_reflection_only_live_source_ranges_block_claim_and_cursor(database, source_state):
    mutations, _, initial, _ = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    old = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="旧范围反馈",
        group_id="3001",
    )
    crossing = None
    if source_state == "cross_generation":
        crossing = await _event(
            mutations._ledger,
            message_id=str(uuid4()),
            sender_user_id="1001",
            content="跨边界反馈",
            group_id="3001",
        )
    await repository.scan_new_events()
    options = dict(
        scheduled_slot="source-range",
        local_date="2026-10-10",
        event_threshold=1,
        character_threshold=100,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=16000,
    )
    (original,) = await repository.claim_due(**options, cycle_id="before-boundary")
    await repository.fail(original.run_id, "provider_unavailable")
    async with database.sessions() as session, session.begin():
        run = await session.get(MemorySelfReflectionRunModel, original.run_id)
        run.retry_state = "isolated" if source_state.endswith("isolated") else "waiting"
        run.next_attempt_at = (
            datetime.now(UTC) + timedelta(days=1) if source_state.startswith("live_") else None
        )
        fingerprint = run.input_fingerprint
        if not source_state.startswith("live_"):
            conversation = await session.get(
                CanonicalConversationModel, old.canonical_conversation_id
            )
            conversation.last_generation_change_event_id = old.id
    current = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="当前范围反馈",
        group_id="3001",
    )
    await repository.scan_new_events()
    options["scheduled_slot"] = "after-boundary"
    claimed = await repository.claim_due(**options, cycle_id="after-boundary")
    if crossing is not None:
        assert claimed == ()
    else:
        (batch,) = claimed
        assert [item.id for item in batch.events] == [current.id]
        await repository.complete(batch, proposals=0, committed=0)
        assert await repository.claim_due(**options, cycle_id="after-completion") == ()
    async with database.sessions() as session:
        run = await session.get(MemorySelfReflectionRunModel, original.run_id)
        state = await session.get(MemorySelfReflectionStateModel, original.state.id)
        assert run.status == "failed" and run.input_fingerprint == fingerprint
        assert run.first_event_id == old.id
        assert run.last_event_id == (crossing.id if crossing else old.id)
        if source_state.startswith("live_"):
            assert state.last_event_id == initial.id and state.pending_events == 1
        elif crossing is not None:
            assert run.retry_state == "isolated" and run.error_category == "source_range_changed"
            assert state.last_event_id == initial.id
        else:
            assert state.last_event_id == current.id and state.pending_events == 0
