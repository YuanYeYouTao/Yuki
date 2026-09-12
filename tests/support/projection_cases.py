"""Durable prefix immutability, global budgets, reset and concurrent writer checks."""

import asyncio

import pytest
from sqlalchemy import func, select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    ProjectionConflict,
    ProjectionSnapshot,
    PromptProjectionRepository,
)


async def projection_storage_cases(database, conversation_id):
    from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
    from qq_ai_bot.domain.messages import ChatMessage, ProviderContinuation

    frozen = FrozenFragments.load([]).append_current(
        1, ChatMessage(role="user", content="old dynamic\n旧名字：第一条")
    )
    # A newly rendered group includes an old event with a changed display name.
    # Only the new event's separately rendered fragment may be appended.
    extended = frozen.extend_history(
        (((1, 2), ChatMessage(role="user", content="新名字：第一条\n第二条")),),
        (
            ((1,), ChatMessage(role="user", content="新名字：第一条")),
            ((2,), ChatMessage(role="user", content="新名字：第二条")),
        ),
    ).append_current(3, ChatMessage(role="user", content="new dynamic\n第三条"))
    assert extended.items[:1] == frozen.items
    assert extended.messages()[1].content == "新名字：第二条"
    assert extended.event_ids == {1, 2, 3}
    with pytest.raises(ProjectionConflict, match="coverage is incomplete"):
        frozen.extend_history((((1, 2), ChatMessage(role="user", content="merged")),), ())
    with pytest.raises(ProjectionConflict, match="trigger already exists"):
        extended.append_current(3, ChatMessage(role="user", content="duplicate"))
    for unsafe in (
        {"type": "reasoning", "content": "must not persist"},
        {"type": "unknown_provider_item", "content": "unknown"},
        {"type": "message", "content": [{"type": "input_image", "image_url": "secret"}]},
    ):
        with pytest.raises(ProjectionConflict, match="explicit boundary"):
            extended.append_responses(
                ProviderContinuation(
                    provider="deepseek",
                    protocol="responses",
                    profile_id="wire",
                    payload=(unsafe,),
                )
            )

    async with database.sessions() as session:
        source = await session.get(CanonicalConversationModel, conversation_id)
        generation, starts = source.generation, source.starts_after_event_id
        source_revision = source.prompt_source_revision
    repository = PromptProjectionRepository(database, max_context_characters=128)
    args = dict(
        view_key="a" * 64,
        conversation_id=conversation_id,
        generation=generation,
        expected_source_revision=source_revision,
        starts_after_event_id=starts,
        context_key="b" * 64,
        contract_revision="c" * 64,
    )
    saved = await repository.commit(**args, items=list(extended.items), rebuild_reason="bootstrap")
    assert FrozenFragments.load(saved.items()).messages() == extended.messages()
    await repository.invalidate_view(args["view_key"], reason="protocol_changed")
    assert await repository.read(args["view_key"]) is None
    assert await repository.invalidation_reason(args["view_key"]) == "protocol_changed"
    with pytest.raises(ProjectionConflict, match="revision changed"):
        await repository.commit(
            **args,
            items=list(extended.items),
            expected_epoch=saved.epoch_id,
            expected_revision=saved.revision,
        )
    replacement = await repository.commit(
        **args, items=list(extended.items), rebuild_reason="protocol_changed"
    )
    assert replacement.epoch_id != saved.epoch_id
    await repository.invalidate(conversation_id)
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
            **{
                **args,
                "generation": generation + 1,
                "expected_source_revision": source_revision + 1,
            },
            items=[],
            rebuild_reason="reset",
        )
        assert reset.epoch_id != rebuilt.epoch_id
    finally:
        async with database.sessions() as session, session.begin():
            source = await session.get(CanonicalConversationModel, conversation_id)
            source.generation = generation
        await repository.invalidate(conversation_id)
        args["expected_source_revision"] = source_revision + 2
    async with database.sessions() as session:
        existing_bytes = await session.scalar(
            select(func.coalesce(func.sum(PromptProjectionModel.byte_size), 0))
        )
    tiny = PromptProjectionRepository(
        database, max_context_characters=128, total_bytes=int(existing_bytes) + 45
    )
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
    from tests.support.projection_invalidation_cases import projection_invalidation_cases

    await projection_invalidation_cases(database, repository, args)
    reclaiming = PromptProjectionRepository(
        database, max_context_characters=128, maximum_views=1, reclaim=True
    )
    first_key, second_key = "8" * 64, "9" * 64
    first = await reclaiming.commit(
        **{**args, "view_key": first_key}, items=[{"text": "first"}], rebuild_reason="bootstrap"
    )
    await reclaiming.commit(
        **{**args, "view_key": second_key}, items=[{"text": "second"}], rebuild_reason="bootstrap"
    )
    assert await reclaiming.read(first_key) is None
    assert await reclaiming.invalidation_reason(first_key) == "capacity"
    with pytest.raises(ProjectionConflict, match="revision changed"):
        await reclaiming.commit(
            **{**args, "view_key": first_key},
            items=[{"text": "stale"}],
            expected_epoch=first.epoch_id,
            expected_revision=first.revision,
        )
    rebuilt = await reclaiming.commit(
        **{**args, "view_key": first_key}, items=[{"text": "rebuilt"}], rebuild_reason="capacity"
    )
    assert rebuilt.epoch_id != first.epoch_id
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PromptProjectionModel)) <= 2
    await reclaiming.invalidate(conversation_id)
