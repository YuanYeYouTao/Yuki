"""Upgrade preserves current bytes, original provenance and paid execution facts."""

import asyncio
import json
import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.schema_guard import require_canonical_schema


def test_chunk_upgrade_copies_current_representation_and_keeps_receipts(tmp_path, monkeypatch):
    path = tmp_path / "chunks.db"
    url, identities = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0132")
    view = identities[0]
    with sqlite3.connect(path) as connection:
        source = connection.execute(
            "SELECT id FROM chat_events WHERE canonical_conversation_id=? ORDER BY id LIMIT 1",
            (view,),
        ).fetchone()[0]
        current = json.dumps(
            [
                {
                    "kind": "model_input",
                    "event_ids": [source],
                    "message": {"role": "user", "content": "本代实际采用的原始文本\n保留空格  "},
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE prompt_projections SET payload_json=?,byte_size=?,invalidated_reason=NULL,"
            "generation=(SELECT generation FROM canonical_conversations WHERE id=?),"
            "source_revision=(SELECT prompt_source_revision FROM canonical_conversations "
            "WHERE id=?) "
            "WHERE view_key=?",
            (current, len(current.encode()), view, view, view),
        )
        connection.execute(
            "INSERT INTO model_context_selections(view_key,conversation_id,generation,source_key,"
            "event_ids_json,payload_json,created_at) VALUES(?,?,1,'old-admission',?,?,?)",
            (view, view, json.dumps([source]), '{"original":"first admission"}', "2026-10-18"),
        )
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if name not in {"prompt_projections", "model_context_selections", "alembic_version"}
        ]
        before = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
        selections = connection.execute(
            "SELECT * FROM model_context_selections ORDER BY id"
        ).fetchall()
        head = connection.execute(
            "SELECT epoch_id,revision,selected_summary_text,selected_summary_coverage FROM "
            "prompt_projections WHERE view_key=?",
            (view,),
        ).fetchone()
    command.upgrade(config, "0133")
    with sqlite3.connect(path) as connection:
        assert "payload_json" not in {
            row[1] for row in connection.execute("PRAGMA table_info(prompt_projections)")
        }
        assert connection.execute(
            "SELECT payload_json FROM prompt_projection_chunks WHERE view_key=?", (view,)
        ).fetchall() == [(current,)]
        assert (
            connection.execute(
                "SELECT epoch_id,revision,selected_summary_text,selected_summary_coverage FROM "
                "prompt_projections WHERE view_key=?",
                (view,),
            ).fetchone()
            == head
        )
        assert connection.execute(
            "SELECT tail_ordinal,item_count FROM prompt_projections WHERE view_key=?",
            (view,),
        ).fetchone() == (1, 1)
        for name, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{name}"').fetchall() == rows
        assert [
            row[:-2]
            for row in connection.execute("SELECT * FROM model_context_selections ORDER BY id")
        ] == selections
        for ids, low, high in connection.execute(
            "SELECT event_ids_json,min_event_id,max_event_id FROM model_context_selections"
        ):
            assert (low, high) == (min(json.loads(ids), default=0), max(json.loads(ids), default=0))
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    asyncio.run(require_canonical_schema(url))


def test_chunk_downgrade_rejoins_current_items_without_replaying_effects(tmp_path, monkeypatch):
    path = tmp_path / "roundtrip.db"
    _url, _identities = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0132")
    with sqlite3.connect(path) as connection:
        originals = connection.execute(
            "SELECT view_key, payload_json, invalidated_reason, selected_summary_text "
            "FROM prompt_projections ORDER BY view_key"
        ).fetchall()
        receipts = connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall()
    command.upgrade(config, "0133")
    command.downgrade(config, "0132")
    with sqlite3.connect(path) as connection:
        restored = connection.execute(
            "SELECT view_key, payload_json, invalidated_reason, selected_summary_text "
            "FROM prompt_projections ORDER BY view_key"
        ).fetchall()
        assert restored == originals
        assert connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall() == receipts
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
