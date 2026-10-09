"""Read original Work effect and delivery facts without dispatching any action."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_schema_v1 import work


class RuntimeEffectQueries:
    """Typed observations for callers that own their own lifecycle.

    Domain receipts remain the delivery evidence. This port reconciles only their
    original Work/call references, without repairing or replaying an effect.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def inspect_social_operation(
        self, *, reference: str, work_id: str, operation_key: str
    ) -> dict[str, Any] | None:
        """Read the exact original parent and its prospective child vector.

        This is observation only: no dispatch, receipt rewrite or lease renewal.
        Missing planned children remain unconfirmed, including a missing caption.
        """
        if not reference.startswith("social:"):
            return None
        from qq_ai_bot.social.repository import SocialOperationRepository

        operation_id = reference.removeprefix("social:")
        async with self._database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            conversation = await reader.scalar(
                select(work.c.conversation_id).where(work.c.id == work_id)
            )
            if conversation is None:
                return None
            # The Work-owned reservation is the file+caption plan; Social reads
            # its own rows in this same snapshot.
            intent = (
                (
                    await reader.execute(
                        select(deliveries.c.kind, deliveries.c.message_count).where(
                            deliveries.c.id == operation_id, deliveries.c.work_id == work_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return await SocialOperationRepository.delivery_facts(
                reader,
                operation_id,
                conversation_id=conversation,
                tool_call_id=operation_key,
                planned_file_parts=intent["message_count"]
                if intent is not None and intent["kind"] == "artifact"
                else None,
            )
