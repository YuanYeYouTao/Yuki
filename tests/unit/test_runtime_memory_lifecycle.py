"""Regression checks for long-lived owners retaining completed conversation work."""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any

import pytest

from qq_ai_bot.adapters.onebot.sender import OneBotSender
from qq_ai_bot.admin.models import ConversationRuntimeConfig
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.mcp.connection import SDKMCPConnection
from qq_ai_bot.mcp.models import MCPServerConfig
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.effect_gate import ConversationEffectGate, EffectGateTimeoutError
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter


class Payload:
    def __init__(self) -> None:
        self.content = bytearray(256 * 1024)


def observation(number: int) -> tuple[InboundMessage, UserProfileSnapshot, OneBotSender]:
    message = InboundMessage(
        message_id=str(number),
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=str(number)),
        text=f"message {number}",
        bot_user_id="bot",
        group_id="group",
    )
    profile = UserProfileSnapshot(user_id=str(number), scope_type=ScopeType.GROUP)
    # Exercise the real sender's event ownership without connecting or sending.
    sender = OneBotSender(bot=SimpleNamespace(), event=Payload())  # type: ignore[arg-type]
    return message, profile, sender


def runtime(*, enabled: bool = True) -> Any:
    policy = ConversationRuntimeConfig(enabled, 0.001, 50, 100, 120, True)
    return SimpleNamespace(conversation_policy=lambda: policy)


def autonomous(config: Any) -> AutonomousGroupService:
    chat = SimpleNamespace(
        _runtime_config=config,
        _turn_coordinator=ConversationTurnCoordinator(),
    )
    return AutonomousGroupService(chat=chat, admission_features=SimpleNamespace())  # type: ignore[arg-type]


async def flush_callbacks() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


async def test_autonomous_replaces_old_event_and_releases_finished_batch(monkeypatch: Any) -> None:
    async def snapshot(**kwargs: Any) -> Any:
        return runtime()

    service = autonomous(SimpleNamespace(snapshot=snapshot))
    admitted: list[int] = []

    async def run_latest(key: str, revision: int, config: Any) -> None:
        admitted.append(revision)

    monkeypatch.setattr(service, "_run_latest", run_latest)
    refs = []
    try:
        for number in range(100):
            message, profile, sender = observation(number)
            refs.append(weakref.ref(sender))
            service.observe(message, profile, sender)
        del message, profile, sender
        gc.collect()
        assert sum(ref() is not None for ref in refs) == 1
        # Use the actual runtime scope key, independent of its wire spelling.
        await asyncio.wait_for(
            asyncio.gather(*(service.wait_until_idle(key) for key in tuple(service._states))), 1
        )
        gc.collect()
        assert admitted == [100]
        assert all(ref() is None for ref in refs)
        assert not service._states
    finally:
        await service.close()


async def test_autonomous_disabled_or_failed_snapshot_does_not_spin() -> None:
    for failing in [False, True]:
        await _check_autonomous_disabled_or_failed_snapshot_does_not_spin(failing)


