"""Frozen SQL facts for historical migration tests, never current runtime adapters."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.persistence.database import Database


async def insert_frozen(session: AsyncSession, table: str, values: dict) -> None:
    """Write only the historical table's real columns, retaining its own defaults."""
    columns = {row[1] for row in await session.execute(text(f'PRAGMA table_info("{table}")'))}
    selected = {key: value for key, value in values.items() if key in columns}
    await session.execute(
        text(
            f'INSERT INTO "{table}" ('
            + ",".join(selected)
            + ") VALUES ("
            + ",".join(":" + key for key in selected)
            + ")"
        ),
        selected,
    )


async def seed_legacy_person(session: AsyncSession, account: str = "10001") -> str:
    owner = await session.scalar(
        text(
            "SELECT person_id FROM identity_bindings "
            "WHERE platform='qq' AND external_account_id=:id"
        ),
        {"id": account},
    )
    if owner is not None:
        return owner
    now = datetime.now(UTC)
    owner = str(uuid4())
    await insert_frozen(
        session,
        "persons",
        {
            "id": owner,
            "enabled": True,
            "revision": 1,
            "display_name": "person",
            "merged_into_person_id": None,
            "is_superuser": False,
            "created_at": now,
            "updated_at": now,
        },
    )
    await insert_frozen(
        session,
        "identity_bindings",
        {
            "id": str(uuid4()),
            "person_id": owner,
            "platform": "qq",
            "external_account_id": account,
            "display_name": "person",
            "status": "active",
            "revision": 1,
            "first_seen_at": now,
            "last_seen_at": now,
            "history_starts_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    return owner


async def seed_legacy_space(session: AsyncSession, native: str = "20001") -> str:
    space = await session.scalar(
        text("SELECT space_id FROM space_bindings WHERE platform='qq' AND external_space_id=:id"),
        {"id": native},
    )
    if space is not None:
        return space
    now = datetime.now(UTC)
    space = str(uuid4())
    await insert_frozen(
        session,
        "spaces",
        {
            "id": space,
            "name": "group",
            "enabled": True,
            "autonomous_enabled": True,
            "require_mention": True,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    await insert_frozen(
        session,
        "space_bindings",
        {
            "id": str(uuid4()),
            "space_id": space,
            "platform": "qq",
            "external_space_id": native,
            "display_name": "group",
            "status": "active",
            "revision": 1,
            "first_seen_at": now,
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    return space


async def seed_legacy_presence(session: AsyncSession, account: str = "80001") -> str:
    presence = await session.scalar(
        text("SELECT id FROM presences WHERE platform='qq' AND external_account_id=:id"),
        {"id": account},
    )
    if presence is not None:
        return presence
    now = datetime.now(UTC)
    presence = str(uuid4())
    await insert_frozen(
        session,
        "presences",
        {
            "id": presence,
            "platform": "qq",
            "external_account_id": account,
            "display_name": "Yuki",
            "enabled": True,
            "ingest_eligible": True,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    return presence


async def frozen_social_env(database: Database, *, private: bool = False) -> SimpleNamespace:
    """Seed original identity, Conversation and event rows without calling today's services."""
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        person = await seed_legacy_person(session)
        space = await seed_legacy_space(session)
        presence = await seed_legacy_presence(session)
        conversation = None
        for kind in ("space", "private") if private else ("space",):
            scope = f"bot:80001:{'group:20001' if kind == 'space' else 'private:10001'}"
            original = await session.scalar(
                text(
                    "SELECT conversation_id FROM conversation_legacy_aliases WHERE scope_key=:key"
                ),
                {"key": scope},
            )
            if original is None:
                original, alias = str(uuid4()), str(uuid4())
                binding = await session.scalar(
                    text("SELECT id FROM identity_bindings WHERE person_id=:id"), {"id": person}
                )
                space_binding = await session.scalar(
                    text("SELECT id FROM space_bindings WHERE space_id=:id"), {"id": space}
                )
                await insert_frozen(
                    session,
                    "canonical_conversations",
                    {
                        "id": original,
                        "kind": kind,
                        "person_id": person if kind == "private" else None,
                        "space_id": space if kind == "space" else None,
                        "identity_binding_id": binding if kind == "private" else None,
                        "entry_presence_id": presence,
                        "space_binding_id": space_binding if kind == "space" else None,
                        "thread_id": None,
                        "primary_alias_id": alias,
                        "primary_marker": 1,
                        "generation": 1,
                        "prompt_source_revision": 0,
                        "starts_after_event_id": 0,
                        "last_event_id": 0,
                        "last_generation_change_event_id": 0,
                        "covered_through_event_id": 0,
                        "uncovered_event_count": 0,
                        "uncovered_character_count": 0,
                        "revision": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                await insert_frozen(
                    session,
                    "conversation_legacy_aliases",
                    {
                        "id": alias,
                        "conversation_id": original,
                        "scope_key": scope,
                        "is_primary": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            if kind == "space":
                conversation = original
        event_id = await session.scalar(
            text("SELECT id FROM chat_events WHERE canonical_conversation_id=:id LIMIT 1"),
            {"id": conversation},
        )
        if event_id is None:
            await insert_frozen(
                session,
                "chat_events",
                {
                    "bot_user_id": "80001",
                    "platform_message_id": "inbound",
                    "scope_type": "group",
                    "group_id": "20001",
                    "sender_user_id": "10001",
                    "sender_nickname": "person",
                    "sender_group_card": "",
                    "direction": "inbound",
                    "event_kind": "message",
                    "content": "hello",
                    "visual_summary": "",
                    "audio_transcript": "",
                    "segments_json": "[]",
                    "origin": "user_message",
                    "occurred_at": now,
                    "observed_at": now,
                    "canonical_event_id": str(uuid4()),
                    "canonical_conversation_id": conversation,
                    "author_kind": "person",
                    "author_person_id": person,
                    "ingress_presence_id": presence,
                    "utterance_fingerprint": hashlib.sha256(b"hello").hexdigest(),
                    "suppression_status": "keeper",
                    "ingress_provider": "snowluma",
                    "ingress_gateway_instance_id": "historical-seed",
                },
            )
            event_id = await session.scalar(text("SELECT last_insert_rowid()"))
            await session.execute(
                text("UPDATE canonical_conversations SET last_event_id=:event WHERE id=:id"),
                {"event": event_id, "id": conversation},
            )
    return SimpleNamespace(
        person=person,
        presence=presence,
        space=space,
        context=SimpleNamespace(conversation_id=conversation),
        bot=SimpleNamespace(self_id="80001"),
    )
