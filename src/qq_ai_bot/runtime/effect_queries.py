"""Read original Work effect and delivery facts without dispatching any action."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Literal

from sqlalchemy import func, select, text

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.source_keys import social_call_key, social_source_key

DeliveryState = Literal["none", "succeeded", "failed", "uncertain"]


@dataclass(frozen=True, slots=True)
class AgentDeliveryOutcome:
    state: DeliveryState
    reason: str


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _receipts(body: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(body.get("parts"), list):
        return [_object(part) for part in body["parts"]]
    if isinstance(body.get("file"), dict):
        return [body["file"], _object(body.get("caption"))]
    return [body]


def _confirmed_sequence(parent: SocialOperationModel, rows: list[SocialOperationModel]) -> bool:
    """Reconcile only an explicitly persisted prospective plan and every child."""
    count = parent.planned_parts
    if (
        parent.action != "send_message_sequence"
        or parent.status != "prepared"
        or count is None
        or count <= 1
    ):
        return False
    prefix = f"seq:{hashlib.sha256(parent.tool_call_id.encode()).hexdigest()[:24]}:"
    children = [row for row in rows if row.tool_call_id.startswith(prefix)]
    return len(children) == count and all(
        (child := next((row for row in children if row.tool_call_id == f"{prefix}{index}"), None))
        is not None
        and child.action == "send_message"
        and child.status == "succeeded"
        and child.source_turn_id == parent.source_turn_id
        and child.source_conversation_id == parent.source_conversation_id
        and child.target_kind == parent.target_kind
        and child.target_id == parent.target_id
        for index in range(count)
    )


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
        async with self._database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            conversation = await reader.scalar(
                select(work.c.conversation_id).where(work.c.id == work_id)
            )
            parent = await reader.get(SocialOperationModel, reference.removeprefix("social:"))
            if parent is None or (
                parent.source_conversation_id != conversation
                or parent.tool_call_id != social_call_key(operation_key)
            ):
                return None

            def receipt(row: SocialOperationModel | None) -> dict[str, Any]:
                if row is None:
                    return {"status": "not_sent"}
                return {
                    "operation_id": row.id,
                    "status": row.status,
                    "target": {"kind": row.target_kind, "id": row.target_id},
                    "event_id": row.event_id,
                    "error_category": row.error_category,
                }

            result = receipt(parent)
            parts = [result.copy()]
            complete_plan = True
            if parent.action == "send_message_sequence":
                count = parent.planned_parts
                if count is None or count <= 1:
                    result["status"] = "uncertain"
                    return result
                prefix = f"seq:{hashlib.sha256(parent.tool_call_id.encode()).hexdigest()[:24]}:"
                children = {
                    row.tool_call_id: row
                    for row in await reader.scalars(
                        select(SocialOperationModel).where(
                            SocialOperationModel.source_turn_id == parent.source_turn_id,
                            SocialOperationModel.source_conversation_id == conversation,
                            SocialOperationModel.target_kind == parent.target_kind,
                            SocialOperationModel.target_id == parent.target_id,
                            SocialOperationModel.action == "send_message",
                            SocialOperationModel.tool_call_id.startswith(prefix),
                        )
                    )
                }
                parts = [receipt(children.get(f"{prefix}{index}")) for index in range(count)]
                result.update(
                    planned_messages=count,
                    sent_messages=sum(part["status"] == "succeeded" for part in parts),
                    parts=parts,
                )
            else:
                intent = (
                    (
                        await reader.execute(
                            select(deliveries).where(
                                deliveries.c.id == parent.id, deliveries.c.work_id == work_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if intent is not None and intent["kind"] == "artifact":
                    result["file"] = parts[0]
                    if intent["message_count"] > 1:
                        caption = await reader.scalar(
                            select(SocialOperationModel).where(
                                SocialOperationModel.source_turn_id
                                == f"social-caption:{parent.id}",
                                SocialOperationModel.tool_call_id == "caption",
                                SocialOperationModel.source_conversation_id == conversation,
                                SocialOperationModel.target_kind == parent.target_kind,
                                SocialOperationModel.target_id == parent.target_id,
                                SocialOperationModel.action == "send_file_caption",
                            )
                        )
                        result["caption"] = receipt(caption)
                        parts.append(result["caption"])
                elif intent is None and parent.action == "send_message":
                    # An unplanned legacy file/caption cannot prove whole-call success.
                    complete_plan = False
            result["status"] = (
                "succeeded"
                if complete_plan and all(p["status"] == "succeeded" for p in parts)
                else "uncertain"
                if not complete_plan
                or any(
                    p["status"] in {"prepared", "executing", "uncertain", "not_sent"} for p in parts
                )
                else "failed"
            )
            return result

    async def inspect_automation_delivery(
        self,
        *,
        conversation_id: str,
        run_id: int,
        step_id: str,
        script_hash: str,
        target_kind: Literal["person", "space"],
        target_id: str,
    ) -> AgentDeliveryOutcome:
        """Inspect the original run/step, including completed-work and restart replay.

        A successful receipt proves transport, not whether the text was progress or a
        satisfactory answer. The caller owns lifecycle and the requested target.
        Unknown effects cannot be cured by a later, different successful send.
        """
        database = self._database
        execution = f"automation:{run_id}:{step_id}:{script_hash}"
        source = social_source_key(f"{conversation_id}:execution:{execution}")
        target = {"kind": target_kind, "id": target_id}
        async with database.sessions() as session:
            if database.url.startswith("sqlite+"):
                # SQLite SELECT autobegin does not establish a database snapshot.
                # Keep reclamation from mixing an old work header with deleted effects;
                # deferred BEGIN remains a reader and never reserves the writer lock.
                await session.execute(text("BEGIN"))
            works = list(
                await session.execute(
                    select(work.c.id, work.c.state, work.c.checkpoint_json).where(
                        work.c.conversation_id == conversation_id,
                        func.json_extract(work.c.source_json, "$.owner") == "automation",
                        func.json_extract(work.c.source_json, "$.parent_execution_id") == execution,
                    )
                )
            )
            if any(_object(row.checkpoint_json).get("archived") for row in works):
                # Reclamation removes complete tool history, including failures that
                # never reached Social.prepare. Surviving sends are only partial proof.
                return AgentDeliveryOutcome("uncertain", "agent_delivery_archived")
            work_ids = [row.id for row in works]
            stored = list(
                (
                    await session.execute(select(effects).where(effects.c.work_id.in_(work_ids)))
                ).mappings()
            )
            snapshots = list(
                await session.scalars(
                    select(journal.c.payload_json).where(journal.c.work_id.in_(work_ids))
                )
            )
            intents = {
                row["id"]: _object(row["payload_json"])
                for row in (
                    await session.execute(
                        select(deliveries).where(deliveries.c.work_id.in_(work_ids))
                    )
                ).mappings()
            }
            roots = list(
                await session.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_conversation_id == conversation_id,
                        SocialOperationModel.source_turn_id == source,
                        SocialOperationModel.action.in_(("send_message", "send_message_sequence")),
                    )
                )
            )
            captions = (
                list(
                    await session.scalars(
                        select(SocialOperationModel).where(
                            SocialOperationModel.source_conversation_id == conversation_id,
                            SocialOperationModel.source_turn_id.in_(
                                [f"social-caption:{row.id}" for row in roots]
                            ),
                            SocialOperationModel.action == "send_file_caption",
                        )
                    )
                )
                if roots
                else []
            )

        social = {row.id: row for row in (*roots, *captions)}
        if any(row.status in {"executing", "uncertain"} for row in social.values()):
            return AgentDeliveryOutcome("uncertain", "send_outcome_unknown")

        pending_sends: set[str] = set()
        for snapshot in snapshots:
            value = _object(snapshot)
            chain = _object(value.get("transcript")).get("chain_id")
            sequence = _object(value.get("metadata")).get("sequence", 0)
            for call in value.get("pending", []):
                if isinstance(call, dict) and call.get("name") == "send_message":
                    pending_sends.add(f"{chain}:{sequence}:{call['id']}")

        attempts: list[tuple[float, DeliveryState]] = []
        covered_calls: set[str] = set()
        missing_results: set[str] = set()
        for effect in stored:
            effect_receipt = _object(effect["receipt_json"])
            result = _object(effect_receipt.get("result"))
            if (
                result.get("tool_name") != "send_message"
                and _object(effect_receipt.get("outcome")).get("tool") != "send_message"
                and effect["effect_key"] not in pending_sends
            ):
                continue
            invocation = _object(effect_receipt.get("invocation"))
            call_id = (
                effect["effect_key"]
                if invocation.get("version") == 1
                else effect["effect_key"].split(":", 2)[-1]
            )
            domain_ref = invocation.get("original_domain_ref")
            if isinstance(domain_ref, str) and domain_ref.startswith("social:"):
                linked = social.get(domain_ref.removeprefix("social:"))
                if linked is not None:
                    call_id = linked.tool_call_id
            body = _object(result.get("data"))
            if not result and effect["effect_key"] in pending_sends:
                missing_results.add(call_id)
                continue
            covered_calls.add(call_id)
            if (
                effect["state"] in {"unknown", "prepared"}
                or result.get("uncertain")
                or body.get("status") in {"uncertain", "unknown"}
            ):
                return AgentDeliveryOutcome("uncertain", "send_outcome_unknown")
            parts = _receipts(body)
            if any(part.get("status") in {"executing", "uncertain", "unknown"} for part in parts):
                return AgentDeliveryOutcome("uncertain", "send_outcome_unknown")
            actual_target = body.get("target")
            if actual_target is not None and actual_target != target:
                continue
            if not result.get("ok") or body.get("status") in {"failed", "cancelled"}:
                attempts.append((effect["created"], "failed"))
                continue
            complete = body.get("status") == "succeeded" and actual_target == target
            if "parts" in body:
                complete = complete and (
                    isinstance(body["parts"], list)
                    and len(parts) == body.get("planned_messages") == body.get("sent_messages")
                )
            complete = (
                complete
                and bool(parts)
                and all(
                    part.get("status") == "succeeded"
                    and (receipt := social.get(str(part.get("operation_id")))) is not None
                    and receipt.status == "succeeded"
                    and {"kind": receipt.target_kind, "id": receipt.target_id} == target
                    for part in parts
                )
            )
            if result.get("truncated") or not complete:
                return AgentDeliveryOutcome("uncertain", "send_aggregate_unavailable")
            attempts.append((effect["created"], "succeeded"))

        sequences = {row.tool_call_id for row in roots if row.action == "send_message_sequence"}
        sequence_prefixes = {
            f"seq:{hashlib.sha256(call.encode()).hexdigest()[:24]}:" for call in sequences
        }
        for row in roots:
            if {"kind": row.target_kind, "id": row.target_id} != target:
                continue
            if row.tool_call_id in covered_calls or any(
                row.tool_call_id.startswith(prefix) for prefix in sequence_prefixes
            ):
                continue
            if row.action == "send_message_sequence":
                if not _confirmed_sequence(row, roots):
                    # Historical parents have no plan; partial or ambiguous new plans
                    # must never be treated as whole-call delivery.
                    return AgentDeliveryOutcome("uncertain", "send_aggregate_unavailable")
                missing_results.discard(row.tool_call_id)
                attempts.append((row.created_at.replace(tzinfo=UTC).timestamp(), "succeeded"))
                continue
            if row.status in {"failed", "prepared"}:
                missing_results.discard(row.tool_call_id)
                attempts.append((row.created_at.replace(tzinfo=UTC).timestamp(), "failed"))
                continue
            arguments = _object(intents.get(row.id, {}).get("arguments"))
            if not arguments.get("text") or any(
                arguments.get(key) for key in ("artifact_id", "voice", "emoji")
            ):
                return AgentDeliveryOutcome("uncertain", "send_aggregate_unavailable")
            missing_results.discard(row.tool_call_id)
            attempts.append((row.created_at.replace(tzinfo=UTC).timestamp(), "succeeded"))

        if missing_results:
            return AgentDeliveryOutcome("uncertain", "send_outcome_unknown")
        if not attempts:
            return AgentDeliveryOutcome("none", "no_confirmed_send")
        state = max(attempts, key=lambda item: item[0])[1]
        if state == "succeeded" and (not works or any(row.state != "completed" for row in works)):
            return AgentDeliveryOutcome("failed", "agent_work_not_completed")
        return AgentDeliveryOutcome(
            state, "send_confirmed" if state == "succeeded" else "send_failed"
        )

    async def has_active_wait(self, work_id: str) -> bool:
        """Observe the original wait binding; delivery belongs to runtime maintenance."""
        from qq_ai_bot.runtime.work_wait_schema import waits

        async with self._database.sessions() as session:
            return (
                await session.scalar(
                    select(waits.c.id).where(waits.c.work_id == work_id, waits.c.status == "active")
                )
                is not None
            )