async def _check_autonomous_disabled_or_failed_snapshot_does_not_spin(failing: bool) -> None:
    calls = 0

    async def snapshot(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if failing:
            raise RuntimeError("offline config failure")
        return runtime(enabled=False)

    service = autonomous(SimpleNamespace(snapshot=snapshot))
    try:
        service.observe(*observation(1))
        await flush_callbacks()
        assert calls == 1
        assert not service._states
    finally:
        await service.close()


async def test_autonomous_update_during_snapshot_is_not_lost(monkeypatch: Any) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def snapshot(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return runtime(enabled=False)
        return runtime()

    service = autonomous(SimpleNamespace(snapshot=snapshot))
    admitted = []

    async def run_latest(key: str, revision: int, config: Any) -> None:
        admitted.append(revision)

    monkeypatch.setattr(service, "_run_latest", run_latest)
    try:
        service.observe(*observation(1))
        key = next(iter(service._states))
        await asyncio.wait_for(entered.wait(), 1)
        service.observe(*observation(2))
        release.set()
        await asyncio.wait_for(service.wait_until_idle(key), 1)
        assert admitted == [2]
        assert calls == 2
        assert not service._states
    finally:
        await service.close()


async def test_autonomous_callback_handoff_and_close_release_events(monkeypatch: Any) -> None:
    async def snapshot(**kwargs: Any) -> Any:
        return runtime()

    service = autonomous(SimpleNamespace(snapshot=snapshot))
    original = service._after_silence
    admitted = []

    async def run_latest(key: str, revision: int, config: Any) -> None:
        admitted.append(revision)

    async def after_silence(key: str) -> None:
        await original(key)
        if len(admitted) == 1:
            service.observe(*observation(2))

    monkeypatch.setattr(service, "_run_latest", run_latest)
    monkeypatch.setattr(service, "_after_silence", after_silence)
    try:
        service.observe(*observation(1))
        key = next(iter(service._states))
        await asyncio.wait_for(service.wait_until_idle(key), 1)
        assert admitted == [1, 2]
        assert not service._states
        service.observe(*observation(3))
    finally:
        await service.close()
    assert not service._states
    service.observe(*observation(4))
    assert not service._states


@pytest.mark.parametrize("failing", [False, True])
async def test_idle_mcp_connection_does_not_retain_last_payload(
    monkeypatch: Any, failing: bool
) -> None:
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def initialize(self) -> Any:
            return SimpleNamespace()

        async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
            if failing:
                raise ValueError("offline failure")
            return arguments["payload"]

    async def open_transport(*args: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr("qq_ai_bot.mcp.connection.ClientSession", lambda *a, **kw: Session())
    connection = SDKMCPConnection(
        MCPServerConfig(url="https://offline.invalid/mcp"),
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
    )
    monkeypatch.setattr(connection, "_open_transport", open_transport)
    await connection.connect()
    try:
        for _ in range(3):
            payload = Payload()
            ref = weakref.ref(payload)
            if failing:
                with pytest.raises(ValueError, match="offline failure"):
                    await connection.call_tool("echo", {"payload": payload})
            else:
                result = await connection.call_tool("echo", {"payload": payload})
                assert result is payload
                del result
            del payload
            await flush_callbacks()
            gc.collect()
            assert ref() is None
            assert connection.connected
    finally:
        await connection.close()


@pytest.fixture(params=["conversation", "effect"])
def lock_owner(request: Any) -> tuple[Any, Callable[[str], AbstractAsyncContextManager[None]]]:
    if request.param == "conversation":
        owner = ConcurrencyManager(2)
        return owner, owner.conversation
    gate = ConversationEffectGate()
    return gate, lambda key: gate.hold(key, timeout_seconds=1)


async def test_idle_conversation_locks_are_released(lock_owner: Any) -> None:
    owner, hold = lock_owner
    for index in range(1000):
        async with hold(str(index)):
            pass
    gc.collect()
    assert not owner._locks


async def test_lock_waiters_keep_same_lock_after_cancellation(lock_owner: Any) -> None:
    owner, hold = lock_owner
    entered = []

    async def waiter(number: int) -> None:
        async with hold("same"):
            entered.append(number)
            await asyncio.sleep(0)
            assert len(entered) == 1
            entered.pop()

    async with hold("same"):
        cancelled = asyncio.create_task(waiter(1))
        queued = asyncio.create_task(waiter(2))
        await flush_callbacks()
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        assert not queued.done()
        assert len(owner._locks) == 1
    newest = asyncio.create_task(waiter(3))
    await asyncio.gather(queued, newest)
    # A caller-held cancelled Task itself owns its exception traceback.
    del cancelled, queued, newest
    await flush_callbacks()
    gc.collect()
    assert not owner._locks


async def test_effect_timeout_does_not_orphan_holder_or_waiter() -> None:
    gate = ConversationEffectGate()
    async with gate.hold("same", timeout_seconds=1):
        with pytest.raises(EffectGateTimeoutError):
            async with gate.hold("same", timeout_seconds=0.001):
                pytest.fail("must remain locked")
        assert len(gate._locks) == 1
    await flush_callbacks()
    gc.collect()
    assert not gate._locks


@pytest.fixture(params=["chat", "vision"])
def limiter(request: Any) -> tuple[Any, Any, list[float]]:
    now = [0.0]
    if request.param == "chat":
        instance = SlidingWindowRateLimiter(per_user=1, per_group=1, clock=lambda: now[0])

        async def allow(user: str, group: str | None = None) -> bool:
            return (await instance.check(user_id=user, group_id=group, category="chat")).allowed

        return instance._buckets, allow, now
    vision = VisionRateLimiter(clock=lambda: now[0])

    async def allow_vision(user: str, group: str | None = None) -> bool:
        return await vision.allow(
            user_id=user, group_id=group, per_user_per_minute=1, per_group_per_minute=1
        )

    return vision._user_windows, allow_vision, now


async def test_rate_limiter_sweeps_expired_identities_without_resetting_active(
    limiter: Any,
) -> None:
    buckets, allow, now = limiter
    for number in range(1000):
        assert await allow(str(number))
    now[0] = 59
    assert await allow("recent")
    now[0] = 61
    assert not await allow("recent")
    assert len(buckets) == 1
    now[0] = 120
    assert await allow("recent")


async def test_group_denials_do_not_retain_empty_user_buckets(limiter: Any) -> None:
    buckets, allow, _ = limiter
    assert await allow("first", "group")
    initial = len(buckets)
    for number in range(1000):
        assert not await allow(str(number), "group")
    assert len(buckets) == initial
