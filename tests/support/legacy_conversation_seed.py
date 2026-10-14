"""Explicit canonical rows for migration fixtures before Rollup retirement."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text

from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_space
from qq_ai_bot.persistence.database import Database


async def seed_pre_0119_social_conversations(database: Database, *, private: bool = False) -> None:
    """Seed the frozen NOT NULL layout used by the 0104–0118 upgrade tests."""
    async with database.immediate_session() as writer:
        space = await ensure_space(writer, "20001")
        owners = [("space", None, space, "bot:80001:group:20001")]
        if private:
            person = await ensure_person(writer, "10001")
            owners.append(("private", person, None, "bot:80001:private:10001"))
        now = datetime.now(UTC)
        for kind, person_id, space_id, scope_key in owners:
            conversation, alias = str(uuid4()), str(uuid4())
            values = {
                "id": conversation,
                "alias": alias,
                "kind": kind,
                "person": person_id,
                "space": space_id,
                "scope": scope_key,
                "now": now,
            }
            await writer.execute(
                text(
                    "INSERT INTO canonical_conversations("
                    "id,kind,person_id,space_id,primary_alias_id,primary_marker,generation,"
                    "starts_after_event_id,last_event_id,last_generation_change_event_id,"
                    "covered_through_event_id,uncovered_event_count,uncovered_character_count,"
                    "revision,created_at,updated_at) "
                    "VALUES(:id,:kind,:person,:space,:alias,1,1,0,0,0,0,0,0,1,:now,:now)"
                ),
                values,
            )
            await writer.execute(
                text(
                    "INSERT INTO conversation_legacy_aliases("
                    "id,conversation_id,scope_key,is_primary,created_at,updated_at) "
                    "VALUES(:alias,:id,:scope,1,:now,:now)"
                ),
                values,
            )
