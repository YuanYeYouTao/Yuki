"""Artifact references belong to the original event, independently of delivery strategy."""

import json

from sqlalchemy import delete, select, update
from tests.support.event_seed import seed_scoped_event

from yuki.domain.conversations import ConversationScope
from yuki.persistence.models import ChatEventArtifactRefModel, ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def references(database, event_id):
    async with database.read_session() as reader:
        return set(
            await reader.execute(
                select(
                    ChatEventArtifactRefModel.artifact_id, ChatEventArtifactRefModel.version
                ).where(ChatEventArtifactRefModel.event_id == event_id)
            )
        )


async def test_original_segment_pairs_are_distinct_and_follow_edit_and_forget(database):
    pair = {"artifact_id": "immutable-artifact", "version": "original-version"}
    original = (
        await seed_scoped_event(
            ScopedEventLedgerUnitOfWork(database),
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="original-file-reference",
            sender_user_id="8000",
            direction="outbound",
            content="original delivery",
            segments=(
                {"type": "text", "data": {"text": "original delivery"}},
                {"type": "file", "data": pair},
                {"type": "image", "data": pair},
                {"type": "file", "data": {"artifact_id": "missing-version"}},
                {"type": "file", "data": {"artifact_id": "typed-version", "version": 1}},
            ),
        )
    ).event
    assert await references(database, original.id) == {("immutable-artifact", "original-version")}
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == original.id)
            .values(content="caption-only update")
        )
    assert await references(database, original.id) == {("immutable-artifact", "original-version")}
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == original.id)
            .values(
                segments_json=json.dumps(
                    [
                        {
                            "type": "file",
                            "data": {
                                "artifact_id": "immutable-artifact",
                                "version": "replacement-version",
                            },
                        }
                    ]
                )
            )
        )
    assert await references(database, original.id) == {
        ("immutable-artifact", "replacement-version")
    }
    async with database.read_session() as reader:
        current = await reader.get(ChatEventModel, original.id)
        assert current.canonical_event_id == original.canonical_event_id
        assert current.author_presence_id == original.author_presence_id
        assert current.canonical_conversation_id == original.canonical_conversation_id
    async with database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == original.id))
    assert not await references(database, original.id)
