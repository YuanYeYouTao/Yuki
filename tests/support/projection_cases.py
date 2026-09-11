"""Durable prefix immutability, global budgets, reset and concurrent writer checks."""

import asyncio

import pytest

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    ProjectionConflict,
    ProjectionSnapshot,
    PromptProjectionRepository,
)


async def projection_storage_cases(database, conversation_id):
    async with database.sessions() as session:
        source = await session.get(CanonicalConversationModel, conversation_id)
        generation, starts = source.generation, source.starts_after_event_id
    repository = PromptProjectionRepository(database, max_context_characters=128)
    args = dict(
        view_key="a" * 64,
        conversation_id=conversation_id,
        generation=generation,
        starts_after_event_id=starts,
        context_key="b" * 64,
        contract_revision="c" * 64,
    )
    first = await repository.commit(**args, items=[{"text": "旧名字"}], rebuild_reason="bootstrap")
    copied = first.items()
    copied[0]["text"] = "mutated"
    reopened = PromptProjectionRepository(database, max_context_characters=128)
    assert (await reopened.read(args["view_key"])).items() == [{"text": "旧名字"}]
    cas = dict(expected_epoch=first.epoch_id, expected_revision=first.revision)
    with pytest.raises(ProjectionConflict, match="cannot be rewritten"):
        await repository.commit(**args, **cas, items=[{"text": "新名字"}])
    with pytest.raises(ProjectionCapacityError, match="view budget"):
        await repository.commit(**args, **cas, items=[{"text": "x" * 600}])
    ordered = await repository.commit(
        **{**args, "view_key": "f" * 64},
        items=[{"a": 1, "b": 2}],
        rebuild_reason="bootstrap",
    )
    with pytest.raises(ProjectionConflict, match="cannot be rewritten"):
        await repository.commit(
            **{**args, "view_key": "f" * 64},
            items=[{"b": 2, "a": 1}],
            expected_epoch=ordered.epoch_id,
            expected_revision=ordered.revision,
        )
    # Distinct repository instances race against one database, not a Python lock.
    results = await asyncio.gather(
        *(
            repo.commit(**args, **cas, items=[{"text": "旧名字"}, {"text": value}])
            for repo, value in ((repository, "one"), (reopened, "two"))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ProjectionConflict) for result in results) == 1
    current = await repository.read(args["view_key"])
    assert current.revision == 2
    rebuilt = await repository.commit(
        **args,
        expected_epoch=current.epoch_id,
        expected_revision=current.revision,
        items=[{"rollup": "summary"}],
        rebuild_reason="rollup",
    )
    assert rebuilt.epoch_id != current.epoch_id and rebuilt.revision == 1
    assert rebuilt.rebuild_reason == "rollup"
    async with database.sessions() as session, session.begin():
        source = await session.get(CanonicalConversationModel, conversation_id)
        source.generation += 1
    try:
        assert await repository.read(args["view_key"]) is None
        with pytest.raises(ProjectionConflict, match="source generation"):
            await repository.commit(**args, **cas, items=[])
        reset = await repository.commit(
            **{**args, "generation": generation + 1},
            items=[],
            rebuild_reason="reset",
        )
        assert reset.epoch_id != rebuilt.epoch_id
    finally:
        async with database.sessions() as session, session.begin():
            source = await session.get(CanonicalConversationModel, conversation_id)
            source.generation = generation
        await repository.invalidate(conversation_id)
    tiny = PromptProjectionRepository(database, max_context_characters=128, total_bytes=45)
    results = await asyncio.gather(
        *(
            tiny.commit(
                **{**args, "view_key": letter * 64},
                items=[{"text": "x" * 20}],
                rebuild_reason="bootstrap",
            )
            for letter in ("d", "e")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ProjectionCapacityError) for result in results) == 1
    assert sum(isinstance(result, ProjectionSnapshot) for result in results) == 1
    await tiny.invalidate(conversation_id)
