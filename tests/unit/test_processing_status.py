"""Typing follows Main lifetime without becoming an execution or delivery gate."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.canonical_identity import ensure_presence
from tests.support.commands_and_chat_helpers import inbound
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import Bot, social_env

from yuki.domain.conversations import ConversationScope
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.processing_status import ProcessingStatusService


class Activity:
    refresh_interval_seconds = 0.02

    def __init__(self, *, fail_on=0, block_start=False, block_stop=False):
        self.starts = 0
        self.stops = 0
        self.fail_on = fail_on
        self.block_start = block_start
        self.block_stop = block_stop
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self):
        self.starts += 1
        self.started.set()
        if self.starts == self.fail_on:
            raise RuntimeError("private transport credentials must not be logged")
        if self.block_start:
            await asyncio.Event().wait()

    async def stop(self):
        self.stops += 1
        self.stopped.set()
        if self.block_stop:
            await asyncio.Event().wait()


def service_for(activity):
    route = SimpleNamespace(connection=object(), external_target_id="external-chat", thread_id="17")
    router = SimpleNamespace(resolve_send_for_conversation=AsyncMock(return_value=route))
    registry = SimpleNamespace(activity=Mock(return_value=activity))
    registry.resolve_active = Mock(
        return_value=SimpleNamespace(
            snapshot=SimpleNamespace(capabilities=frozenset({"typing_private", "typing_group"}))
        )
    )
    return ProcessingStatusService(router, registry), router, registry, route


def during(service, **changes):
    return service.during(
        **{
            "conversation_id": "internal-conversation",
            "presence_id": "original-presence",
            "source_event_id": 71,
            "group": False,
            **changes,
        }
    )


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_actual_main_starts_typing_while_model_is_pending_and_stops_on_exit(
    database, outcome
):
    class PausedModel(FakeLLMProvider):
        async def complete(self, request):
            entered.set()
            await release.wait()
            if outcome == "error":
                raise RuntimeError("synthetic model failure")
            return await super().complete(request)

    entered, release = asyncio.Event(), asyncio.Event()
    activity = Activity()
    activity.refresh_interval_seconds = 10
    service, router, registry, route = service_for(activity)
    provider = PausedModel(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat.runtime.main_turns._processing_status = service
    sender = MemorySender()
    task = asyncio.create_task(
        harness.processor.handle(inbound("真实请求", message_id="typing-main"), sender)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(activity.started.wait(), 5)
        assert not task.done() and activity.stops == 0
        async with database.read_session() as reader:
            event = (await reader.scalars(select(ChatEventModel))).one()
        router.resolve_send_for_conversation.assert_awaited_once_with(
            event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
            source_event_id=event.id,
        )
        target = registry.activity.call_args.args[1]
        assert registry.activity.call_args.args[0] is route.connection
        assert (target.kind, target.external_id, target.thread_id) == (
            "person",
            "external-chat",
            "17",
        )
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert task.cancelled()
        else:
            release.set()
            result = await asyncio.wait_for(task, 5)
            assert result.reason == {"success": "chat", "error": "send_or_storage_failure"}[outcome]
        await service.close()
        assert activity.stops == 1 and sender.calls == 0
        assert not service._active and not service._tasks
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


async def test_route_and_start_network_never_delay_main_entry():
    activity = Activity(block_start=True)
    service, router, _, _ = service_for(activity)
    resolving, release = asyncio.Event(), asyncio.Event()
    original = router.resolve_send_for_conversation.return_value

    async def resolve(*args, **kwargs):
        resolving.set()
        await release.wait()
        return original

    router.resolve_send_for_conversation.side_effect = resolve
    async with during(service):
        await asyncio.wait_for(resolving.wait(), 1)
        assert not activity.started.is_set()
        release.set()
        await asyncio.wait_for(activity.started.wait(), 1)
    await service.close()
    assert activity.stops == 1 and not service._active


async def test_overlapping_turns_share_status_until_the_last_turn_exits():
    activity = Activity()
    service, router, registry, _ = service_for(activity)
    first = during(service)
    second = during(service, source_event_id=72)
    await first.__aenter__()
    await asyncio.wait_for(activity.started.wait(), 1)
    await second.__aenter__()
    await first.__aexit__(None, None, None)
    assert activity.stops == 0
    router.resolve_send_for_conversation.assert_awaited_once()
    registry.activity.assert_called_once()
    await second.__aexit__(None, None, None)
    await service.close()
    assert activity.stops == 1 and not service._active


@pytest.mark.parametrize("conversation", [None, "internal-conversation"])
async def test_no_activity_keeps_execution_unaffected(conversation):
    service, router, registry, _ = service_for(None)
    async with during(service, conversation_id=conversation):
        await asyncio.sleep(0)
    await service.close()
    assert not service._active
    assert router.resolve_send_for_conversation.await_count == (0 if conversation is None else 1)
    assert registry.activity.call_count == (0 if conversation is None else 1)


@pytest.mark.parametrize("fail_on", [1, 2])
async def test_failed_start_or_refresh_ends_status_without_retry_or_sensitive_logging(
    caplog, fail_on
):
    activity = Activity(fail_on=fail_on)
    service, router, _, _ = service_for(activity)
    async with during(service):
        await asyncio.wait_for(activity.stopped.wait(), 1)
        async with during(service, source_event_id=72):
            await asyncio.sleep(activity.refresh_interval_seconds * 2)
        assert activity.starts == fail_on and activity.stops == 1
        router.resolve_send_for_conversation.assert_awaited_once()
    await service.close()
    assert not service._active
    assert "RuntimeError" in caplog.text and "credentials" not in caplog.text
    assert f"phase={'start' if fail_on == 1 else 'refresh'}" in caplog.text


async def test_failed_route_does_not_try_another_connection_or_gate_execution(caplog):
    service, router, registry, _ = service_for(Activity())
    router.resolve_send_for_conversation.side_effect = RuntimeError("private route details")
    async with during(service):
        await asyncio.sleep(0)
    await service.close()
    router.resolve_send_for_conversation.assert_awaited_once()
    registry.activity.assert_not_called()
    assert "phase=resolve" in caplog.text and "private route" not in caplog.text


@pytest.mark.parametrize("phase", ["start", "stop"])
async def test_start_and_stop_are_bounded_by_the_native_ui_refresh_lifetime(caplog, phase):
    activity = Activity(block_start=phase == "start", block_stop=phase == "stop")
    service, _, _, _ = service_for(activity)

    async def execute():
        async with during(service):
            await asyncio.wait_for(
                (activity.stopped if phase == "start" else activity.started).wait(), 1
            )
        await service.close()

    await asyncio.wait_for(execute(), 1)
    assert activity.starts == activity.stops == 1 and not service._active
    assert "TimeoutError" in caplog.text and f"phase={phase}" in caplog.text


async def test_ui_task_does_not_inherit_business_context():
    identity = ContextVar("original-business-identity", default=None)
    activity = Activity()
    service, router, _, _ = service_for(activity)
    route = router.resolve_send_for_conversation.return_value
    observed = []

    async def resolve(*args, **kwargs):
        observed.append(identity.get())
        return route

    router.resolve_send_for_conversation.side_effect = resolve
    token = identity.set("private-work-authority")
    try:
        async with during(service):
            await asyncio.wait_for(activity.started.wait(), 1)
        await service.close()
    finally:
        identity.reset(token)
    assert observed == [None]


@pytest.mark.parametrize("inflight_phase", ["start", "stop"])
async def test_slow_stop_never_delays_main_completion_or_the_next_model_request(
    database, inflight_phase
):
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    stop_release = asyncio.Event()
    native_order = []

    class PausedModel(FakeLLMProvider):
        calls = 0

        async def complete(self, request):
            number = self.calls
            self.calls += 1
            entered[number].set()
            await release[number].wait()
            return await super().complete(request)

    class SlowStop(Activity):
        async def start(self):
            await super().start()
            if inflight_phase == "start":
                await stop_release.wait()
            native_order.append("original-start-finished")

        async def stop(self):
            self.stops += 1
            self.stopped.set()
            if inflight_phase == "stop":
                await stop_release.wait()
            native_order.append("original-stop-finished")

    class Following(Activity):
        async def start(self):
            await super().start()
            native_order.append("next-start")

    first, second = SlowStop(), Following()
    first.refresh_interval_seconds = second.refresh_interval_seconds = 10
    service, _, registry, _ = service_for(first)
    registry.activity.side_effect = [first, second]
    provider = PausedModel(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat.runtime.main_turns._processing_status = service
    first_turn = asyncio.create_task(
        harness.processor.handle(inbound("第一次请求", message_id="first-main"), MemorySender())
    )
    second_turn = None
    try:
        await asyncio.wait_for(entered[0].wait(), 5)
        await asyncio.wait_for(first.started.wait(), 1)
        release[0].set()
        assert (await asyncio.wait_for(first_turn, 1)).reason == "chat"
        if inflight_phase == "stop":
            await asyncio.wait_for(first.stopped.wait(), 1)
        else:
            assert first.stops == 0
        assert not stop_release.is_set()
        second_turn = asyncio.create_task(
            harness.processor.handle(
                inbound("第二次请求", message_id="second-main"), MemorySender()
            )
        )
        await asyncio.wait_for(entered[1].wait(), 5)
        assert not second.started.is_set() and not stop_release.is_set()
        stop_release.set()
        await asyncio.wait_for(second.started.wait(), 1)
        assert native_order == ["original-start-finished", "original-stop-finished", "next-start"]
        release[1].set()
        assert (await asyncio.wait_for(second_turn, 1)).reason == "chat"
        await service.close()
        assert first.stops == second.stops == 1
        assert len(provider.requests) == 2 and not service._tasks and not service._active
    finally:
        stop_release.set()
        for event in release:
            event.set()
        for task in (first_turn, second_turn):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first_turn, second_turn) if task is not None),
            return_exceptions=True,
        )
        await service.close()


@pytest.mark.parametrize("middle_started", [False, True])
async def test_cancelled_middle_session_keeps_the_original_stop_before_a_third_start(
    middle_started,
):
    events = []
    release = asyncio.Event()

    class Original(Activity):
        async def stop(self):
            self.stops += 1
            events.append("original-stop-started")
            self.stopped.set()
            await release.wait()
            events.append("original-stop-finished")

    class Following(Activity):
        async def start(self):
            await super().start()
            events.append("third-started")

    original, following = Original(), Following()
    original.refresh_interval_seconds = following.refresh_interval_seconds = 10
    service, _, registry, _ = service_for(original)
    registry.activity.side_effect = [original, following]
    first, middle, third = during(service), during(service), during(service)
    await first.__aenter__()
    await asyncio.wait_for(original.started.wait(), 1)
    await first.__aexit__(None, None, None)
    await asyncio.wait_for(original.stopped.wait(), 1)
    await middle.__aenter__()
    if middle_started:
        await asyncio.sleep(0)
    await middle.__aexit__(None, None, None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await third.__aenter__()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(following.started.wait(), 0.05)
        release.set()
        await asyncio.wait_for(following.started.wait(), 1)
        assert events == ["original-stop-started", "original-stop-finished", "third-started"]
    finally:
        release.set()
        await third.__aexit__(None, None, None)
        await service.close()
    assert original.stops == following.stops == 1 and not service._tasks and not service._active


async def test_qq_group_without_typing_support_never_reads_routes_or_probes_membership(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    service = ProcessingStatusService(env.router, env.registry)
    observed = {"sql": [], "native_actions": []}
    env.bot.calls.clear()

    def statement(_connection, _cursor, sql, _parameters, _context, _many):
        observed["sql"].append(sql.split()[0])

    event.listen(database.engine.sync_engine, "before_cursor_execute", statement)
    try:
        async with service.during(
            conversation_id=env.context.conversation_id,
            presence_id=env.presence,
            source_event_id=None,
            group=True,
        ):
            await asyncio.wait_for(asyncio.shield(next(iter(service._tasks))), 1)
        await service.close()
        observed["native_actions"] = [name for name, _params in env.bot.calls]
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", statement)
        await service.close()
    assert observed == {"sql": [], "native_actions": []}


@pytest.mark.parametrize("group", [False, True])
async def test_supported_scope_keeps_the_original_route_and_unsupported_scope_stops_in_memory(
    group,
):
    activity = Activity()
    service, router, registry, _ = service_for(activity)
    registry.resolve_active.return_value.snapshot.capabilities = frozenset({"typing_private"})
    async with during(service, group=group):
        if group:
            await asyncio.shield(next(iter(service._tasks)))
        else:
            await asyncio.wait_for(activity.started.wait(), 1)
    await service.close()
    registry.resolve_active.assert_called_once_with("original-presence")
    if group:
        router.resolve_send_for_conversation.assert_not_awaited()
        registry.activity.assert_not_called()
    else:
        router.resolve_send_for_conversation.assert_awaited_once_with(
            "internal-conversation", presence_id="original-presence", source_event_id=71
        )
        assert activity.starts == activity.stops == 1


async def test_unknown_presence_uses_original_route_instead_of_guessing_capabilities():
    activity = Activity()
    service, router, registry, _ = service_for(activity)
    registry.resolve_active.side_effect = AssertionError("must not guess a current bot")
    async with during(service, presence_id=None):
        await asyncio.wait_for(activity.started.wait(), 1)
    await service.close()
    registry.resolve_active.assert_not_called()
    router.resolve_send_for_conversation.assert_awaited_once_with(
        "internal-conversation", presence_id=None, source_event_id=71
    )
    assert activity.starts == activity.stops == 1


async def test_supported_private_activity_keeps_the_second_bot_entry_and_target(database, tmp_path):
    env = await social_env(database, tmp_path)
    second = Bot("80002")
    async with database.immediate_session() as writer:
        presence_id = await ensure_presence(writer, second.self_id)
    env.registry.connect(second, provider_id="snowluma", presence_id=presence_id)
    event = await seed_scoped_event(
        env.service.writer,
        scope=ConversationScope.private(second.self_id, "10001", presence_id=presence_id),
        platform_message_id="second-private-entry",
        sender_user_id="10001",
        direction="inbound",
        content="原入口请求",
    )
    original = event.event
    started = asyncio.Event()
    native = second.call_api

    async def call(action, **params):
        result = await native(action, **params)
        if action == "set_input_status" and params["event_type"] == 1:
            started.set()
        return result

    second.call_api = call
    env.bot.calls.clear()
    service = ProcessingStatusService(env.router, env.registry)
    try:
        async with service.during(
            conversation_id=original.canonical_conversation_id,
            presence_id=presence_id,
            source_event_id=original.id,
            group=False,
        ):
            await asyncio.wait_for(started.wait(), 1)
        await service.close()
    finally:
        await service.close()
    assert env.bot.calls == []
    assert second.calls == [
        ("set_input_status", {"user_id": 10001, "event_type": 1}),
        ("set_input_status", {"user_id": 10001, "event_type": 0}),
    ]
