"""Database mutations erase cached bodies atomically and reject stale writers."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.projections import ProjectionConflict
from qq_ai_bot.persistence.models import ChatEventModel


async def projection_invalidation_cases(database, repository, args):
    async def seed(reason):
        async with database.sessions() as session:
            source = await session.get(CanonicalConversationModel, args["conversation_id"])
            args["expected_source_revision"] = source.prompt_source_revision
        return await repository.commit(
            **args, items=[{"text": "cached body"}], rebuild_reason=reason
        )

    await repository.invalidate(args["conversation_id"])
    async with database.sessions() as session:
        source = await session.get(CanonicalConversationModel, args["conversation_id"])
        args["expected_source_revision"] = source.prompt_source_revision
        event = await session.scalar(
            select(ChatEventModel)
            .where(
                ChatEventModel.canonical_conversation_id == args["conversation_id"],
            )
            .limit(1)
        )
        untouched_id, untouched_content = event.id, event.content
    async with database.sessions() as session, session.begin():
        event = await session.get(ChatEventModel, untouched_id)
        event.content = "edit before first projection"
    try:
        with pytest.raises(ProjectionConflict, match="source revision changed"):
            await repository.commit(
                **args, items=[{"text": untouched_content}], rebuild_reason="bootstrap"
            )
        assert await repository.read(args["view_key"]) is None
    finally:
        async with database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, untouched_id)
            event.content = untouched_content
    current = await seed("bootstrap")
    async with database.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel)
            .where(
                ChatEventModel.canonical_conversation_id == args["conversation_id"],
            )
            .limit(1)
        )
        event_id, original = event.id, event.content
        clone = {
            column.name: getattr(event, column.name) for column in ChatEventModel.__table__.columns
        }
    # Rollback must undo the cache invalidation along with the source edit.
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, event_id)
            event.content = "changed"
            await session.flush()
            raise RuntimeError("rollback")
    assert (await repository.read(args["view_key"])).epoch_id == current.epoch_id
    async with database.sessions() as session, session.begin():
        event = await session.get(ChatEventModel, event_id)
        event.content = "changed"
    try:
        assert await repository.read(args["view_key"]) is None
        assert await repository.invalidation_reason(args["view_key"]) == "source_changed"
        async with database.sessions() as session:
            stored = await session.get(PromptProjectionModel, args["view_key"])
            assert stored.payload_json == "[]" and stored.byte_size == 2
        with pytest.raises(ProjectionConflict, match="revision changed"):
            await repository.commit(
                **args,
                items=current.items(),
                expected_epoch=current.epoch_id,
                expected_revision=current.revision,
            )
    finally:
        async with database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, event_id)
            event.content = original
    await seed("source_changed")
    # A new event only extends history; deleting it is an explicit boundary.
    clone.update(
        id=None, platform_message_id=f"projection-{uuid4()}", canonical_event_id=str(uuid4())
    )
    async with database.sessions() as session, session.begin():
        inserted = ChatEventModel(**clone)
        session.add(inserted)
        await session.flush()
        inserted_id = inserted.id
    assert await repository.read(args["view_key"]) is not None
    async with database.sessions() as session, session.begin():
        await session.delete(await session.get(ChatEventModel, inserted_id))
    assert await repository.invalidation_reason(args["view_key"]) == "deleted_event"
    await seed("deleted_event")
    for model in (
        CanonicalConversationRollupModel,
        CanonicalConversationRollupEmergencyOverlayModel,
    ):
        now = datetime.now(UTC)
        values = dict(
            conversation_id=args["conversation_id"],
            generation=args["generation"],
            covered_through_event_id=0,
            summary_text="summary",
            source_fingerprint="0" * 64,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        if model is CanonicalConversationRollupModel:
            values["summary_kind"] = "extractive"
        async with database.sessions() as session, session.begin():
            assert await session.get(model, args["conversation_id"]) is None
            session.add(model(**values))
        assert await repository.invalidation_reason(args["view_key"]) == "rollup"
        await seed("rollup")
        async with database.sessions() as session, session.begin():
            row = await session.get(model, args["conversation_id"])
            row.revision += 1
        assert await repository.read(args["view_key"]) is None
        await seed("rollup")
        async with database.sessions() as session, session.begin():
            await session.delete(await session.get(model, args["conversation_id"]))
        assert await repository.invalidation_reason(args["view_key"]) == "rollup"
        await seed("rollup")
    await repository.invalidate(args["conversation_id"])
