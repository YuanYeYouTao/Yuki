"""Actual platform entries have distinct history while retaining canonical owners."""

from datetime import UTC, datetime

import pytest

from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from qq_ai_bot.persistence.database import Database


@pytest.mark.asyncio
async def test_private_entries_share_person_without_merging_bot_history(database: Database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        person = await create_person_binding(
            session, external_account_id="42", display_name="P", now=now, platform="telegram"
        )
        bots = [
            await create_presence(
                session, external_account_id=account, now=now, platform="telegram"
            )
            for account in ("100", "101")
        ]
        entries = [
            await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key=f"telegram:bot:{bot.external_account_id}:private:42",
                person_id=person.person_id,
                identity_binding_id=person.id,
                presence_id=bot.id,
            )
            for bot in bots
        ]
        again = await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key="telegram:bot:100:private:42",
            person_id=person.person_id,
            identity_binding_id=person.id,
            presence_id=bots[0].id,
        )
    assert entries[0].person_id == entries[1].person_id
    assert entries[0].conversation_id != entries[1].conversation_id
    assert again.conversation_id == entries[0].conversation_id


@pytest.mark.asyncio
async def test_topics_keep_one_space_and_distinct_conversations(database: Database):
    async with database.immediate_session() as session:
        binding = await create_space_binding(
            session,
            external_space_id="-10042",
            name="G",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=datetime.now(UTC),
            platform="telegram",
        )
        topics = [
            await ensure_canonical_conversation(
                session,
                kind="space",
                primary_scope_key=f"telegram:bot:100:group:-10042:thread:{topic}",
                space_id=binding.space_id,
                space_binding_id=binding.id,
                thread_id=topic,
            )
            for topic in ("1", "2")
        ]
        again = await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="telegram:bot:101:group:-10042:thread:1",
            space_id=binding.space_id,
            space_binding_id=binding.id,
            thread_id="1",
        )
    assert topics[0].space_id == topics[1].space_id
    assert topics[0].conversation_id != topics[1].conversation_id
    assert again.conversation_id == topics[0].conversation_id
