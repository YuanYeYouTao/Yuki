"""Retirement upgrades retain selected history and original paid Work facts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

from alembic import command
from alembic.config import Config

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.conversation.scope_repository import ConversationScopeRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.schema_guard import require_canonical_schema
from qq_ai_bot.services.context_assembler import ContextAssembler


def _seed_0118(path, monkeypatch):
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "0118")
    now = "2026-10-14 00:00:00"
    identities = []
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO presences VALUES(?,'qq','test',1,1,1,?,?)",
            (str(uuid4()), now, now),
        )
        for index in range(3):
            person, conversation, alias, epoch = (str(uuid4()) for _ in range(4))
            identities.append(conversation)
            connection.execute("INSERT INTO persons VALUES(?,1,1,?,?)", (person, now, now))
            connection.execute(
                "INSERT INTO identity_bindings(id,person_id,platform,external_account_id,"
                "display_name,status,revision,first_seen_at,last_seen_at,created_at,updated_at) "
                "VALUES(?,?,'qq',?,'','active',1,?,?,?,?)",
                (str(uuid4()), person, str(index), now, now, now, now),
            )
            connection.execute(
                "INSERT INTO canonical_conversations("
                "id,kind,person_id,primary_alias_id,generation,starts_after_event_id,last_event_id,"
                "last_generation_change_event_id,covered_through_event_id,uncovered_event_count,"
                "uncovered_character_count,revision,created_at,updated_at) "
                "VALUES(?,'private',?,?,1,0,10,0,5,5,100,1,?,?)",
                (conversation, person, alias, now, now),
            )
            connection.execute(
                "INSERT INTO conversation_legacy_aliases VALUES(?,?,?,1,?,?)",
                (alias, conversation, ConversationScope.private("test", str(index)).key, now, now),
            )
            connection.execute(
                "INSERT INTO canonical_conversation_rollups VALUES(?,1,5,?,'model',?,1,?,?)",
                (conversation, f"legacy checkpoint {index}", "0" * 64, now, now),
            )
            selected = "already selected" if index == 1 else None
            connection.execute(
                "INSERT INTO prompt_projections("
                "view_key,conversation_id,generation,source_revision,starts_after_event_id,"
                "epoch_id,context_key,revision,rebuild_reason,payload_json,byte_size,updated_at,"
                "selected_summary_text,selected_summary_coverage,selected_summary_kind,"
                "selected_summary_renderer) VALUES(?,?,1,0,0,?,?,1,'bootstrap','[]',2,?,?,?,?,?)",
                (
                    conversation,
                    conversation,
                    epoch,
                    "test",
                    now,
                    selected,
                    3 if selected else 0,
                    "model" if selected else None,
                    1 if selected else None,
                ),
            )
            if index == 2:
                connection.execute(
                    "UPDATE canonical_conversation_rollups SET generation=2 "
                    "WHERE conversation_id=?",
                    (conversation,),
                )
            work_id = str(uuid4())
            connection.execute(
                "INSERT INTO runtime_work("
                "id,conversation_id,generation,source_key,source_json,goal,state,checkpoint_json,"
                "model_requests,tool_calls,active_seconds,sent_messages,created,updated) "
                "VALUES(?,?,1,?,'{}','original goal','waiting_external',?,7,12,42,3,1,1)",
                (
                    work_id,
                    conversation,
                    f"source:{work_id}",
                    json.dumps({"context_rollup": {"coverage": 5}, "paid_receipt": "keep"}),
                ),
            )
        for event_id in (2, 4, 6, 8):
            connection.execute(
                "INSERT INTO chat_events("
                "id,bot_user_id,platform_message_id,scope_type,private_peer_user_id,sender_user_id,"
                "direction,content,visual_summary,segments_json,origin,occurred_at,observed_at,"
                "canonical_event_id,canonical_conversation_id,author_kind,suppression_status) "
                "VALUES(?,'test',?,'private','0','test','outbound',?,'','[]','user_message',"
                "?,?,?,?,'system','keeper')",
                (
                    event_id,
                    str(event_id),
                    f"original event {event_id}",
                    now,
                    now,
                    str(uuid4()),
                    identities[0],
                ),
            )
    return url, identities


def test_0119_preserves_checkpoints_work_budget_and_paid_material(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.sqlite3"
    url, identities = _seed_0118(path, monkeypatch)
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(path) as connection:
        assert not list(connection.execute("PRAGMA foreign_key_check"))
        migrated = connection.execute(
            "SELECT selected_summary_text,selected_summary_coverage,selected_summary_kind,"
            "selected_summary_renderer,invalidated_reason FROM prompt_projections "
            "WHERE conversation_id=?",
            (identities[0],),
        ).fetchone()
        assert migrated == ("legacy checkpoint 0", 5, "compact", 2, "compact")
        existing = connection.execute(
            "SELECT selected_summary_text,selected_summary_coverage,selected_summary_kind,"
            "selected_summary_renderer,invalidated_reason FROM prompt_projections "
            "WHERE conversation_id=?",
            (identities[1],),
        ).fetchone()
        assert existing == ("already selected", 3, "compact", 2, "compact")
        stale = connection.execute(
            "SELECT selected_summary_text FROM prompt_projections WHERE conversation_id=?",
            (identities[2],),
        ).fetchone()
        assert stale == (None,)
        for conversation in identities:
            row = connection.execute(
                "SELECT state,reason,checkpoint_json,model_requests,tool_calls,active_seconds,"
                "sent_messages,goal FROM runtime_work WHERE conversation_id=?",
                (conversation,),
            ).fetchone()
            assert row == (
                "queued",
                "context_prepared",
                '{"paid_receipt":"keep"}',
                7,
                12,
                42,
                3,
                "original goal",
            )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(canonical_conversations)")
        }
        assert not columns & {
            "covered_through_event_id",
            "uncovered_event_count",
            "uncovered_character_count",
        }
        assert not list(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%canonical%rollup%'"
            )
        )
    asyncio.run(require_canonical_schema(url))


def test_scope_snapshot_reads_selected_checkpoint_and_all_raw_suffix(tmp_path, monkeypatch):
    path = tmp_path / "scope.sqlite3"
    url, identities = _seed_0118(path, monkeypatch)
    command.upgrade(Config("alembic.ini"), "head")

    async def verify():
        database = Database(url)
        try:
            repository = ConversationScopeRepository(database)
            snapshot = await repository.load_prompt_snapshot(ConversationScope.private("test", "0"))
            assert snapshot.scope.id == identities[0]
            assert snapshot.checkpoint_text == "legacy checkpoint 0"
            assert snapshot.effective_coverage == 5
            assert tuple(row.id for row in snapshot.raw_events) == (6, 8)
            assert tuple(row.content for row in snapshot.raw_events) == (
                "original event 6",
                "original event 8",
            )
            prefix = await repository.load_prompt_snapshot(
                ConversationScope.private("test", "0"), before_event_id=7
            )
            assert tuple(row.id for row in prefix.raw_events) == (6,)
            settings = Settings(_env_file=None)
            runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
            runtime = replace(runtime, context=replace(runtime.context, window_tokens=1))
            assembled = await ContextAssembler(
                settings=settings,
                ledger=EventLedgerRepository(database),
                conversation_scopes=repository,
            ).assemble(
                inbound=None,
                identity=ConversationScope.private("test", "0"),
                profile=None,
                turn=ConversationTurnSnapshot(
                    identities[0], ConversationScope.private("test", "0").key, 1, 6, 1
                ),
                content="current activation",
                runtime=runtime,
            )
            assert assembled.checkpoint_text == "legacy checkpoint 0"
            assert assembled.history_messages
            # Main now decides actual request capacity. Assembly neither truncates
            # this legal suffix nor waits for an independently bought summary.
            assert all(
                str(event_id)
                in "".join(message.content or "" for message in assembled.history_messages)
                for event_id in (6, 8)
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE chat_events SET audio_transcript='new source' WHERE id=6"
                )
            changed = await repository.load_prompt_snapshot(ConversationScope.private("test", "0"))
            assert changed.checkpoint_text == ""
            assert tuple(row.id for row in changed.raw_events) == (2, 4, 6, 8)
        finally:
            await database.close()

    asyncio.run(verify())
