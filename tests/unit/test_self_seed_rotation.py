"""Bounded, restartable SELF changes do not rescan old facts or miss new evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.unit.test_self_initiative_memory_quality import reflection_fact

from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.self_origin import read_self_seed_page
from qq_ai_bot.persistence.models import MemoryEvidenceModel, MemoryFactModel

pytestmark = pytest.mark.asyncio


async def _seeds(database, count=6):
    facts, event, _, batch, first_id = await reflection_fact(database)
    ids = [first_id]
    for index in range(1, count):
        fact = await facts.remember(
            MemoryFactCreate(
                scope_type="self",
                visibility_type="group",
                visibility_group_id="3001",
                kind="episode",
                memory_key=f"self_episode:rotation:{index}",
                category="self_episode",
                content=f"合成经历 {index}：我完成了另一项绘图。",
                source_type="automatic",
                authority="agent_reflection",
                importance=5 if index < 4 else 1,
            ),
            evidence=MemoryEvidenceCreate(
                tool_receipt_id=batch.first_receipt_id,
                source_speaker_user_id="8000",
                relation="agent_reflection",
                authority="agent_reflection",
                excerpt="已生成并校验曲线绘图",
            ),
        )
        ids.append(fact.id)
    return event, ids, batch


async def test_change_cursor_reaches_lower_priority_facts_and_never_wraps(database):
    event, ids, _ = await _seeds(database)
    # Equal timestamps still page each fact once using ID as the tie-breaker.
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryFactModel).values(
                updated_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    arguments = {"canonical_conversation_id": event.canonical_conversation_id, "limit": 2}
    first = await read_self_seed_page(database, **arguments)
    assert [f.id for f in first.facts] == ids[:2]
    assert first.next_cursor[1] == ids[1] and first.scanned == 2
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == ids[0])
            .values(
                importance=5,
                updated_at=datetime.now(UTC),
                content="合成经历的修订：完成了绘图校验。",
            )
        )
    restored_cursor = tuple(json.loads(json.dumps(first.next_cursor)))
    second = await read_self_seed_page(database, cursor=restored_cursor, **arguments)
    third = await read_self_seed_page(database, cursor=second.next_cursor, **arguments)
    assert [f.id for f in (*second.facts, *third.facts)] == ids[2:]
    changed = await read_self_seed_page(database, cursor=third.next_cursor, **arguments)
    assert [f.id for f in changed.facts] == [ids[0]]
    assert changed.facts[0].content == "合成经历的修订：完成了绘图校验。"
    ended = await read_self_seed_page(database, cursor=changed.next_cursor, **arguments)
    assert not ended.facts and ended.scanned == 0 and ended.next_cursor == changed.next_cursor


async def test_invalid_lineage_advances_empty_page_with_bounded_work(database):
    event, ids, _ = await _seeds(database, count=4)
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryEvidenceModel)
            .where(
                MemoryEvidenceModel.fact_id.in_(ids[:2]),
            )
            .values(excerpt="并非真实工具回执中的文本")
        )
    arguments = {
        "canonical_conversation_id": event.canonical_conversation_id,
        "limit": 4,
        "scan_limit": 2,
    }
    first = await read_self_seed_page(database, **arguments)
    assert first.facts == () and first.scanned == 2 and first.next_cursor[1] == ids[1]
    second = await read_self_seed_page(database, cursor=first.next_cursor, **arguments)
    assert [f.id for f in second.facts] == ids[2:] and second.scanned == 2
    missing = await read_self_seed_page(
        database,
        canonical_conversation_id="missing",
        cursor=second.next_cursor,
    )
    assert not missing.facts and missing.scanned == 0 and missing.next_cursor == second.next_cursor
    with pytest.raises(ValueError, match="nonnegative"):
        await read_self_seed_page(database, cursor=(second.next_cursor[0], -1), **arguments)
    async with database.sessions() as db:
        assert len(list(await db.scalars(select(MemoryFactModel.id)))) == 4


async def test_new_evidence_advances_fact_change_time_but_duplicate_does_not(database):
    event, ids, batch = await _seeds(database, count=2)
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryEvidenceModel)
            .where(
                MemoryEvidenceModel.fact_id == ids[0],
            )
            .values(excerpt="失效来源，不在工具回执中")
        )
    arguments = {"canonical_conversation_id": event.canonical_conversation_id}
    scanned = await read_self_seed_page(database, **arguments)
    assert [f.id for f in scanned.facts] == [ids[1]]
    evidence = MemoryEvidenceCreate(
        tool_receipt_id=batch.last_receipt_id,
        source_speaker_user_id="8000",
        relation="agent_reflection",
        authority="agent_reflection",
        excerpt="已生成并校验曲线绘图",
    )
    async with database.immediate_session() as db:
        assert await MemoryFactRepository(database).add_evidence(ids[0], evidence, session=db)
    repaired = await read_self_seed_page(database, cursor=scanned.next_cursor, **arguments)
    assert [f.id for f in repaired.facts] == [ids[0]]
    async with database.immediate_session() as db:
        before = (await db.get(MemoryFactModel, ids[0])).updated_at
        assert not await MemoryFactRepository(database).add_evidence(ids[0], evidence, session=db)
        assert (await db.get(MemoryFactModel, ids[0])).updated_at == before
    assert not (await read_self_seed_page(database, cursor=repaired.next_cursor, **arguments)).facts


async def test_each_scan_has_a_fixed_time_ceiling(database, monkeypatch):
    event, ids, _ = await _seeds(database, count=2)
    ceiling = datetime.now(UTC) - timedelta(seconds=10)
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == ids[0])
            .values(
                updated_at=ceiling,
            )
        )
        await db.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == ids[1])
            .values(
                updated_at=ceiling + timedelta(seconds=1),
            )
        )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ceiling

    monkeypatch.setattr("qq_ai_bot.memory.self_origin.datetime", Clock)
    arguments = {"canonical_conversation_id": event.canonical_conversation_id}
    first = await read_self_seed_page(database, **arguments)
    assert [f.id for f in first.facts] == [ids[0]]
    ceiling += timedelta(seconds=2)
    next_page = await read_self_seed_page(database, cursor=first.next_cursor, **arguments)
    assert [f.id for f in next_page.facts] == [ids[1]]


async def test_future_valid_from_is_discovered_once_after_prior_high_water(database, monkeypatch):
    event, ids, _ = await _seeds(database, count=2)
    ceiling = datetime.now(UTC)
    activation = ceiling + timedelta(seconds=60)
    async with database.immediate_session() as db:
        await db.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == ids[0])
            .values(updated_at=ceiling - timedelta(seconds=60), valid_from=activation)
        )
        await db.execute(
            update(MemoryFactModel).where(MemoryFactModel.id == ids[1]).values(updated_at=ceiling)
        )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ceiling

    monkeypatch.setattr("qq_ai_bot.memory.self_origin.datetime", Clock)
    arguments = {"canonical_conversation_id": event.canonical_conversation_id}
    first = await read_self_seed_page(database, **arguments)
    assert [f.id for f in first.facts] == [ids[1]]
    assert not (await read_self_seed_page(database, cursor=first.next_cursor, **arguments)).facts
    ceiling = activation
    activated = await read_self_seed_page(database, cursor=first.next_cursor, **arguments)
    assert [f.id for f in activated.facts] == [ids[0]]
    assert activated.next_cursor == (activation.isoformat(), ids[0])
    ended = await read_self_seed_page(database, cursor=activated.next_cursor, **arguments)
    assert not ended.facts and ended.next_cursor == activated.next_cursor
