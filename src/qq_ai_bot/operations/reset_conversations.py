"""Idempotent, offline /ai new equivalent for every canonical conversation."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationModel, CanonicalGenerationResetModel


async def reset_all(database: Database, batch_id: str, *, apply: bool) -> dict[str, int]:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch_id) is None:
        raise ValueError("invalid_batch_id")
    async with database.sessions() as session:
        ids = (
            await session.scalars(
                select(CanonicalConversationModel.id).order_by(CanonicalConversationModel.id)
            )
        ).all()
        completed = int(
            await session.scalar(
                select(func.count())
                .select_from(CanonicalGenerationResetModel)
                .where(CanonicalGenerationResetModel.batch_id == batch_id)
            )
            or 0
        )
        running_automation = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM automation_runs r "
                    "JOIN automations a ON a.id = r.automation_id "
                    "WHERE r.status='running' AND a.status IN ('active', 'paused')"
                )
            )
            or 0
        )
        running_work = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM runtime_work "
                    "WHERE state IN ('running', 'waiting_external')"
                )
            )
            or 0
        )
    summary = {
        "total": len(ids),
        "already_reset": completed,
        "running_automation": running_automation,
        "running_work": running_work,
        "newly_reset": 0,
        "self_automations_rebound": 0,
    }
    if not apply:
        return summary
    if running_automation or running_work:
        raise RuntimeError("active_execution_must_drain_before_generation_reset")
    for conversation_id in ids:
        async with database.immediate_session() as session:
            existing = await session.get(CanonicalGenerationResetModel, (batch_id, conversation_id))
            if existing is not None:
                continue
            row = await session.get(CanonicalConversationModel, conversation_id)
            if row is None:
                raise RuntimeError("conversation_disappeared_during_reset")
            prior = int(row.generation)
            floor = int(row.last_event_id)
            generation = await bump_canonical_generation(
                session, conversation_id, event_id=floor, force=True
            )
            automations = (
                await session.scalars(
                    select(AutomationModel).where(
                        AutomationModel.creator_kind == "self",
                        AutomationModel.status.in_(("active", "paused")),
                    )
                )
            ).all()
            for automation in automations:
                authority = json.loads(automation.authority_snapshot_json)
                if authority.get("canonical_conversation_id") != conversation_id:
                    continue
                if authority.get("conversation_generation") != prior:
                    # Already stale before the cutover: do not resurrect it.
                    continue
                authority["conversation_generation"] = generation
                automation.authority_snapshot_json = json.dumps(
                    authority, ensure_ascii=False, separators=(",", ":")
                )
                automation.updated_at = datetime.now(UTC)
                summary["self_automations_rebound"] += 1
            session.add(
                CanonicalGenerationResetModel(
                    batch_id=batch_id,
                    conversation_id=conversation_id,
                    prior_generation=prior,
                    new_generation=generation,
                    floor_event_id=floor,
                    created_at=datetime.now(UTC),
                )
            )
        summary["newly_reset"] += 1
    async with database.sessions() as session:
        complete = int(
            await session.scalar(
                select(func.count())
                .select_from(CanonicalGenerationResetModel)
                .where(CanonicalGenerationResetModel.batch_id == batch_id)
            )
            or 0
        )
    if complete != len(ids):
        raise RuntimeError("generation_reset_incomplete")
    return summary


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument(
        "--apply-offline", action="store_true", help="Bot ingress must already be stopped"
    )
    args = parser.parse_args()
    database = Database(Settings().database_url)
    try:
        result = await reset_all(database, args.batch_id, apply=args.apply_offline)
        print(json.dumps(result, sort_keys=True))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(_main())
