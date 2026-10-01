"""Resolve the canonical source of a completed sandbox request."""

import json

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
from qq_ai_bot.services.execution_sources import MessageTaskSource, recover_source


async def recover_message_source(database: Database, request_id: str) -> MessageTaskSource:
    """Recheck on every use; the returned value is context, not a permission token.

    Scheduled automation has a different source/delegation contract and must never
    be reconstructed as a real user message through this entry point.
    """
    async with database.sessions() as session:
        task = await session.get(SandboxTaskRunModel, request_id)
        if task is None or task.status != "completed" or not task.completion_json:
            raise ValueError("task_not_completed")
        if json.loads(task.completion_json).get("status") == "cancelled":
            raise ValueError("task_cancelled")
        source = json.loads(task.source_json)
    return await recover_source(
        database, task.source_conversation_id, source, request_id=request_id
    )
