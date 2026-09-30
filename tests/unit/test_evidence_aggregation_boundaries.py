"""Real confirm/version SQL slopes and the canonical reassignment copy fence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import insert
from tests.unit.test_memory_mutation import _context, _event, _service

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.memory.enums import MemoryAuthority, MemoryEvidenceRelation, MemoryScopeType
from qq_ai_bot.memory.evidence import MemoryEvidencePolicy
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel


async def _seed_evidence(database, count, *, group_id=None):
    mutations, facts, ledger, _processor = _service(database)
    source = await _event(
        ledger,
        message_id=str(uuid.uuid4()),
        sender_user_id="1001",
        content="稳定的事实证据",
        group_id=group_id,
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        original = await writer.get(ChatEventModel, source.id)
        template = {
            column.key: getattr(original, column.key)
            for column in ChatEventModel.__table__.columns
            if column.key not in {"id", "canonical_event_id", "platform_message_id"}
        }
        event_ids = [source.id]
        if count > 1:
            event_ids.extend(
                await writer.scalars(
                    insert(ChatEventModel).returning(ChatEventModel.id),
                    [
                        dict(
                            template,
                            canonical_event_id=str(uuid.uuid4()),
                            platform_message_id=str(uuid.uuid4()),
                        )
                        for _ in range(count - 1)
                    ],
                )
            )
        person = await ensure_person(writer, "1001")
        conversation = await writer.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        fact_id = await writer.scalar(
            insert(MemoryFactModel)
            .values(
                scope_type="person_group" if group_id else "person",
                canonical_subject_person_id=person,
                canonical_subject_space_id=conversation.space_id if group_id else None,
                kind="fact",
                category="profile",
                memory_key="stable:fact",
                content="稳定事实",
                normalized_content="稳定事实",
                source_type="automatic",
                authority="self_report",
                confidence=0.1,
                status="active",
                conflict_state="clear",
                review_state="verified",
                created_at=now,
                updated_at=now,
            )
            .returning(MemoryFactModel.id)
        )
        await writer.execute(
            insert(MemoryEvidenceModel),
            [
                dict(
                    fact_id=fact_id,
                    event_id=event_id,
                    source_speaker_user_id="1001",
                    relation="self_statement",
                    authority="self_report",
                    confidence=0.1,
                    excerpt="稳定的事实证据",
                    created_at=now,
                )
                for event_id in event_ids
            ],
        )
    return mutations, facts, ledger, fact_id


@pytest.mark.parametrize("entry", ["confirm", "version"])
@pytest.mark.parametrize("count", [1, 64, 1000])
async def test_history_prepared_before_first_write_with_bounded_sql_slope(database, entry, count):
    _mutations, facts, ledger, fact_id = await _seed_evidence(database, count)
    trigger = await _event(
        ledger, message_id=str(uuid.uuid4()), sender_user_id="1001", content="再次确认稳定事实"
    )
    evidence = MemoryEvidenceCreate(
        event_id=trigger.id,
        source_speaker_user_id="1001",
        relation=MemoryEvidenceRelation.CONFIRMATION,
        authority=MemoryAuthority.SELF_REPORT,
        confidence=0.2,
        excerpt=trigger.content,
    )
    writer_started = False
    reads = []

    def trace(_conn, _cursor, statement, _params, context, _many):
        nonlocal writer_started
        if context.isinsert or context.isupdate or context.isdelete:
            writer_started = True
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append((writer_started, statement))

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        if entry == "confirm":
            result = await facts.confirm_fact(fact_id, evidence)
        else:
            result = await facts.version_fact(
                fact_id,
                replacement=MemoryFactCreate(
                    scope_type="person",
                    subject_user_id="1001",
                    kind="fact",
                    memory_key="stable:fact",
                    category="profile",
                    content="修正事实",
                    source_type="automatic",
                    authority="self_report",
                ),
                evidence=evidence,
                actor_user_id="1001",
                reason_code="test_version",
                limit=None,
                copy_existing_evidence=True,
                confirmed_at=datetime.now(UTC),
            )
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", trace)
    assert result is not None and result.evidence_count == count + 1
    assert len(reads) <= 32  # This bound holds for 1, 64 and 1000 source rows.
    after_write = [sql for written, sql in reads if written]
    assert len(after_write) <= 12
    assert not any("memory_evidence" in sql.lower() for sql in after_write)
    async with database.sessions() as reader:
        rows = await facts.repository.list_evidence(result.id, limit=None, session=reader)
    policy = MemoryEvidencePolicy()
    assert result.authority is MemoryAuthority.SELF_REPORT
    assert result.confidence == pytest.approx(policy.aggregate(rows, authority=result.authority))
    assert len(rows) == count + 1


async def test_reassign_copies_only_readable_sources_and_recomputes_new_authority(database):
    mutations, facts, ledger, fact_id = await _seed_evidence(database, 1, group_id="3001")
    wrong_space = await _event(
        ledger,
        message_id=str(uuid.uuid4()),
        sender_user_id="1001",
        content="其他群中的无关证据",
        group_id="2001",
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(MemoryEvidenceModel).values(
                fact_id=fact_id,
                event_id=wrong_space.id,
                source_speaker_user_id="1001",
                relation="self_statement",
                authority="self_report",
                confidence=1.0,
                excerpt=wrong_space.content,
                created_at=datetime.now(UTC),
            )
        )
    trigger = await _event(
        ledger,
        message_id=str(uuid.uuid4()),
        sender_user_id="1001",
        content="其实这是另一个人的事实",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    result = await mutations.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.REASSIGN,
            fact_id=fact_id,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user", scope_type=MemoryScopeType.PERSON_GROUP
            ),
            reason="misattributed_subject",
        ),
        _context(trigger),
    )
    assert result.ok and result.new_fact_id is not None
    copied = await facts.get_fact(result.new_fact_id)
    assert copied.subject_user_id == "2002" and copied.group_id == "3001"
    async with database.sessions() as reader:
        rows = await facts.repository.list_evidence(copied.id, limit=None, session=reader)
    assert copied.evidence_count == len(rows) == 2
    assert wrong_space.id not in {row.event_id for row in rows}
    assert {row.authority for row in rows} == {MemoryAuthority.THIRD_PARTY}
    assert {row.relation for row in rows if row.event_id != trigger.id} == {
        MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
    }
    assert {row.relation for row in rows if row.event_id == trigger.id} == {
        MemoryEvidenceRelation.CORRECTION
    }
    assert copied.authority is MemoryAuthority.THIRD_PARTY
    assert copied.confidence == pytest.approx(
        MemoryEvidencePolicy().aggregate(rows, authority=copied.authority)
    )
