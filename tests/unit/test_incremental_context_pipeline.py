"""Real SQLite/Assembler/Composer/provider dispatch keeps only actual raw delta work."""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_history_dispatch_ownership import _scene

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import PromptProjectionRepository
from yuki.conversation.prompt_history import PromptHistorySource
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.llm.openai_compatible import OpenAICompatibleProvider


@pytest.mark.parametrize("request_limit, expected_main_calls", [(2, 0), (3, 1)])
async def test_preparation_requests_reach_main_budget_without_prompt_hashes(
    database, tmp_path, monkeypatch, request_limit, expected_main_calls
):
    from yuki.llm.fake import FakeLLMProvider
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_budget import WorkBudgetExceeded

    provider = FakeLLMProvider(lambda _: "内部结束")
    _env, harness, chat, _state, message = await _scene(
        database, tmp_path, provider, request_limit=request_limit
    )
    compose = chat.runtime.main_turns.compose
    run = chat._run_agent
    completed = []
    exhausted = []

    async def prepared(**kwargs):
        # These calls have already happened in preparation. Preserve their cost
        # even when no request slot remains for the ordinary Main response.
        return replace(await compose(**kwargs), preparation_model_requests=2)

    async def capture(key, messages, runtime, **kwargs):
        assert runtime.preparation_model_requests == 2
        try:
            result = await run(key, messages, runtime, **kwargs)
        except WorkBudgetExceeded:
            exhausted.append(current_work_control.get().requests_started)
            raise
        completed.append(result.result)
        return result

    monkeypatch.setattr(chat.runtime.main_turns, "compose", prepared)
    monkeypatch.setattr(chat, "_run_agent", capture)
    await harness.processor.handle(message, MemorySender())
    assert len(provider.requests) == expected_main_calls
    if expected_main_calls:
        assert len(completed) == 1 and not exhausted
        assert completed[0].model_requests == 2 + expected_main_calls
    else:
        assert not completed and exhausted == [2]


async def test_real_foreground_delta_preserves_full_render_bytes_and_dispatch(
    database, tmp_path, monkeypatch
):
    wire = []

    async def transport(request):
        wire.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "内部结束"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        env, harness, chat, state, message = await _scene(database, tmp_path, provider)
        for number in range(64):
            await seed_scoped_event(
                env.service.writer,
                scope=message.scope(),
                platform_message_id=f"raw-{number}",
                sender_user_id="10001",
                direction="inbound",
                content=f"历史正文{number}",
            )
        captured, costs = [], []
        original = chat.runtime.main_turns.compose
        render = ChatEventPromptRenderer.reference_message
        processed = []
        loaded = []
        production = True
        extend = PromptHistorySource.extend

        def count_loaded(self, rows, **kwargs):
            if production:
                loaded.extend(row.id for row in rows)
            return extend(self, rows, **kwargs)

        def count(self, row, **kwargs):
            if production:
                processed.append(row.id)
            return render(self, row, **kwargs)

        async def compose(**kwargs):
            nonlocal production
            production = False
            context = kwargs["context"]
            raw = await chat._conversation_scopes.load_prompt_snapshot(message.scope())
            previous = context.projection_snapshot
            frozen = previous.fragments if previous else FrozenFragments.load([])
            # The old algorithm renders the entire authorized tail before
            # discarding adopted bytes. Keep that oracle only in this test.
            renderer = ChatEventPromptRenderer(
                raw.raw_events,
                yuki_account_ids=message.yuki_account_ids,
                bot_display_name=harness.settings.bot_display_name,
                timezone=harness.settings.default_timezone,
            )
            grouped, individual = renderer.main_agent_history_views(
                row for row in raw.raw_events if row.id != context.current_event_id
            )
            expected = frozen.extend_history(
                tuple((ids, text) for _, ids, text in grouped),
                tuple((ids, text) for _, ids, text in individual),
            )
            assert tuple(context.history_messages) == expected.messages()
            assert context.current_message == renderer.reference_message(
                next(row for row in raw.raw_events if row.id == context.current_event_id),
                current_event_id=context.current_event_id,
                current_content=kwargs["inbound"].text,
            )
            oracle = chat._prompt_composer.compose(
                inbound=kwargs["inbound"],
                context=replace(context, history_messages=expected.messages()),
                runtime=kwargs["runtime"],
                short_state=state.snapshot(),
            )
            production = True
            actual = await original(**kwargs)
            assert actual.messages == oracle.messages
            captured.append((context, actual))
            return actual

        chat.runtime.main_turns.compose = compose
        monkeypatch.setattr(ChatEventPromptRenderer, "reference_message", count)
        monkeypatch.setattr(PromptHistorySource, "extend", count_loaded)
        for number in range(3):
            processed.clear()
            loaded.clear()
            result = await harness.processor.handle(
                replace(message, message_id=f"trigger-{number}", text=f"当前要求{number}"),
                MemorySender(),
            )
            assert result.reason == "chat"
            costs.append((tuple(loaded), tuple(processed)))
        assert len(wire) == 3 and len(captured) == 3
        for (_, composition), payload in zip(captured, wire, strict=True):
            for item in composition.messages:
                assert {"role": item.role, "content": item.content} in payload["messages"]
        assert captured[-1][0].projection_snapshot is not None
        for (context, _), cost in zip(captured[1:], costs[1:], strict=True):
            assert cost == ((context.current_event_id,), (context.current_event_id,))
        # Raw history API keeps its full-tail contract while foreground preparation
        # fetches only pending bodies. Re-read the stable epoch without rendering.
        snapshot = await chat._conversation_scopes.prepare_prompt_snapshot(message.scope())
        assert snapshot.raw_events == ()
        assert (
            len((await chat._conversation_scopes.load_prompt_snapshot(message.scope())).raw_events)
            == len(costs[0][0]) + 2
        )
        assert captured[-1][0].history_fragments == ()


