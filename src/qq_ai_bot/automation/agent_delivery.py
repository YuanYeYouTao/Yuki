"""Verify an Agent step's delivery facts without generating or repeating a send."""

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
from qq_ai_bot.social.source_keys import social_source_key

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


async def inspect_agent_delivery(
    database: Database,
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
                await session.execute(select(deliveries).where(deliveries.c.work_id.in_(work_ids)))
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
        result = _object(_object(effect["receipt_json"]).get("result"))
        if result.get("tool_name") != "send_message" and effect["effect_key"] not in pending_sends:
            continue
        call_id = effect["effect_key"].split(":", 2)[-1]
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
            # The parent remains PREPARED even after a complete split send. Its
            # children do not retain the planned count; only the tool aggregate does.
            return AgentDeliveryOutcome("uncertain", "send_aggregate_unavailable")
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
    return AgentDeliveryOutcome(state, "send_confirmed" if state == "succeeded" else "send_failed")
