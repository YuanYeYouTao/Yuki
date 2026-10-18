"""One canonical revision owns current chunk retirement, preserving real reasons."""

import pytest
from sqlalchemy import delete, func, select, update
from tests.unit.test_history_missing_body import scene
from tests.unit.test_projection_chunk_storage import _prepare

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projection_models import PromptProjectionChunkModel, PromptProjectionModel
from yuki.conversation.projections import PromptProjectionRepository
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize(
    "mutation", ["audio", "metadata", "content", "reset", "delete", "revision", "noop"]
)
async def test_source_invalidation_updates_the_head_once_with_its_original_reason(
    database, mutation
):
    _, context, _ = await scene(database, 3)
    repository = PromptProjectionRepository(database)
    fragments = FrozenFragments.load([]).extend_history(context.history_fragments, ())
    publication = await _prepare(repository, context, fragments, reason="bootstrap")
    async with database.immediate_session() as writer:
        original = await publication(writer)
    conversation = context.read_version.conversation_id
    identity = context.history_fragments[-1][0][0]
    async with database.immediate_session() as writer:
        if mutation == "delete":
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == identity))
        elif mutation in {"reset", "revision"}:
            values = (
                {"generation": CanonicalConversationModel.generation + 1}
                if mutation == "reset"
                else {
                    "prompt_source_revision": CanonicalConversationModel.prompt_source_revision + 1
                }
            )
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == conversation)
                .values(**values)
            )
        else:
            values = {
                "audio": {"audio_transcript": '{"text":"source enrichment"}'},
                "metadata": {"sender_nickname": "changed source name"},
                "content": {"content": "corrected original content"},
                "noop": {"content": ChatEventModel.content},
            }[mutation]
            await writer.execute(
                update(ChatEventModel).where(ChatEventModel.id == identity).values(**values)
            )
    async with database.sessions() as reader:
        head = await reader.get(PromptProjectionModel, conversation)
        remaining = await reader.scalar(
            select(func.count()).select_from(PromptProjectionChunkModel)
        )
        if mutation == "noop":
            assert head.revision == original.revision and head.invalidated_reason is None
            assert remaining > 0
        else:
            reason = {"reset": "reset", "delete": "deleted_event"}.get(mutation, "source_changed")
            assert head.revision == original.revision + 1
            assert head.invalidated_reason == reason
            assert remaining == 0
    if mutation != "noop":
        assert await repository.read(conversation) is None