async def test_legacy_summary_rebuild_reads_raw_bodies_from_real_assembler(database, tmp_path):
    from yuki.llm.fake import FakeLLMProvider

    env, harness, chat, _, message = await _scene(
        database, tmp_path, FakeLLMProvider(lambda _: "内部结束")
    )
    await seed_scoped_event(
        env.service.writer,
        scope=message.scope(),
        platform_message_id="legacy-old",
        sender_user_id="10001",
        direction="inbound",
        content="必须重建的原始正文",
    )
    assert (await harness.processor.handle(message, MemorySender())).reason == "chat"
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PromptProjectionModel).values(
                selected_summary_text="旧摘要",
                selected_summary_kind=None,
                selected_summary_renderer=0,
                selected_summary_coverage=0,
            )
        )
    # A new owner proves cold loading as well as excluding obsolete IDs from
    # the new candidate's frozen membership.
    projections = PromptProjectionRepository(database)
    chat._conversation_scopes.projections = projections
    chat.runtime.main_turns._projections = projections
    captured = []
    original = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs["context"])
        return await original(**kwargs)

    chat.runtime.main_turns.compose = capture
    assert (
        await harness.processor.handle(
            replace(message, message_id="legacy-next", text="继续"), MemorySender()
        )
    ).reason == "chat"
    assert "必须重建的原始正文" in "\n".join(
        item.content or "" for item in captured[0].history_messages
    )
    async with database.sessions() as reader:
        head = (await reader.scalars(select(PromptProjectionModel))).one()
        assert head.rebuild_reason == "source_changed" and head.selected_summary_renderer == 2


async def test_concurrent_admission_cannot_remove_an_older_candidates_captured_holes(
    database, tmp_path, monkeypatch
):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.llm.fake import FakeLLMProvider

    env, _, chat, _, message = await _scene(database, tmp_path, FakeLLMProvider())
    for number in range(2):
        await seed_scoped_event(
            env.service.writer,
            scope=message.scope(),
            platform_message_id=f"hole-{number}",
            sender_user_id="10001",
            direction="inbound",
            content=f"未接纳洞{number}",
        )
    scopes = chat._conversation_scopes
    raw = await scopes.load_prompt_snapshot(message.scope())
    repository = scopes.projections
    renderer = ChatEventPromptRenderer(raw.raw_events)
    first, *holes = raw.raw_events
    initial = FrozenFragments.load([]).append_current(first.id, renderer.reference_message(first))

    async def publish(fragments, previous=None):
        publication = await repository.prepare_commit(
            view_key=raw.scope.id,
            conversation_id=raw.scope.id,
            generation=raw.scope.generation,
            expected_source_revision=raw.prompt_source_revision,
            starts_after_event_id=raw.scope.starts_after_event_id,
            context_key="same-source",
            fragments=fragments,
            expected_epoch=previous.epoch_id if previous else None,
            expected_revision=previous.revision if previous else 0,
            previous_snapshot=previous,
            rebuild_reason=None if previous else "bootstrap",
        )
        async with database.immediate_session() as writer:
            return await publication(writer)

    await publish(initial)
    warm = await scopes.prepare_prompt_snapshot(message.scope())
    base = warm.projection
    expected_ids = tuple(row.id for row in holes)
    assert tuple(row.id for row in warm.raw_events) == expected_ids
    query_started, release = asyncio.Event(), asyncio.Event()
    scalars = AsyncSession.scalars
    candidate_task = None

    async def paused(reader, statement, *args, **kwargs):
        sql = str(statement)
        if (
            asyncio.current_task() is candidate_task
            and not query_started.is_set()
            and "FROM chat_events" in sql
            and "chat_events.id >" in sql
        ):
            query_started.set()
            await release.wait()
        return await scalars(reader, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalars", paused)
    candidate_task = asyncio.create_task(scopes.prepare_prompt_snapshot(message.scope()))
    try:
        await asyncio.wait_for(query_started.wait(), 5)
        later = base.fragments.extend_history(
            tuple(((row.id,), renderer.reference_message(row)) for row in holes), ()
        )
        await publish(later, base)
        accepted = await scopes.prepare_prompt_snapshot(message.scope())
        assert accepted.raw_events == () and warm.source.pending(raw.scope.last_event_id) == ()
    finally:
        release.set()
        candidate = await candidate_task
    # Both the selected head and the unadmitted body selection belong to the
    # original read snapshot. The other candidate's committed head stays intact.
    assert candidate.projection is base
    assert tuple(row.id for row in candidate.raw_events) == expected_ids
    assert candidate.frozen_prefix.event_ids == {first.id}
    assert accepted.projection.fragments.event_ids == {row.id for row in raw.raw_events}
