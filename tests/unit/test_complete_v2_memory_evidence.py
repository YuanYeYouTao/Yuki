"""C21 slice 2B2b: evidence chain ownership and Person-level forgetme."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_complete_v2_memory_owner import (
    _person_fact,
    _person_group_fact,
    _self_fact,
)
from tests.unit.test_complete_v2_memory_owner_backfill import (
    _insert_fact,
    _open,
)
from tests.unit.test_complete_v2_memory_owner_cutover import _ready
from tests.unit.test_complete_v2_processor_runtime import _flip_v2
from tests.unit.test_identity_backfill import _insert_people as _legacy_people
from tests.unit.test_identity_backfill import _service as _backfill_service
from tests.unit.test_identity_backfill import _settings as _backfill_settings
from tests.unit.test_identity_cutover import _insert_event as _cutover_insert_event
from tests.unit.test_identity_cutover import _insert_memory_fact_person, _prepare_snapshots
from tests.unit.test_identity_cutover import _insert_people as _cutover_insert_people
from tests.unit.test_identity_cutover import _open as _cutover_open
from tests.unit.test_identity_cutover import _service as _cutover_service
from tests.unit.test_migration_0043 import _upgrade
from tests.unit.test_user_profiles import _flip_complete_v2
from tests.unit.test_v1_person_agent import inbound

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.canonical_event_schema import (
    CHAT_EVENT_CANONICAL_SHADOW_TRIGGERS,
)
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.backfill_repository import IdentityBackfillRepository
from qq_ai_bot.identity.c21_evidence import C21_EVIDENCE_INCOMPLETE
from qq_ai_bot.identity.canonical_memory_owners import (
    c21_signature_chunks,
    c21_source_material,
)
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.identity.dual_write import (
    _create_person_binding,
    ensure_canonical_presence_preconfig,
    ensure_v2_space,
    set_identity_failpoint,
)
from qq_ai_bot.identity.inventory import (
    CUTOVER_BASELINE_PENDING,
    DEFERRED_SHADOWS,
    IDENTITY_PLATFORM,
    LEGACY_PROVENANCE_RETAINED,
)
from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event
from qq_ai_bot.memory.dream.db_models import MemoryDreamClusterModel, MemoryDreamRunModel
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate, MemoryFactQuery
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryJobModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
    PersonModel,
)
from qq_ai_bot.persistence.repositories import PeopleRepository
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_NOW_TEXT = "2026-08-24T00:00:00+00:00"
_SRC = Path("src/qq_ai_bot")
_HASH = "a" * 64
_MARKER = "[已删除用户]"


def _legacy_event(
    connection,
    *,
    message_id: str,
    sender: str,
    peer: str | None = None,
    group: str | None = None,
    content: str = "secret-body",
) -> int:
    scope = "group" if group else "private"
    cursor = connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, event_kind, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES ('8000', ?, ?, ?, ?, ?, 'inbound', 'message', ?, '', '[]', "
        "'user_message', ?, ?)",
        (message_id, scope, group, peer, sender, content, _NOW_TEXT, _NOW_TEXT),
    )
    return int(cursor.lastrowid)


def _legacy_evidence(
    connection,
    *,
    fact_id: int,
    event_id: int,
    relation: str = "self_statement",
    excerpt: str = "x",
) -> None:
    connection.execute(
        "INSERT INTO memory_evidence("
        "fact_id, event_id, source_speaker_user_id, relation, confidence, "
        "authority, excerpt, created_at"
        ") VALUES (?, ?, '1001', ?, 1.0, 'self_report', ?, ?)",
        (fact_id, event_id, relation, excerpt, _NOW_TEXT),
    )


def _set_event_suppression(connection, event_id: int, status: str) -> None:
    # C4 only admits NULL/keeper/duplicate on write; preexisting suppressed/unknown
    # still exist and must be hidden by event_suppression_is_hidden.
    connection.execute("DROP TRIGGER IF EXISTS trg_chat_events_canonical_shadow_update")
    try:
        connection.execute(
            "UPDATE chat_events SET suppression_status = ? WHERE id = ?",
            (status, event_id),
        )
    finally:
        connection.execute(CHAT_EVENT_CANONICAL_SHADOW_TRIGGERS[1])


def _legacy_receipt(connection, *, event_id: int, person_id: str) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_tool_receipts("
        "conversation_key_hash, trigger_event_id, bot_user_id, provider_id, tool_name, "
        "success, result_excerpt, result_characters, created_at, expires_at, "
        "canonical_person_id"
        ") VALUES (?, ?, '8000', 'test', 'web_search', 1, 'ok', 2, ?, ?, ?)",
        (_HASH, event_id, _NOW_TEXT, "2026-12-31T00:00:00+00:00", person_id),
    )
    return int(cursor.lastrowid)


def _legacy_receipt_evidence(connection, *, fact_id: int, tool_receipt_id: int) -> None:
    connection.execute(
        "INSERT INTO memory_evidence("
        "fact_id, tool_receipt_id, source_speaker_user_id, relation, confidence, "
        "authority, excerpt, created_at"
        ") VALUES (?, ?, '1001', 'self_statement', 1.0, 'self_report', 'x', ?)",
        (fact_id, tool_receipt_id, _NOW_TEXT),
    )


def _seed_real_legacy_identity_blank(connection) -> dict[str, int]:
    _legacy_people(connection, "1001")
    _legacy_people(connection, "8000", is_bot=1)
    event_id = _legacy_event(connection, message_id="legacy-p", sender="1001", peer="1001")
    fact_id = _insert_fact(connection, user_id="1001", person_id=None)
    _legacy_evidence(connection, fact_id=fact_id, event_id=event_id)
    return {"event": event_id, "fact": fact_id}


def _space_only_fact() -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=MemoryScopeType.GROUP,
        group_id="2001",
        kind=MemoryKind.FACT,
        memory_key="space-rule",
        category="test",
        content="群公约",
        importance=3,
        confidence=0.8,
        source_type=MemorySourceType.EXPLICIT,
    )


def _person_query(user_id: str) -> MemoryFactQuery:
    return MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id=user_id)


async def _seed_person(
    session,
    *,
    external_id: str,
    display_name: str,
    extras: tuple[str, ...] = (),
):
    created = await _create_person_binding(
        session, external_id=external_id, display_name=display_name, now=_NOW
    )
    for extra in extras:
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=extra,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return created


async def _add_conversation(
    session,
    *,
    conversation_id: str,
    kind: str,
    person_id: str | None = None,
    space_id: str | None = None,
    scope_key: str,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind=kind,
            person_id=person_id,
            space_id=space_id,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=1,
            starts_after_event_id=0,
            last_event_id=0,
            last_generation_change_event_id=0,
            covered_through_event_id=0,
            uncovered_event_count=0,
            uncovered_character_count=0,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(
        ConversationLegacyAliasModel(
            id=alias_id,
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _event(
    *,
    message_id: str,
    sender_user_id: str,
    author_person_id: str,
    conversation_id: str,
    canonical_event_id: str | None,
    suppression_status: str | None = "keeper",
    group_id: str | None = None,
    author_kind: str | None = AuthorKind.PERSON.value,
) -> ChatEventModel:
    return ChatEventModel(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type="group" if group_id else "private",
        group_id=group_id,
        private_peer_user_id=None if group_id else sender_user_id,
        sender_user_id=sender_user_id,
        direction="inbound",
        event_kind="message",
        content="喜欢喝茶",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        author_kind=author_kind,
        author_person_id=author_person_id,
        canonical_event_id=canonical_event_id,
        canonical_conversation_id=conversation_id,
        suppression_status=suppression_status,
    )


async def _add_evidence(facts: MemoryFactRepository, fact_id: int, event_id: int) -> bool:
    async with facts.database.sessions() as session, session.begin():
        return await facts.add_evidence(
            fact_id,
            MemoryEvidenceCreate(
                event_id=event_id,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                authority=MemoryAuthority.SELF_REPORT,
                excerpt="ok",
            ),
            session=session,
        )


@pytest.mark.asyncio
async def test_multi_binding_sees_same_person_evidence_not_other_owners(
    database: Database,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(
            session, external_id="1001", display_name="P", extras=("1002",)
        )
        await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        conv_id = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=conv_id,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_id = person_p.person_id
        cid = conv_id
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        p_fact = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        q_fact = await facts.create_fact(
            _person_fact("1003", memory_key="q-likes"),
            normalized_content="别人的茶",
            supersedes_id=None,
            session=session,
        )
        space_fact = await facts.create_fact(
            _person_group_fact("1003", "2001"),
            normalized_content="群里别人",
            supersedes_id=None,
            session=session,
        )
        keeper = _event(
            message_id="keep-p",
            sender_user_id="1001",
            author_person_id=p_id,
            conversation_id=cid,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(keeper)
        await session.flush()
        keeper_id = int(keeper.id)
        p_fact_id = int(p_fact.id)
        q_fact_id = int(q_fact.id)
        space_fact_id = int(space_fact.id)
    assert await _add_evidence(facts, p_fact_id, keeper_id) is True
    seen_by_b = await facts.list_facts(_person_query("1002"), limit=20)
    seen_by_q = await facts.list_facts(_person_query("1003"), limit=20)
    projected = await facts.list_person_facts_projected_to_group("1002", "2001")
    evidence = await facts.list_evidence(p_fact_id)
    assert {row.id for row in seen_by_b} == {p_fact_id}
    assert {row.id for row in seen_by_q} == {q_fact_id}
    assert {row.id for row in projected} == {p_fact_id}
    assert [row.event_id for row in evidence] == [keeper_id]
    assert space_fact_id not in {row.id for row in seen_by_b}


@pytest.mark.asyncio
async def test_non_live_and_legacy_evidence_hidden_keeper_visible(
    database: Database,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        space_id = await ensure_v2_space(session, "2001")
        conv_id = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=conv_id,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_id = person_p.person_id
        cid = conv_id
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        created = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        rows = []
        for name, status, canonical in (
            ("keep", "keeper", str(uuid4())),
            ("null", None, str(uuid4())),
            ("dup", "duplicate", str(uuid4())),
            ("sup", "suppressed", str(uuid4())),
            ("unk", "unknown", str(uuid4())),
            ("legacy", "keeper", None),
        ):
            event = _event(
                message_id=name,
                sender_user_id="1001",
                author_person_id=p_id,
                conversation_id=cid,
                canonical_event_id=canonical,
                suppression_status=status,
                group_id="2001",
            )
            session.add(event)
            rows.append((name, event))
        await session.flush()
        fact_id = int(created.id)
        event_ids = {name: int(event.id) for name, event in rows}
    assert await _add_evidence(facts, fact_id, event_ids["keep"]) is True
    assert await _add_evidence(facts, fact_id, event_ids["null"]) is True
    assert await _add_evidence(facts, fact_id, event_ids["dup"]) is False
    async with database.sessions() as session, session.begin():
        for name in ("dup", "sup", "unk", "legacy"):
            session.add(
                MemoryEvidenceModel(
                    fact_id=fact_id,
                    event_id=event_ids[name],
                    source_speaker_user_id="1001",
                    relation=MemoryEvidenceRelation.SELF_STATEMENT.value,
                    confidence=1.0,
                    authority=MemoryAuthority.SELF_REPORT.value,
                    excerpt="hidden",
                    created_at=_NOW,
                )
            )
    visible = await facts.list_evidence(fact_id)
    assert {row.event_id for row in visible} == {event_ids["keep"], event_ids["null"]}


@pytest.mark.asyncio
async def test_binding_b_forgetme_wipes_person_memory_keeps_others(
    database: Database,
) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(
            session, external_id="1001", display_name="P", extras=("1002",)
        )
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        private_conv = str(uuid4())
        group_conv = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=private_conv,
            kind="private",
            person_id=person_p.person_id,
            scope_key="bot:8000:private:1001",
        )
        await _add_conversation(
            session,
            conversation_id=group_conv,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_id = person_p.person_id
        q_id = person_q.person_id
        s_id = space_id
        gconv = group_conv
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        p_person = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        p_group = await facts.create_fact(
            _person_group_fact("1001", "2001"),
            normalized_content="在群里喝茶",
            supersedes_id=None,
            session=session,
        )
        p_self = await facts.create_fact(
            _self_fact(visibility=SelfMemoryVisibility.PRIVATE, visibility_user_id="1001"),
            normalized_content="私下语气",
            supersedes_id=None,
            session=session,
        )
        global_self = await facts.create_fact(
            _self_fact(visibility=SelfMemoryVisibility.GLOBAL),
            normalized_content="全局语气",
            supersedes_id=None,
            session=session,
        )
        q_person = await facts.create_fact(
            _person_fact("1003", memory_key="q-likes"),
            normalized_content="别人的茶",
            supersedes_id=None,
            session=session,
        )
        space_only = await facts.create_fact(
            _space_only_fact(),
            normalized_content="群公约",
            supersedes_id=None,
            session=session,
        )
        keeper = _event(
            message_id="p-keep",
            sender_user_id="1001",
            author_person_id=p_id,
            conversation_id=gconv,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        q_keeper = _event(
            message_id="q-keep",
            sender_user_id="1003",
            author_person_id=q_id,
            conversation_id=gconv,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(keeper)
        session.add(q_keeper)
        await session.flush()
        session.add(
            MemoryJobModel(
                event_id=int(keeper.id),
                conversation_key=f"person:{p_id}",
                canonical_person_id=p_id,
                status="pending",
                attempts=0,
                next_attempt_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                processing_source="live",
            )
        )
        session.add(
            MemoryToolReceiptModel(
                conversation_key_hash=_HASH,
                trigger_event_id=int(keeper.id),
                bot_user_id="8000",
                provider_id="test",
                tool_name="web_search",
                success=True,
                result_excerpt="ok",
                result_characters=2,
                created_at=_NOW,
                expires_at=_NOW,
                canonical_person_id=p_id,
            )
        )
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash=_HASH,
                bot_user_id="8000",
                scope_type="private",
                private_peer_user_id="1001",
                last_event_id=int(keeper.id),
                latest_event_id=int(keeper.id),
                pending_events=1,
                pending_characters=1,
                has_yuki_reply=False,
                has_tool_result=False,
                high_value_signal=False,
                updated_at=_NOW,
                canonical_person_id=p_id,
            )
        )
        session.add(
            MemorySelfReflectionRunModel(
                conversation_key_hash=_HASH,
                bot_user_id="8000",
                scheduled_slot="2026-08-24:04",
                trigger_reason="manual",
                first_event_id=int(keeper.id),
                last_event_id=int(keeper.id),
                status="completed",
                proposal_count=0,
                committed_count=0,
                started_at=_NOW,
                canonical_person_id=p_id,
            )
        )
        session.add(
            MemoryDreamRunModel(
                public_id=str(uuid4()),
                mode="incremental",
                status="planned",
                snapshot_max_fact_id=1,
                snapshot_created_at=_NOW,
                statistics_json="{}",
                model_calls=0,
                completed_clusters=0,
                failed_clusters=0,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await session.flush()
        run_id = int((await session.scalars(select(MemoryDreamRunModel.id))).first())
        session.add(
            MemoryDreamClusterModel(
                run_id=run_id,
                cluster_key="cluster-p",
                partition_key=f"person:{p_id}",
                bot_user_id="8000",
                kind="fact",
                status="pending",
                fact_ids_json="[1]",
                fingerprint=_HASH,
                attempts=0,
                model_calls=0,
                operation_count=0,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_subject_person_id=p_id,
            )
        )
        ids = {
            "p_person": int(p_person.id),
            "p_group": int(p_group.id),
            "p_self": int(p_self.id),
            "global_self": int(global_self.id),
            "q_person": int(q_person.id),
            "space_only": int(space_only.id),
            "p_id": p_id,
            "q_id": q_id,
            "s_id": s_id,
            "run_id": run_id,
            "p_event": int(keeper.id),
            "q_event": int(q_keeper.id),
        }
    assert await _add_evidence(facts, ids["p_person"], ids["p_event"]) is True
    assert await _add_evidence(facts, ids["q_person"], ids["q_event"]) is True
    assert await PeopleRepository(database).delete_person("1002") is True
    async with database.sessions() as session:
        leftover_facts = {
            int(row.id): row for row in await session.scalars(select(MemoryFactModel))
        }
        assert ids["p_person"] not in leftover_facts
        assert ids["p_group"] not in leftover_facts
        assert ids["p_self"] not in leftover_facts
        assert ids["global_self"] in leftover_facts
        assert ids["q_person"] in leftover_facts
        assert ids["space_only"] in leftover_facts
        assert leftover_facts[ids["space_only"]].canonical_subject_space_id == ids["s_id"]
        assert leftover_facts[ids["space_only"]].canonical_subject_person_id is None
        leftover_evidence = list(await session.scalars(select(MemoryEvidenceModel)))
        assert {row.fact_id for row in leftover_evidence} == {ids["q_person"]}
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.canonical_person_id == ids["p_id"])
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.canonical_person_id == ids["p_id"])
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemorySelfReflectionStateModel)
                .where(MemorySelfReflectionStateModel.canonical_person_id == ids["p_id"])
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.canonical_person_id == ids["p_id"])
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemoryDreamClusterModel)
                .where(MemoryDreamClusterModel.canonical_subject_person_id == ids["p_id"])
            )
            == 0
        )
        assert await session.get(MemoryDreamRunModel, ids["run_id"]) is not None
        assert await session.get(CanonicalPersonModel, ids["p_id"]) is None
        assert await session.get(CanonicalPersonModel, ids["q_id"]) is not None
        leftover_bindings = list(
            await session.scalars(
                select(IdentityBindingModel).where(IdentityBindingModel.person_id == ids["p_id"])
            )
        )
        assert leftover_bindings == []


@pytest.mark.asyncio
async def test_v1_forgetme_stays_single_qq(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="P")
    await people.observe(user_id="1002", nickname="Other")
    async with database.sessions() as session, session.begin():
        fact = MemoryFactRepository(database)
        created = await fact.create_fact(
            _person_fact("1002"),
            normalized_content="别人的茶",
            supersedes_id=None,
            session=session,
        )
        kept_id = int(created.id)
    assert await people.delete_person("1001") is True
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        assert await session.get(PersonModel, "1002") is not None
        assert await session.get(MemoryFactModel, kept_id) is not None


@pytest.mark.asyncio
async def test_forgetme_failpoint_rolls_back_memory_and_person(database: Database) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        created = await _seed_person(session, external_id="1001", display_name="P")
        person_id = created.person_id
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        row = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        fact_id = int(row.id)

    def boom(name: str) -> None:
        if name == "after_c21_forget_memory":
            raise RuntimeError("after_c21_forget_memory")

    set_identity_failpoint(boom)
    try:
        with pytest.raises(RuntimeError, match="after_c21_forget_memory"):
            await PeopleRepository(database).delete_person("1001")
    finally:
        set_identity_failpoint(None)
    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, person_id) is not None
        assert await session.get(MemoryFactModel, fact_id) is not None
        leftover = list(
            await session.scalars(
                select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
            )
        )
        assert leftover != []


def test_inventory_expresses_evidence_phase_not_completed_runtime() -> None:
    deferred = {key for key, _reason in DEFERRED_SHADOWS}
    baseline = {key for key, _reason in CUTOVER_BASELINE_PENDING}
    provenance = {key for key, _reason in LEGACY_PROVENANCE_RETAINED}
    reason = next(item for key, item in CUTOVER_BASELINE_PENDING if key == "memory_evidence")
    assert "memory_evidence" not in deferred
    assert "memory_evidence" in baseline
    assert "C7" in reason
    assert "C26" in reason
    assert "completed runtime" in reason
    assert "canonical Conversation" in reason
    assert "memory_evidence.source_speaker_user_id" in provenance


def test_v2_evidence_and_forget_ast_forbid_raw_qq_owners() -> None:
    forbidden = {
        "sender_user_id",
        "private_peer_user_id",
        "bot_user_id",
        "external_account_id",
    }
    allowed_provenance = {
        ("add_evidence", "source_speaker_user_id"),
        ("list_evidence", "source_speaker_user_id"),
    }
    files = (
        _SRC / "identity" / "c21_evidence.py",
        _SRC / "identity" / "memory_guard.py",
        _SRC / "memory" / "repository.py",
        _SRC / "persistence" / "people_repository.py",
        _SRC / "services" / "agent_tools.py",
    )
    hits: list[tuple[str, str, int, str]] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name not in {
                "list_person_facts_projected_to_group",
                "add_evidence",
                "list_evidence",
                "event_is_v2_live",
                "fact_conversation_aligns",
                "v2_evidence_event_chain_readable",
                "v2_evidence_row_readable",
                "refuse_unreadable_v2_evidence_event",
                "_forget_c21_person_memory",
                "_can_read_fact_complete_v2",
                "_can_read_own_person_fact",
                "_runtime_canonical_owners",
            }:
                continue
            for child in ast.walk(node):
                name = ""
                if isinstance(child, ast.Attribute):
                    name = child.attr
                elif isinstance(child, ast.Name):
                    name = child.id
                if name in forbidden and (node.name, name) not in allowed_provenance:
                    if node.name == "list_person_facts_projected_to_group":
                        continue
                    hits.append((path.name, node.name, child.lineno, name))
    leftover = [item for item in hits if item[1] != "list_person_facts_projected_to_group"]
    assert leftover == []


def _v2_branch_uses_raw_owner(source: str, function_name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            for statement in node.body:
                if isinstance(statement, ast.If) and _is_complete_v2(statement.test):
                    text = ast.dump(ast.Module(body=statement.body, type_ignores=[]))
                    return any(
                        token in text
                        for token in (
                            "sender_user_id",
                            "private_peer_user_id",
                            "source_speaker_user_id",
                        )
                    )
    return False


def _is_complete_v2(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Await) and isinstance(expr.value, ast.Call):
        func = expr.value.func
        return isinstance(func, ast.Name) and func.id == "identity_runtime_is_complete_v2"
    if isinstance(expr, ast.Call):
        return isinstance(expr.func, ast.Name) and expr.func.id == "identity_runtime_is_complete_v2"
    return False


def test_v2_projected_evidence_branch_does_not_use_raw_qq() -> None:
    source = (_SRC / "memory" / "repository.py").read_text(encoding="utf-8")
    assert _v2_branch_uses_raw_owner(source, "list_person_facts_projected_to_group") is False


@pytest.mark.parametrize("revision", ["0047", "head"])
def test_real_legacy_backfill_fills_owners_without_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    path = tmp_path / f"legacy-{revision}.db"
    _upgrade(path, monkeypatch, revision)
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_bindings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        ids = _seed_real_legacy_identity_blank(connection)
        event = connection.execute(
            "SELECT canonical_event_id, canonical_conversation_id FROM chat_events WHERE id = ?",
            (ids["event"],),
        ).fetchone()
        assert event[0] is None
        assert event[1] is None
        fact = connection.execute(
            "SELECT canonical_subject_person_id FROM memory_facts WHERE id = ?",
            (ids["fact"],),
        ).fetchone()
        assert fact[0] is None
        connection.commit()
        before = IdentityBackfillRepository(path).business_signature(connection)
    first = _backfill_service(path, _backfill_settings()).apply()
    assert first.status == "succeeded", first.error_category
    with _open(path) as connection:
        after_first = IdentityBackfillRepository(path).business_signature(connection)
        assert after_first != before
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        event = connection.execute(
            "SELECT canonical_event_id, canonical_conversation_id FROM chat_events WHERE id = ?",
            (ids["event"],),
        ).fetchone()
        assert event[0] is None
        assert event[1] is None
        fact = connection.execute(
            "SELECT canonical_subject_person_id FROM memory_facts WHERE id = ?",
            (ids["fact"],),
        ).fetchone()
        assert fact[0] is not None
    second = _backfill_service(path, _backfill_settings()).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == after_first


@pytest.mark.parametrize("revision", ["0047", "head"])
def test_legacy_backfill_owner_mismatch_is_content_free_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    path = tmp_path / f"legacy-conflict-{revision}.db"
    _upgrade(path, monkeypatch, revision)
    with _open(path) as connection:
        _legacy_people(connection, "1001")
        _legacy_people(connection, "1002")
        _legacy_people(connection, "8000", is_bot=1)
        event_id = _legacy_event(
            connection,
            message_id="other-peer",
            sender="1002",
            peer="1002",
            content="secret",
        )
        fact_id = _insert_fact(connection, user_id="1001", person_id=None)
        _legacy_evidence(connection, fact_id=fact_id, event_id=event_id)
        connection.commit()
        before = IdentityBackfillRepository(path).business_signature(connection)
    report = _backfill_service(path, _backfill_settings()).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0
    assert any(
        item.error_category in {"missing_owner", "ambiguous_owner"} for item in report.conflicts
    )
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == before
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        rendered = json.dumps([item.error_category for item in report.conflicts], ensure_ascii=True)
        assert "1001" not in rendered
        assert "1002" not in rendered
        assert "secret" not in rendered


def test_backfill_signature_covers_evidence_decision_inputs_not_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy-fingerprint.db"
    _upgrade(path, monkeypatch, "head")
    with _open(path) as connection:
        ids = _seed_real_legacy_identity_blank(connection)
        connection.commit()
        before_chunks = c21_signature_chunks(connection)
        before_material = c21_source_material(connection)
        connection.execute(
            "UPDATE memory_evidence SET excerpt = 'tampered-excerpt' WHERE fact_id = ?",
            (ids["fact"],),
        )
        connection.execute(
            "UPDATE chat_events SET content = 'tampered-body' WHERE id = ?",
            (ids["event"],),
        )
        assert c21_signature_chunks(connection) == before_chunks
        assert c21_source_material(connection) == before_material
        connection.execute(
            "UPDATE memory_evidence SET relation = 'third_party_statement' WHERE fact_id = ?",
            (ids["fact"],),
        )
        assert c21_signature_chunks(connection) != before_chunks
        connection.execute(
            "UPDATE memory_evidence SET relation = 'self_statement' WHERE fact_id = ?",
            (ids["fact"],),
        )
        connection.execute(
            "UPDATE chat_events SET group_id = '2001', scope_type = 'group', "
            "private_peer_user_id = NULL WHERE id = ?",
            (ids["event"],),
        )
        assert c21_source_material(connection) != before_material


def test_cutover_maps_unique_legacy_evidence_without_reenqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "evidence-cutover-ok.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_evidence(connection, fact_id=fact_id, event_id=int(ids["event"]))
        jobs_before = int(connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0])
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "evidence-cutover-ok-snap")
    planned = _cutover_service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _cutover_open(live) as connection:
        event = connection.execute(
            "SELECT canonical_event_id, canonical_conversation_id, author_kind, "
            "suppression_status FROM chat_events WHERE id = ?",
            (int(ids["event"]),),
        ).fetchone()
        assert event[0] and event[1] and event[2]
        assert event[3] in {None, "keeper"}
        jobs_after = int(connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0])
        assert jobs_after == jobs_before
        starts_after = int(
            connection.execute(
                "SELECT starts_after_event_id FROM canonical_conversations LIMIT 1"
            ).fetchone()[0]
        )
        assert int(ids["event"]) <= starts_after

    async def _read_and_refuse() -> None:
        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            facts = MemoryFactRepository(database)
            visible = await facts.list_evidence(fact_id)
            assert [row.event_id for row in visible] == [int(ids["event"])]
            listed = await facts.list_facts(_person_query("1001"))
            assert listed and listed[0].evidence_count == 1
            created = await MemoryJobRepository(database).enqueue(
                int(ids["event"]), "group:2001:user:1001"
            )
            assert created is False
            async with database.sessions() as session:
                event_row = await session.get(ChatEventModel, int(ids["event"]))
                assert event_row is not None
                assert await refuse_legacy_live_event(session, event_row) is True
        finally:
            await database.close()

    import asyncio

    asyncio.run(_read_and_refuse())


def test_cutover_blocks_unique_duplicate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "evidence-dup-only.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        keeper = _cutover_insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="shared-dup",
            author_person_id=ids["person"],
            content="shared-body",
        )
        duplicate = _cutover_insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="shared-dup",
            author_person_id=ids["person"],
            content="shared-body",
        )
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_evidence(connection, fact_id=fact_id, event_id=duplicate)
        connection.commit()
        assert keeper < duplicate
    settings = _prepare_snapshots(live, tmp_path / "evidence-dup-only-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_EVIDENCE_INCOMPLETE


@pytest.mark.parametrize("status", ["duplicate", "suppressed", "unknown"])
def test_cutover_allows_keeper_plus_preexisting_hidden_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    live = tmp_path / f"evidence-keeper-plus-{status}.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        hidden = _cutover_insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id=f"pre-hidden-{status}",
            author_person_id=ids["person"],
        )
        _set_event_suppression(connection, hidden, status)
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_evidence(connection, fact_id=fact_id, event_id=int(ids["event"]))
        _legacy_evidence(connection, fact_id=fact_id, event_id=hidden)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"evidence-keeper-plus-{status}-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


@pytest.mark.parametrize("status", ["duplicate", "suppressed", "unknown"])
def test_cutover_blocks_only_preexisting_hidden_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    live = tmp_path / f"evidence-only-hidden-{status}.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        hidden = _cutover_insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id=f"only-hidden-{status}",
            author_person_id=ids["person"],
        )
        _set_event_suppression(connection, hidden, status)
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_evidence(connection, fact_id=fact_id, event_id=hidden)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"evidence-only-hidden-{status}-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_EVIDENCE_INCOMPLETE


def test_cutover_allows_receipt_keeper_plus_preexisting_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "evidence-receipt-keeper-plus-hidden.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        hidden = _cutover_insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="receipt-hidden",
            author_person_id=ids["person"],
        )
        _set_event_suppression(connection, hidden, "duplicate")
        keeper_receipt = _legacy_receipt(
            connection, event_id=int(ids["event"]), person_id=ids["person"]
        )
        hidden_receipt = _legacy_receipt(connection, event_id=hidden, person_id=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_receipt_evidence(connection, fact_id=fact_id, tool_receipt_id=keeper_receipt)
        _legacy_receipt_evidence(connection, fact_id=fact_id, tool_receipt_id=hidden_receipt)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "evidence-receipt-keeper-plus-hidden-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


@pytest.mark.parametrize("status", ["duplicate", "suppressed", "unknown"])
def test_cutover_blocks_receipt_only_preexisting_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    live = tmp_path / f"evidence-receipt-only-hidden-{status}.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        hidden = _cutover_insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id=f"receipt-only-hidden-{status}",
            author_person_id=ids["person"],
        )
        _set_event_suppression(connection, hidden, status)
        receipt_id = _legacy_receipt(connection, event_id=hidden, person_id=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_receipt_evidence(connection, fact_id=fact_id, tool_receipt_id=receipt_id)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"evidence-receipt-only-hidden-{status}-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_EVIDENCE_INCOMPLETE


def test_cutover_plan_stales_when_evidence_decision_input_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "evidence-stale.db"
    ids = _ready(live, monkeypatch)
    with _cutover_open(live) as connection:
        _cutover_insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        fact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _legacy_evidence(connection, fact_id=fact_id, event_id=int(ids["event"]))
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "evidence-stale-snap")
    planned = _cutover_service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _cutover_open(live) as connection:
        connection.execute(
            "UPDATE memory_evidence SET relation = 'third_party_statement' WHERE fact_id = ?",
            (fact_id,),
        )
        connection.commit()
    report = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert report.status == "failed"
    assert report.error_category == "source_fingerprint"
    with _cutover_open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"


@pytest.mark.asyncio
async def test_watermark_does_not_hide_keeper_evidence(database: Database) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        space_id = await ensure_v2_space(session, "2001")
        conv_id = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=conv_id,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_id = person_p.person_id
        cid = conv_id
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        created = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        keeper = _event(
            message_id="wm-keep",
            sender_user_id="1001",
            author_person_id=p_id,
            conversation_id=cid,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(keeper)
        await session.flush()
        fact_id = int(created.id)
        event_id = int(keeper.id)
        conversation = await session.get(CanonicalConversationModel, cid)
        assert conversation is not None
        conversation.starts_after_event_id = event_id
        conversation.last_generation_change_event_id = event_id
        conversation.last_event_id = event_id
        conversation.covered_through_event_id = event_id
    assert await _add_evidence(facts, fact_id, event_id) is True
    visible = await facts.list_evidence(fact_id)
    assert [row.event_id for row in visible] == [event_id]
    created_job = await MemoryJobRepository(database).enqueue(event_id, "group:2001:user:1001")
    assert created_job is False
    async with database.sessions() as session:
        event_row = await session.get(ChatEventModel, event_id)
        assert event_row is not None
        assert await refuse_legacy_live_event(session, event_row) is True


@pytest.mark.asyncio
async def test_v2_forget_redacts_foreign_event_without_reenqueue(database: Database) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        group_conv = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=group_conv,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        q_id = person_q.person_id
        s_id = space_id
        gconv = group_conv
        del person_p
    async with database.sessions() as session, session.begin():
        q_event = _event(
            message_id="q-mentions-p",
            sender_user_id="1003",
            author_person_id=q_id,
            conversation_id=gconv,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        q_event.content = "刚才 1001 说了喝茶"
        session.add(q_event)
        await session.flush()
        q_event_id = int(q_event.id)
        session.add(
            MemoryJobModel(
                event_id=q_event_id,
                conversation_key=f"person:{q_id}",
                canonical_person_id=q_id,
                status="pending",
                attempts=0,
                next_attempt_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                processing_source="live",
            )
        )
        space_event = _event(
            message_id="space-job",
            sender_user_id="1003",
            author_person_id=q_id,
            conversation_id=gconv,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(space_event)
        await session.flush()
        session.add(
            MemoryJobModel(
                event_id=int(space_event.id),
                conversation_key=f"space:{s_id}",
                canonical_space_id=s_id,
                status="pending",
                attempts=0,
                next_attempt_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                processing_source="live",
            )
        )
        jobs_before = int(await session.scalar(select(func.count()).select_from(MemoryJobModel)))
        ids = {"q_event": q_event_id, "jobs_before": jobs_before, "q_id": q_id, "s_id": s_id}
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        leftover = await session.get(ChatEventModel, ids["q_event"])
        assert leftover is not None
        assert "1001" not in leftover.content
        assert _MARKER in leftover.content
        jobs_after = int(await session.scalar(select(func.count()).select_from(MemoryJobModel)))
        assert jobs_after == ids["jobs_before"]
        null_pending = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(
                    MemoryJobModel.status == "pending",
                    MemoryJobModel.canonical_person_id.is_(None),
                    MemoryJobModel.canonical_space_id.is_(None),
                )
            )
        )
        assert null_pending == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.canonical_person_id == ids["q_id"])
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.canonical_space_id == ids["s_id"])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_v1_forget_still_requeues_redacted_foreign_event(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="P")
    await people.observe(user_id="1003", nickname="Q")
    async with database.sessions() as session, session.begin():
        event = ChatEventModel(
            bot_user_id="8000",
            platform_message_id="v1-mention",
            scope_type="group",
            group_id="2001",
            sender_user_id="1003",
            direction="inbound",
            event_kind="message",
            content="刚才 1001 说了喝茶",
            visual_summary="",
            segments_json="[]",
            origin="user_message",
            occurred_at=_NOW,
            observed_at=_NOW,
        )
        session.add(event)
        await session.flush()
        event_id = int(event.id)
        jobs_before = int(await session.scalar(select(func.count()).select_from(MemoryJobModel)))
    assert await people.delete_person("1001") is True
    async with database.sessions() as session:
        leftover = await session.get(ChatEventModel, event_id)
        assert leftover is not None
        assert "1001" not in leftover.content
        assert _MARKER in leftover.content
        jobs_after = int(await session.scalar(select(func.count()).select_from(MemoryJobModel)))
        assert jobs_after == jobs_before + 1
        queued = (
            await session.scalars(select(MemoryJobModel).where(MemoryJobModel.event_id == event_id))
        ).first()
        assert queued is not None
        assert queued.status == "pending"


@pytest.mark.asyncio
async def test_v2_unknown_and_forgotten_profile_is_none(database: Database) -> None:
    await _flip_complete_v2(database)
    people = PeopleRepository(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        await _seed_person(session, external_id="1001", display_name="P")
    assert await people.get(user_id="9999") is None
    assert await people.get(user_id="8000") is None
    assert await people.get(user_id="1001") is not None
    assert await people.delete_person("1001") is True
    assert await people.get(user_id="1001") is None


@pytest.mark.asyncio
async def test_v2_unknown_actor_cannot_read_null_owner_fact(database: Database) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        session.add(
            MemoryFactModel(
                scope_type=MemoryScopeType.PERSON.value,
                subject_user_id="1001",
                kind=MemoryKind.FACT.value,
                memory_key="legacy-null",
                category="test",
                content="旧事实",
                normalized_content="旧事实",
                importance=3,
                confidence=0.8,
                source_type=MemorySourceType.EXPLICIT.value,
                authority=MemoryAuthority.SELF_REPORT.value,
                status="active",
                conflict_state="clear",
                created_at=_NOW,
                updated_at=_NOW,
                last_confirmed_at=_NOW,
                validation_version="memory-v2-quality-v1",
                review_state="verified",
            )
        )
        await session.flush()
        fact_id = int(
            (
                await session.scalars(
                    select(MemoryFactModel.id).order_by(MemoryFactModel.id.desc())
                )
            ).first()
        )
    tools = object.__new__(AgentToolService)
    tools._settings = make_settings(database.url, superusers_csv="9")
    tools._memories = type("Mem", (), {"repository": MemoryFactRepository(database)})()
    fact = await MemoryFactRepository(database).get_fact(fact_id)
    assert fact is not None
    runtime = ToolRuntime(
        inbound=inbound("读记忆", message_id="read-null", user_id="9999"),
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="9999",
        actor_is_superuser=True,
    )
    assert await tools._can_read_own_person_fact(fact, runtime) is False
    assert await tools._can_read_fact(fact, runtime) is False


def test_v2_superuser_cannot_read_incomplete_canonical_fact() -> None:
    tools = object.__new__(AgentToolService)
    tools._settings = make_settings("sqlite+aiosqlite:///:memory:", superusers_csv="9")
    fact = type(
        "Fact",
        (),
        {
            "scope_type": MemoryScopeType.PERSON,
            "visibility_type": None,
            "canonical_subject_person_id": None,
            "canonical_subject_space_id": None,
            "canonical_visibility_person_id": None,
            "canonical_visibility_space_id": None,
        },
    )()
    runtime = ToolRuntime(
        inbound=inbound("读记忆", message_id="read-super", user_id="9"),
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="9",
        actor_is_superuser=True,
    )
    assert tools._can_read_fact_complete_v2(fact, runtime, None, None) is False
    assert tools._can_read_fact_complete_v2(fact, runtime, "person-1", None) is False


@pytest.mark.asyncio
async def test_v2_evidence_count_counts_only_readable(
    database: Database,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        space_id = await ensure_v2_space(session, "2001")
        conv_id = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=conv_id,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_id = person_p.person_id
        cid = conv_id
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        created = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        rows = []
        for name, status, canonical in (
            ("keep", "keeper", str(uuid4())),
            ("dup", "duplicate", str(uuid4())),
            ("legacy", "keeper", None),
        ):
            event = _event(
                message_id=name,
                sender_user_id="1001",
                author_person_id=p_id,
                conversation_id=cid,
                canonical_event_id=canonical,
                suppression_status=status,
                group_id="2001",
            )
            session.add(event)
            rows.append((name, event))
        await session.flush()
        fact_id = int(created.id)
        event_ids = {name: int(event.id) for name, event in rows}
    assert await _add_evidence(facts, fact_id, event_ids["keep"]) is True
    async with database.sessions() as session, session.begin():
        for name in ("dup", "legacy"):
            session.add(
                MemoryEvidenceModel(
                    fact_id=fact_id,
                    event_id=event_ids[name],
                    source_speaker_user_id="1001",
                    relation=MemoryEvidenceRelation.SELF_STATEMENT.value,
                    confidence=1.0,
                    authority=MemoryAuthority.SELF_REPORT.value,
                    excerpt="hidden",
                    created_at=_NOW,
                )
            )
    listed = await facts.list_facts(_person_query("1001"))
    loaded = await facts.get_fact(fact_id)
    visible = await facts.list_evidence(fact_id)
    assert listed and listed[0].evidence_count == 1
    assert loaded is not None
    assert loaded.evidence_count == 1
    assert [row.event_id for row in visible] == [event_ids["keep"]]
