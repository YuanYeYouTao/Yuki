"""Checkpoint publication cannot restore source text erased during model work."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_mutation import _event, _service
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.partition import MemoryPartitionResolutionError
from qq_ai_bot.memory.self_reflection.models import StoredToolReceipt
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import (
    _CHECKPOINT,
    ReflectionCheckpoint,
    SelfReflectionService,
)
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionRunModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository


async def checkpoint_source(database, *, tool=False):
    source = await _append_event(
        EventLedgerRepository(database),
        message_id="checkpoint-private-source",
        group_id="2001",
        content="需要遗忘的原事件正文",
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        conversation = await writer.get(
            CanonicalConversationModel, source.canonical_conversation_id
        )
        run = MemorySelfReflectionRunModel(
            conversation_key_hash="a" * 64,
            bot_user_id=source.bot_user_id,
            canonical_space_id=conversation.space_id,
            scheduled_slot="checkpoint-private-source",
            trigger_reason="manual",
            first_event_id=source.id,
            last_event_id=source.id,
            status="failed",
            retry_state="isolated",
            attempt_count=3,
            proposal_count=0,
            committed_count=0,
            started_at=now,
        )
        writer.add(run)
        await writer.flush()
        run_id = run.id
        tool_map = {}
        if tool:
            receipt = MemoryToolReceiptModel(
                conversation_key_hash=run.conversation_key_hash,
                trigger_event_id=source.id,
                canonical_space_id=conversation.space_id,
                bot_user_id=source.bot_user_id,
                provider_id="core",
                tool_name="terminal_exec",
                success=True,
                result_excerpt="需要遗忘的原工具正文",
                result_characters=12,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
            writer.add(receipt)
            await writer.flush()
            tool_map = {
                "tool_1": StoredToolReceipt(
                    receipt.id,
                    source.id,
                    receipt.tool_name,
                    True,
                    receipt.result_excerpt,
                    bot_user_id=source.bot_user_id,
                    occurred_at=now,
                )
            }
    raw = _CHECKPOINT.dump_json(
        ReflectionCheckpoint(
            {"proposals": [], "episodes": []}, {} if tool else {"event_1": source}, tool_map
        )
    ).decode()
    return SelfReflectionRepository(database), source, run_id, raw


@pytest.mark.parametrize("tool", [False, True])
async def test_late_initial_checkpoint_cannot_restore_forgotten_source(database, tool):
    repository, source, run_id, raw = await checkpoint_source(database, tool=tool)
    assert await PeopleRepository(database).delete_person_id(source.author_person_id)
    with pytest.raises(MemoryPartitionResolutionError, match="checkpoint_source_changed"):
        await repository.save_checkpoint(run_id, raw)
    assert await repository.load_checkpoint(run_id) is None
    async with database.sessions() as reader:
        assert await reader.get(ChatEventModel, source.id) is None
        run = await reader.get(MemorySelfReflectionRunModel, run_id)
        assert (run.status, run.retry_state, run.attempt_count) == ("failed", "isolated", 3)


async def test_final_checkpoint_cas_cannot_restore_privacy_cleared_original(database):
    repository, source, run_id, raw = await checkpoint_source(database)
    await repository.save_checkpoint(run_id, raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert await PeopleRepository(database).delete_person_id(source.author_person_id)
    assert await repository.load_checkpoint(run_id) is None
    with pytest.raises(MemoryPartitionResolutionError, match="checkpoint_changed"):
        await repository.save_checkpoint(run_id, raw, expected=raw)
    assert await repository.load_checkpoint(run_id) is None
    assert hashlib.sha256(raw.encode()).hexdigest() == digest


async def test_forget_commit_between_source_read_and_checkpoint_write_rechecks_wal(
    database, monkeypatch
):
    repository, source, run_id, raw = await checkpoint_source(database)
    execute = AsyncSession.execute
    erased = False
    errors = []

    async def interleave(session, statement, *args, **kwargs):
        nonlocal erased
        if not erased and str(statement).startswith(
            "UPDATE memory_self_reflection_runs SET checkpoint_json="
        ):
            erased = True
            assert await PeopleRepository(database).delete_person_id(source.author_person_id)
        return await execute(session, statement, *args, **kwargs)

    def record_error(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    monkeypatch.setattr(AsyncSession, "execute", interleave)
    sql_event.listen(database.engine.sync_engine, "handle_error", record_error)
    try:
        with pytest.raises(MemoryPartitionResolutionError, match="checkpoint_source_changed"):
            await repository.save_checkpoint(run_id, raw)
    finally:
        sql_event.remove(database.engine.sync_engine, "handle_error", record_error)
    assert erased and 517 in errors
    assert await repository.load_checkpoint(run_id) is None
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(ChatEventModel.id).where(ChatEventModel.id == source.id))
            is None
        )


async def test_empty_output_finishes_without_snapshotting_unreferenced_forgotten_source(database):
    mutations, _, ledger, _ = _service(database, self_memory_enabled=True)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    source = await _event(
        ledger,
        message_id="empty-output-private-source",
        sender_user_id="1001",
        group_id="2001",
        content="没有形成新记忆的个人消息",
    )
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="empty-output",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=10,
        max_characters=16000,
        force=True,
    )

    class ErasingProvider(FakeLLMProvider):
        async def complete(self, request):
            assert await PeopleRepository(database).delete_person_id(source.author_person_id)
            return await super().complete(request)

    provider = ErasingProvider(lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
    )
    assert await reflection.reflect(batch) == (0, 0)
    raw = await repository.load_checkpoint(batch.run_id)
    checkpoint = _CHECKPOINT.validate_json(raw)
    assert not checkpoint.event_map and not checkpoint.tool_map
    assert source.content not in raw and "payload" not in raw
    await repository.complete(batch, proposals=0, committed=0)
    assert len(provider.requests) == 1
