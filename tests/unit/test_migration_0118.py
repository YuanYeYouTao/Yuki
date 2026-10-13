"""A partial index makes the poll skip completed receipts without rewriting them."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

QUERY = """EXPLAIN QUERY PLAN
SELECT s.run_id FROM sandbox_task_runs s JOIN runtime_work w
ON w.id=json_extract(s.source_json, '$.work_id')
WHERE w.state IN ('failed','cancelled') AND s.status='waiting' AND s.run_id IS NOT NULL
"""


def test_upgrade_indexes_original_waiting_runs_and_preserves_all_receipts(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'waiting.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0117")

    async def read(*, seed=False):
        database = Database(url)
        try:
            if seed:
                env = await social_env(database, tmp_path)
                now = datetime.now(UTC)
                async with database.immediate_session() as writer:
                    for index in range(102):
                        writer.add(
                            SandboxTaskRunModel(
                                request_id=f"original:{index}",
                                source_conversation_id=env.context.conversation_id,
                                source_json="{}",
                                payload_hash="original-hash",
                                run_id=str(uuid4()),
                                status="waiting" if index >= 100 else "completed",
                                completion_json=None
                                if index >= 100
                                else '{"original":"paid receipt"}',
                                created_at=now,
                                updated_at=now,
                            )
                        )
            async with database.sessions() as reader:
                plan = [row.detail for row in await reader.execute(text(QUERY))]
                rows = [
                    dict(row)
                    for row in (
                        await reader.execute(
                            text("SELECT * FROM sandbox_task_runs ORDER BY request_id")
                        )
                    ).mappings()
                ]
                return rows, plan
        finally:
            await database.close()

    original, before = asyncio.run(read(seed=True))
    assert any("SCAN s" in line for line in before)
    command.upgrade(config, "head")
    current, after = asyncio.run(read())
    assert current == original
    assert any("ix_sandbox_task_runs_waiting_run" in line for line in after)
    command.upgrade(config, "head")
    assert asyncio.run(read()) == (current, after)
