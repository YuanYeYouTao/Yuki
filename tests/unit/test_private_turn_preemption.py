"""Original-owner cancellation joins real cleanup and protects actual effects."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_model_telemetry_failures import executor

from qq_ai_bot.conversation.scope import runtime_conversation_key
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    NativeToolDefinition,
    NativeToolType,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol, ModelTask
from qq_ai_bot.model_runtime.request_accounting import current_provider_attempts
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.message_splitter import OutboundMessageSplitter
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
)


async def flush():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("nested", [False, True])
async def test_repeated_private_inputs_keep_original_effect_protection(nested):
    turns = ConversationTurnCoordinator()
    original = await turns.notify_message("private")
    entered, release = asyncio.Event(), asyncio.Event()

    async def old():
        async with turns.track(original, "admission"):
            if nested:
                async with turns.track(original, "generation"):
                    await turns.mark_mutation_started(original)
            else:
                await turns.mark_mutation_started(original)
            entered.set()
            await release.wait()

    task = asyncio.create_task(old())
    await entered.wait()
    first = await turns.notify_message("private", preempt_private=True)
    assert not turns.is_current(original) and turns.is_current(first)
    # Even late marking still belongs to the original registered task/token.
    await turns.mark_mutation_started(original)
    second = await turns.notify_message("private", preempt_private=True)
    await flush()
    assert turns.is_current(second) and not task.done()
    assert turns._states["private"].registrations[task].effect_started
    release.set()
    await task
    assert not turns._states["private"].registrations


@pytest.mark.parametrize("preserve", [False, True])
async def test_group_and_accepted_work_do_not_use_private_preemption(preserve):
    turns = ConversationTurnCoordinator()
    original = await turns.notify_message("conversation")
    entered, release = asyncio.Event(), asyncio.Event()

    async def old():
        async with turns.track(original, "generation"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(old())
    await entered.wait()
    await turns.notify_message("conversation", preserve_active=preserve, preempt_private=preserve)
    await flush()
    assert not task.done()
    assert turns.is_current(original) is preserve
    release.set()
    await task


async def test_private_join_is_original_owner_and_survives_new_waiter_cancellation():
    turns = ConversationTurnCoordinator()
    original = await turns.notify_message("private")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup_cancellations = 0

    async def old():
        nonlocal cleanup_cancellations
        async with turns.track(original, "admission"):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cleanup_cancellations += 1
                    raise
                raise

    old_task = asyncio.create_task(old())
    await entered.wait()
    notification = asyncio.create_task(turns.notify_message("private", preempt_private=True))
    await cleaning.wait()
    again = asyncio.create_task(turns.notify_message("private", preempt_private=True))
    notification.cancel()
    await flush()
    assert not notification.done() and not again.done() and not old_task.done()
    notification.cancel()
    await flush()
    assert not notification.done() and not old_task.done()
    release.set()
    result = await asyncio.gather(old_task, notification, again, return_exceptions=True)
    assert isinstance(result[0], TurnInterruptedError)
    assert isinstance(result[1], asyncio.CancelledError)
    assert cleanup_cancellations == 0
    # A subsequent owner is not accidentally cancelled by either original join.
    token = await turns.notify_message("private")
    async with turns.track(token, "generation"):
        assert turns.is_current(token)


async def test_private_http_cancel_joins_transport_before_slot_and_mutex_release():
    turns, concurrency = ConversationTurnCoordinator(), ConcurrencyManager(1)
    original = await turns.notify_message("private")
    entered, cleaning, release, acquired = [asyncio.Event() for _ in range(4)]
    attempts = []
    physical = 0

    async def transport(_request):
        nonlocal physical
        physical += 1
        attempts.append(current_provider_attempts.get())
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    async with httpx.AsyncClient(
        base_url="https://offline.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=2,
            client=client,
        )
        models = executor(provider, None)
        models._max_concurrency = 1

        async def old():
            async with turns.track(original, "generation"), concurrency.conversation("private"):
                await concurrency.run_llm(
                    "private",
                    lambda: models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=())),
                )

        task = asyncio.create_task(old())
        await entered.wait()
        notification = asyncio.create_task(turns.notify_message("private", preempt_private=True))
        await cleaning.wait()

        async def next_owner():
            async with concurrency.conversation("private"):
                acquired.set()

        waiter = asyncio.create_task(next_owner())
        await flush()
        assert not acquired.is_set() and not notification.done()
        assert models._provider_active == 1 and concurrency.is_processing("private")
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await notification
        await waiter
        assert models._provider_active == 0 and not concurrency.is_processing("private")
        assert physical == 1 and attempts[0].unknown_usage_requests == 1
        await models.close()


async def test_processor_new_private_message_interrupts_original_provider(database, monkeypatch):
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    entered, cleaning, release = [asyncio.Event() for _ in range(3)]
    calls = 0
    original_complete = provider.complete

    async def complete(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise
        return await original_complete(request)

    monkeypatch.setattr(provider, "complete", complete)
    old_sender, new_sender = MemorySender(), MemorySender()
    old = asyncio.create_task(
        harness.processor.handle(inbound("第一问", message_id="first"), old_sender)
    )
    await asyncio.wait_for(entered.wait(), 3)
    new = asyncio.create_task(
        harness.processor.handle(inbound("补充", message_id="next"), new_sender)
    )
    await asyncio.wait_for(cleaning.wait(), 3)
    await flush()
    assert not new.done() and calls == 1
    release.set()
    first, second = await asyncio.wait_for(asyncio.gather(old, new), 3)
    assert first.reason in {"cancelled", "turn_interrupted"} and first.sent_messages == 0
    assert second.reason == "chat" and calls == 2
    assert not old_sender.messages and not new_sender.messages
    key_message = inbound("", message_id="key")
    assert not harness.processor._turn_coordinator._states[
        runtime_conversation_key(identity=key_message.scope(), inbound=key_message)
    ].registrations


async def test_processor_preparation_is_registered_before_first_model(database, monkeypatch):
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    entered, cancelled = asyncio.Event(), asyncio.Event()
    builds = 0
    original_build = chat._build_messages

    async def build(*args, **kwargs):
        nonlocal builds
        builds += 1
        if builds == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await original_build(*args, **kwargs)

    monkeypatch.setattr(chat, "_build_messages", build)
    old = asyncio.create_task(
        harness.processor.handle(inbound("first", message_id="preparing"), MemorySender())
    )
    await asyncio.wait_for(entered.wait(), 3)
    assert not provider.requests
    next_result = await harness.processor.handle(
        inbound("next", message_id="prepared-next"), MemorySender()
    )
    old_result = await old
    assert cancelled.is_set() and old_result.reason == "turn_interrupted"
    assert next_result.reason == "chat" and builds == 2 and len(provider.requests) == 1


@pytest.mark.parametrize("second_part", [False, True])
async def test_repeated_private_inputs_do_not_cancel_actual_social_sequence(
    database, tmp_path, monkeypatch, second_part
):
    env = await social_env(database, tmp_path)
    incoming = await env.service.writer.append(
        scope=ConversationScope.private("80001", "10001"),
        platform_message_id="original",
        sender_user_id="10001",
        direction="inbound",
        content="synthetic",
    )
    context = replace(
        env.context,
        turn_id="private",
        call_id="send",
        conversation_id=incoming.event.canonical_conversation_id,
        space_id=None,
        person_refs={"current_speaker": env.person},
        account_refs={"current_speaker": "10001"},
        trigger_event_id=incoming.event.id,
        reply_presence_id=env.presence,
        runtime_snapshot=SimpleNamespace(
            reply=SimpleNamespace(delay_min_seconds=0, delay_max_seconds=0)
        ),
    )
    turns = ConversationTurnCoordinator()
    token = await turns.notify_message("private")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    gateway = env.bot.call_api

    async def held_gateway(action, **params):
        nonlocal calls
        if action == "send_private_msg":
            calls += 1
            if calls == (2 if second_part else 1):
                entered.set()
                await release.wait()
        return await gateway(action, **params)

    monkeypatch.setattr(env.bot, "call_api", held_gateway)
    monkeypatch.setattr(
        OutboundMessageSplitter,
        "render",
        lambda *_, **__: ("first", "second") if second_part else ("one",),
    )

    async def send():
        async with turns.track(token, "generation"):
            await turns.mark_mutation_started(token)
            return await env.service.execute("send_message", {"text": "synthetic"}, context)

    task = asyncio.create_task(send())
    await asyncio.wait_for(entered.wait(), 3)
    await turns.notify_message("private", preempt_private=True)
    await turns.notify_message("private", preempt_private=True)
    assert not task.done()
    release.set()
    result = await task
    assert result["status"] == "succeeded"
    prior = calls
    replay = await env.service.execute("send_message", {"text": "synthetic"}, context)
    assert replay["status"] == "succeeded" and calls == prior == (2 if second_part else 1)


async def test_actual_native_runner_dispatch_is_protected_from_two_private_inputs(
    database, monkeypatch
):
    turns, concurrency = ConversationTurnCoordinator(), ConcurrencyManager(1)
    token = await turns.notify_message("private")
    entered = asyncio.Event()
    physical = 0

    async def transport(_request):
        nonlocal physical
        physical += 1
        # The native hook must precede physical HTTP, not observe its result.
        assert any(item.effect_started for item in turns._states["private"].registrations.values())
        entered.set()
        await asyncio.Event().wait()

    chat = build_harness(database, make_settings(database.url)).processor._chat
    monkeypatch.setattr(chat, "_turn_coordinator", turns)
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=4,
        max_model_requests=4,
        fixed_tools=(),
    )
    backend = MainAgentBackend(
        chat,
        ToolRuntime(
            inbound=inbound("synthetic", message_id="native"),
            gateway=None,
            allow_generic_onebot=False,
            turn_token=token,
        ),
    )
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAIResponsesProvider(
            base_url="https://offline.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=2,
            client=client,
        )
        models = executor(provider, None, ModelProtocol.RESPONSES)
        catalog = models._router.catalog
        profile = catalog.profiles["test"].model_copy(
            update={"provider": "openai", "capabilities": frozenset(ModelCapability)}
        )
        models.apply_catalog(
            catalog.model_copy(update={"profiles": {"test": profile}}), models._pool
        )
        runner = AgentRunner(models, concurrency)
        monkeypatch.setattr(
            runner,
            "prepare_request_tools",
            lambda *_, **__: ((), (NativeToolDefinition(NativeToolType.WEB_SEARCH),)),
        )

        async def old():
            async with turns.track(token, "generation"), concurrency.conversation("private"):
                return await runner.run((), runtime, backend)

        task = asyncio.create_task(old())
        awaiting_entry = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait(
                (task, awaiting_entry), timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            if task.done():
                await task
            assert entered.is_set()
            await turns.notify_message("private", preempt_private=True)
            await turns.notify_message("private", preempt_private=True)
            await flush()
            assert not task.done() and physical == 1 and models._provider_active == 1
        finally:
            # Explicit test cleanup still cancels native; it must not retry.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            awaiting_entry.cancel()
            await asyncio.gather(awaiting_entry, return_exceptions=True)
            await models.close()
