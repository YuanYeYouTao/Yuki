"""A partial index makes the poll skip completed receipts without rewriting them."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tests.support.legacy_conversation_seed import frozen_social_env

from yuki.persistence.database import Database
from yuki.sandbox.db_models import SandboxTaskRunModel


def test_upgrade_indexes_original_waiting_runs_and_preserves_all_receipts(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'waiting.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0117")

    async def read(*, seed=False):
        database = Database(url)
        try:
            if seed:
                env = await frozen_social_env(database)
                now = datetime.now(UTC)
                async with database.immediate_session() as writer:
                    for index in range(2):
                        writer.add(
                            SandboxTaskRunModel(
                                request_id=f"original:{index}",
                                source_conversation_id=env.context.conversation_id,
                                source_json="{}",
                                payload_hash="original-hash",
                                run_id=str(uuid4()),
                                status="waiting" if index == 1 else "completed",
                                completion_json=None
                                if index == 1
                                else '{"original":"paid receipt"}',
                                created_at=now,
                                updated_at=now,
                            )
                        )
            async with database.read_session() as reader:
                rows = [
                    dict(row)
                    for row in (
                        await reader.execute(
                            text("SELECT * FROM sandbox_task_runs ORDER BY request_id")
                        )
                    ).mappings()
                ]
                return rows
        finally:
            await database.close()

    original = asyncio.run(read(seed=True))
    command.upgrade(config, "head")
    current = asyncio.run(read())
    assert current == original
    command.upgrade(config, "head")
    assert asyncio.run(read()) == current
