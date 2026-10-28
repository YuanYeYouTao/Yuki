"""Retired SELF candidate history follows its real erased source, never transport labels."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from tests.support.event_seed import seed_event, seed_scoped_event
from tests.unit.test_memory_v2 import _append_event

from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.identity.canonical_repository import create_person_binding, create_presence
from yuki.persistence.models import (
    ChatEventModel,
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
)
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _candidate(database, *events, status="pending"):
    """Persist the actual historical layout without reviving its retired producer."""
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        row = MemoryClaimCandidateModel(
            fingerprint=uuid4().hex + uuid4().hex,
            candidate_type="self",
            target_scope="self",
            subject_user_id=events[0].sender_user_id,
            group_id=events[0].group_id,
            target_fingerprint=uuid4().hex + uuid4().hex,
            normalized_memory_key="original:self",
            content="原来源中的私人评价候选",
            subject_basis="",
            retention="",
            source_style="",
            confidence=0.8,
            status=status,
            evidence_count=len(events),
            expires_at=now + timedelta(days=7),
            created_at=now,
            updated_at=now,
        )
        writer.add(row)
        await writer.flush()
        writer.add_all(
            MemoryClaimCandidateEvidenceModel(
                candidate_id=row.id, event_id=event.id, created_at=now
            )
            for event in events
        )
        return row.id


def _values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


async def test_forget_erases_historical_candidate_body_in_every_original_status(database):
    ledger = EventLedgerRepository(database)
    erased = await _append_event(ledger, message_id="erased-private", content="原私人评价")
    retained = await _append_event(
        ledger, message_id="retained-private", user_id="1002", content="其他人物原资料"
    )
    candidates = [
        (
            await _candidate(database, erased, status=status),
            await _candidate(database, retained, status=status),
        )
        for status in ("pending", "accepted", "rejected", "expired")
    ]
    async with database.read_session() as reader:
        before = {
            key: _values(await reader.get(MemoryClaimCandidateModel, key)) for _, key in candidates
        }
    assert await PeopleRepository(database).delete_person_id(erased.author_person_id)
    async with database.read_session() as reader:
        assert await reader.get(ChatEventModel, erased.id) is None
        for erased_candidate, retained_candidate in candidates:
            assert await reader.get(MemoryClaimCandidateModel, erased_candidate) is None
            assert not list(
                await reader.scalars(
                    select(MemoryClaimCandidateEvidenceModel).where(
                        MemoryClaimCandidateEvidenceModel.candidate_id == erased_candidate
                    )
                )
            )
            assert (
                _values(await reader.get(MemoryClaimCandidateModel, retained_candidate))
                == before[retained_candidate]
            )
        assert (await reader.get(ChatEventModel, retained.id)).content == "其他人物原资料"


async def test_any_erased_candidate_source_cleans_body_without_clearing_unrelated_group(database):
    ledger = EventLedgerRepository(database)
    erased = await _append_event(ledger, message_id="erased-group", group_id="2001")
    other = await _append_event(ledger, message_id="other-group", user_id="1002", group_id="2001")
    mixed = await _candidate(database, other, erased)
    unrelated = await _candidate(database, other)
    assert await PeopleRepository(database).delete_person_id(erased.author_person_id)
    async with database.read_session() as reader:
        assert await reader.get(MemoryClaimCandidateModel, mixed) is None
        assert await reader.get(MemoryClaimCandidateModel, unrelated) is not None
        assert await reader.get(ChatEventModel, other.id) is not None
        assert set(
            await reader.scalars(select(MemoryClaimCandidateEvidenceModel.candidate_id))
        ) == {unrelated}


async def test_qq_forget_keeps_same_number_telegram_candidate_and_source(database):
    qq = await _append_event(EventLedgerRepository(database), message_id="qq-private")
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        binding = await create_person_binding(
            writer,
            external_account_id="1001",
            display_name="TG owner",
            now=now,
            platform="telegram",
        )
        presence = await create_presence(
            writer, external_account_id="telegram-bot", now=now, platform="telegram"
        )
        tg_person, tg_presence = binding.person_id, presence.id
    telegram = (
        await seed_scoped_event(
            ScopedEventLedgerUnitOfWork(database),
            scope=ConversationScope.private(
                "telegram-bot", "1001", platform="telegram", presence_id=tg_presence
            ),
            sender_user_id="1001",
            origin_key="tg-private",
            direction="inbound",
            content="TG同号不同人物的原资料",
        )
    ).event
    assert telegram.author_person_id == tg_person != qq.author_person_id
    qq_candidate = await _candidate(database, qq)
    tg_candidate = await _candidate(database, telegram)
    async with database.read_session() as reader:
        before = _values(await reader.get(MemoryClaimCandidateModel, tg_candidate))
    assert await PeopleRepository(database).delete_person_id(qq.author_person_id)
    async with database.read_session() as reader:
        assert await reader.get(MemoryClaimCandidateModel, qq_candidate) is None
        assert _values(await reader.get(MemoryClaimCandidateModel, tg_candidate)) == before
        assert (await reader.get(ChatEventModel, telegram.id)).author_person_id == tg_person
        assert set(await reader.scalars(select(MemoryClaimCandidateEvidenceModel.event_id))) == {
            telegram.id
        }


async def test_forget_cleans_candidate_of_other_authors_actually_redacted_mention(database):
    ledger = EventLedgerRepository(database)
    owner = await _append_event(ledger, message_id="private-owner")
    mention, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        origin_key="actual-private-mention",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1002",
        direction="inbound",
        content="1001的私人资料",
        segments=({"type": "at", "data": {"qq": "1001"}},),
    )
    candidate = await _candidate(database, mention)
    assert mention.author_person_id != owner.author_person_id
    assert await PeopleRepository(database).delete_person_id(owner.author_person_id)
    async with database.read_session() as reader:
        assert await reader.get(MemoryClaimCandidateModel, candidate) is None
        remaining = await reader.get(ChatEventModel, mention.id)
        assert remaining is not None and remaining.content == "[已删除用户]的私人资料"
