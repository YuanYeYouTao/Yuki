"""A one-time upgrade retires detached claims while preserving original receipts."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel


def test_upgrade_only_retires_old_claimed_continuations(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'continuations.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0115")

    async def snapshot(database):
        async with database.sessions() as reader:
            return {
                name: [
                    dict(row)
                    for row in (await reader.execute(text(f"SELECT * FROM {name}"))).mappings()
                ]
                for name in ("sandbox_task_runs", "sandbox_task_continuations")
            }

    async def seed():
        database = Database(url)
        try:
            env = await social_env(database, tmp_path)
            now = datetime.now(UTC)
            async with database.immediate_session() as writer:
                for state in ("claimed", "ready", "observed", "finished", "uncertain", "blocked"):
                    writer.add(
                        SandboxTaskRunModel(
                            request_id=state,
                            source_conversation_id=env.context.conversation_id,
                            source_json=json.dumps(
                                {"conversation_id": env.context.conversation_id}
                            ),
                            payload_hash="a" * 64,
                            run_id=str(uuid4()),
                            status="completed",
                            completion_json='{"original":"paid result"}',
                            created_at=now,
                            updated_at=now,
                        )
                    )
                await writer.flush()
                for state in ("claimed", "ready", "observed", "finished", "uncertain", "blocked"):
                    writer.add(
                        SandboxTaskContinuationModel(
                            request_id=state,
                            state=state,
                            claim_token=str(uuid4()),
                            attempts=2,
                            reason="original_reason",
                            outcome_json='{"original":"receipt"}',
                            updated_at=now,
                        )
                    )
            return await snapshot(database)
        finally:
            await database.close()

    original = asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        database = Database(url)
        try:
            saved = await snapshot(database)
            assert saved["sandbox_task_runs"] == original["sandbox_task_runs"]
            for before, after in zip(
                original["sandbox_task_continuations"],
                saved["sandbox_task_continuations"],
                strict=True,
            ):
                if before["state"] == "claimed":
                    assert after["state"] == "blocked"
                    assert after["reason"] == "legacy_continuation_retired"
                    assert {
                        key: value
                        for key, value in after.items()
                        if key not in {"state", "reason", "updated_at"}
                    } == {
                        key: value
                        for key, value in before.items()
                        if key not in {"state", "reason", "updated_at"}
                    }
                else:
                    assert after == before
            async with database.sessions() as reader:
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
                assert not list(
                    await reader.scalars(
                        select(SandboxTaskContinuationModel).where(
                            SandboxTaskContinuationModel.state == "claimed"
                        )
                    )
                )
        finally:
            await database.close()

    asyncio.run(verify())
    command.upgrade(config, "head")
    asyncio.run(verify())
