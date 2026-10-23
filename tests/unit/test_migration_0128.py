"""Real Alembic upgrade keeps notification provenance independent of temporary bytes."""

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.database import Database
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore


def _insert(connection, table, values):
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


def test_0128_preserves_old_receipt_and_budget_before_real_artifact_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "original-receipt.db"
    url, _ = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0126")
    media_path = tmp_path / "original.png"
    media_path.write_bytes(b"original publication bytes")
    now = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert(
            connection,
            "plugin_installations",
            dict(
                plugin_id="receipt.fixture",
                name="Receipt fixture",
                version="1.0",
                plugin_api="4.0",
                yuki_requires=">=3.9",
                manifest_hash="fixture",
                entrypoint="fixture:plugin",
                status="running",
                enabled=1,
                approved_permissions_json="[]",
                requested_permissions_json="[]",
                failure_count=0,
                discovered_at=now,
                approved_at=now,
                started_at=now,
                updated_at=now,
            ),
        )
        _insert(
            connection,
            "plugin_media_artifacts",
            dict(
                handle_id="original-handle",
                plugin_id="receipt.fixture",
                content_type="image/png",
                filename="original.png",
                byte_size=26,
                sha256="a" * 64,
                storage_path=str(media_path),
                created_at=now,
                expires_at=now,
            ),
        )
        _insert(
            connection,
            "plugin_notification_outbox",
            dict(
                id=63,
                notification_id="original-notification",
                part_key="media:0:" + "a" * 64,
                source_event_id=2,
                plugin_id="receipt.fixture",
                target_type="private",
                target_id="original-person",
                bot_user_id="original-bot",
                part_type="media",
                text="",
                media_handle_id="original-handle",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=now,
                platform_message_id="original-send",
                created_at=now,
                updated_at=now,
                sent_at=now,
            ),
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("plugin_notification_outbox", "chat_events", "runtime_work")
        }
        old_keys = connection.execute(
            "PRAGMA foreign_key_list(plugin_notification_outbox)"
        ).fetchall()
        assert any(row[3] == "media_handle_id" and row[6] == "SET NULL" for row in old_keys)
    command.upgrade(config, "0128")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0128",)
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    async def cleanup():
        database = Database(url)
        try:
            return await PluginMediaArtifactStore(database, tmp_path).cleanup()
        finally:
            await database.close()

    assert asyncio.run(cleanup()) == 1
    assert not media_path.exists()
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT * FROM plugin_media_artifacts").fetchall()
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
