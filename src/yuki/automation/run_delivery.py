"""On-demand delivery observations from the original run and effect owners."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import func, or_, select, text

from yuki.automation.models import AutomationRunRecord
from yuki.persistence.database import Database
from yuki.persistence.models import AutomationVersionModel
from yuki.runtime.work_recovery_schema import deliveries, invocations
from yuki.runtime.work_schema_v1 import work
from yuki.social.db_models import SocialEmojiIntentModel, SocialOperationModel
from yuki.social.repository import sequence_prefix
from yuki.social.source_keys import social_source_key


async def read_run_deliveries(
    database: Database, runs: tuple[AutomationRunRecord, ...]
) -> tuple[AutomationRunRecord, ...]:
    """Read a bounded authorized history page without changing its stored counters.

    Source keys and Work parent identities locate effects; text, timestamps and
    platform message IDs never establish ownership. Counts describe Social parts,
    while Work reservations retain their own states and message counts.
    """
    if not runs:
        return runs
    observed = []
    async with database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        versions = {
            version.id: version
            for version in await reader.scalars(
                select(AutomationVersionModel).where(
                    AutomationVersionModel.id.in_(run.version_id for run in runs)
                )
            )
        }
        for run in runs:
            version = versions.get(run.version_id) if run.version_id is not None else None
            scene = (
                json.loads(version.authority_snapshot_json)
                if version is not None and version.authority_snapshot_json is not None
                else {}
            )
            conversation_id = scene.get("canonical_conversation_id")
            if conversation_id is None:
                observed.append(
                    run.model_copy(update={"delivery": {"evidence": "legacy_counter_only"}})
                )
                continue
            assert version is not None
            execution_id = f"automation:{run.id}:{version.script_hash}"
            original_work_id = await reader.scalar(
                select(func.json_extract(invocations.c.payload_json, "$.work_id")).where(
                    invocations.c.run_id == run.id,
                    invocations.c.script_hash == version.script_hash,
                )
            )
            owned = (
                select(work.c.id)
                .where(
                    or_(work.c.source_key == execution_id, work.c.id == original_work_id),
                    func.json_extract(work.c.source_json, "$.owner") == "automation",
                    func.json_extract(work.c.source_json, "$.automation_run_id") == run.id,
                )
                .cte(recursive=True)
            )
            owned = owned.union_all(
                select(work.c.id).join(owned, work.c.parent_work_id == owned.c.id)
            )
            work_ids = list(await reader.scalars(select(owned.c.id)))
            reservations = (
                (await reader.execute(select(deliveries).where(deliveries.c.work_id.in_(work_ids))))
                .mappings()
                .all()
            )
            original = json.loads(version.script_json)
            execution_ids = [execution_id] + [
                f"automation:{run.id}:{step['id']}:{version.script_hash}"
                for step in original.get("steps", ())
            ]
            source_keys = [
                social_source_key(f"{conversation_id}:execution:{identity}")
                for identity in (*execution_ids, *work_ids)
            ]
            operations = list(
                await reader.scalars(
                    select(SocialOperationModel).where(
                        or_(
                            SocialOperationModel.source_turn_id.in_(source_keys),
                            SocialOperationModel.id.in_(item["id"] for item in reservations),
                        ),
                        SocialOperationModel.action.in_(("send_message", "send_message_sequence")),
                    )
                )
            )
            # Historical file captions have their own exact parent-derived key.
            operations.extend(
                await reader.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_turn_id.in_(
                            f"social-caption:{item.id}" for item in operations
                        ),
                        SocialOperationModel.action == "send_file_caption",
                    )
                )
            )
            pending_emoji = list(
                await reader.scalars(
                    select(SocialEmojiIntentModel.id).where(
                        SocialEmojiIntentModel.source_turn_id.in_(source_keys),
                        SocialEmojiIntentModel.closed.is_(False),
                    )
                )
            )
            parts = Counter(
                item.status for item in operations if item.action != "send_message_sequence"
            )
            sequences = []
            for parent in operations:
                if parent.action != "send_message_sequence":
                    continue
                count = parent.planned_parts
                children = sum(
                    item.source_turn_id == parent.source_turn_id
                    and item.tool_call_id.startswith(sequence_prefix(parent.tool_call_id))
                    for item in operations
                )
                sequences.append(
                    {"operation_id": parent.id, "status": parent.status, "planned_parts": count}
                )
                if count is not None:
                    parts["not_sent"] += count - children
            delivery: dict[str, Any] = {
                "evidence": "source_linked_receipts",
                "confirmed_parts": parts["succeeded"],
                "failed_parts": parts["failed"],
                "unconfirmed_parts": parts["prepared"] + parts["executing"] + parts["uncertain"],
                "not_sent_parts": parts["not_sent"],
                "sequences": sequences,
                "pending_emoji_intent_ids": pending_emoji,
                "work_reservations": [
                    {key: item[key] for key in ("id", "work_id", "state", "message_count")}
                    for item in reservations
                ],
            }
            if not operations and not reservations and not pending_emoji:
                delivery["evidence"] = (
                    "no_send_recorded"
                    if run.result_summary.get("messages_sent_semantics")
                    in {"social_successful_parts", "work_delivery_reservations"}
                    and run.messages_sent == 0
                    else "legacy_counter_only"
                )
                if delivery["evidence"] == "legacy_counter_only":
                    delivery["confirmed_parts"] = None
            observed.append(run.model_copy(update={"delivery": delivery}))
    return tuple(observed)
