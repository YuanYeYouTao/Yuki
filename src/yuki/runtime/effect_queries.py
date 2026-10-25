"""Read original Work effect and delivery facts without dispatching any action."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from sqlalchemy import select, text

from yuki.capabilities.results import ToolExecutionResult
from yuki.persistence.database import Database
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_schema_v1 import work


class RuntimeEffectQueries:
    """Typed observations for callers that own their own lifecycle.

    Domain receipts remain the delivery evidence. This port reconciles only their
    original Work/call references, without repairing or replaying an effect.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def inspect_memory_mutation(
        self, *, reference: str, operation_key: str
    ) -> ToolExecutionResult | None:
        from yuki.memory.mutation.models import MemoryMutationOutcome, MemoryMutationResult
        from yuki.memory.mutation.repository import MemoryMutationReceiptRepository

        # The reference is bound to the original effect in the mutation transaction.
        # Preserve the persisted fingerprint bytes, including the operation_id name.
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation_id": operation_key},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = await MemoryMutationReceiptRepository(self._database).get(
            reference.removeprefix("memory:")
        )
        if (
            receipt is None
            or receipt.idempotency_key != fingerprint
            or receipt.outcome is MemoryMutationOutcome.PROCESSING
        ):
            return None
        result = MemoryMutationResult.from_receipt(receipt, deduplicated=False)
        return ToolExecutionResult(
            ok=result.ok,
            data=result.tool_payload(),
            error_code=None if result.ok else result.reason_code,
            mutation_committed=result.ok,
        )

    async def inspect_native_operation(
        self, *, work_id: str, operation_key: str, action: str
    ) -> ToolExecutionResult | None:
        from yuki.external_agents.repository import AgentRepository
        from yuki.runtime.effect_outcomes import execution_evidence

        async with self._database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            source_json = await reader.scalar(
                select(work.c.source_json).where(work.c.id == work_id)
            )
            if source_json is None:
                return None
            actor = json.loads(source_json).get("actor_person_id")
            if not isinstance(actor, str):
                return None
            original = await AgentRepository(self._database).work_operation(
                hashlib.sha256(operation_key.encode()).hexdigest(),
                work_id=work_id,
                action=action,
                actor_person_id=actor,
                session=reader,
            )
        if original is None:
            return None
        uncertain = bool(original.get("uncertain"))
        error = original.get("error")
        outcome = ToolExecutionResult(
            ok=not error and not uncertain,
            data=original,
            error_code=str(error) if error else None,
            uncertain=uncertain,
            mutation_committed=None if uncertain or original.get("pending") else not bool(error),
        )
        evidence = execution_evidence(
            outcome,
            tool="external_agent",
            side_effecting=True,
            arguments=json.dumps({"action": action}),
        )
        # A later unknown native observation cannot undo confirmed control admission.
        return (
            replace(outcome, ok=True, uncertain=False, mutation_committed=True)
            if evidence["status"] == "admitted"
            else outcome
        )

    async def inspect_local_operation(
        self, *, reference: str, work_id: str, operation_key: str
    ) -> ToolExecutionResult | None:
        """Read a retired executor's original receipt, without a live executor."""
        from yuki.executor.db_models import ExecutorOperationModel
        from yuki.time.formatting import utc_iso

        async with self._database.read_session() as reader:
            row = await reader.scalar(
                select(ExecutorOperationModel).where(
                    ExecutorOperationModel.op_id == reference.removeprefix("local_device:"),
                    ExecutorOperationModel.tool_call_key == operation_key,
                    ExecutorOperationModel.work_id == work_id,
                )
            )
            if row is None or reference != "local_device:" + row.op_id:
                return None
            original = {
                "tool": "local_device",
                "op_id": row.op_id,
                "operation_id": row.op_id,
                "capability": row.capability,
                "params": json.loads(row.params_json or "{}"),
                "state": row.state,
                "status": row.state,
                "level": row.level,
                "result": json.loads(row.result_json or "{}"),
                "observation": json.loads(row.observation_json) if row.observation_json else None,
                "created_at": utc_iso(row.created_at),
                "dispatched_at": utc_iso(row.dispatched_at),
                "acknowledged_at": utc_iso(row.acknowledged_at),
                "settled_at": utc_iso(row.settled_at),
                "pending": row.state not in {"succeeded", "failed", "unknown"},
                "ok": row.state != "failed",
                "uncertain": row.state == "unknown",
            }
        uncertain = bool(original["uncertain"])
        return ToolExecutionResult(
            ok=original["state"] != "failed" and not uncertain,
            data=original,
            error_code="local_device_failed" if original["state"] == "failed" else None,
            uncertain=uncertain,
            mutation_committed=None
            if uncertain or original["pending"]
            else original["state"] == "succeeded",
        )

    async def inspect_social_operation(
        self, *, reference: str, work_id: str, operation_key: str
    ) -> dict[str, Any] | None:
        """Read the exact original parent and its prospective child vector.

        This is observation only: no dispatch, receipt rewrite or lease renewal.
        Missing planned children remain unconfirmed, including a missing caption.
        """
        if not reference.startswith(("social:", "social-emoji:")):
            return None
        from yuki.social.repository import SocialOperationRepository

        operation_id = reference.removeprefix("social:")
        async with self._database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            conversation = await reader.scalar(
                select(work.c.conversation_id).where(work.c.id == work_id)
            )
            if conversation is None:
                return None
            if reference.startswith("social-emoji:"):
                from yuki.social.emoji_intents import EmojiIntentRepository

                return await EmojiIntentRepository.effect_facts(
                    reader,
                    reference.removeprefix("social-emoji:"),
                    conversation_id=conversation,
                    work_id=work_id,
                    call_id=operation_key,
                )
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
