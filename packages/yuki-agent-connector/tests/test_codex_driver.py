from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.codex import CodexDriver


class Owner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active = False
        self.socket: Any = None
        self.responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def handler(self, socket: Any) -> None:
        self.socket = socket
        async for raw in socket:
            value = json.loads(raw)
            self.calls.append(value)
            if "method" not in value or "id" not in value:
                if "result" in value:
                    self.responses.put_nowait(value)
                continue
            method, params = value["method"], value.get("params", {})
            result: Any = {}
            thread = {
                "id": "thread",
                "status": {"type": "active" if self.active else "idle"},
                "turns": [{"id": "turn", "status": "inProgress" if self.active else "completed"}],
            }
            if method == "initialize":
                result = {"userAgent": "fake-codex-0.163.0"}
            elif method == "thread/list":
                result = {"data": [thread]}
            elif method == "thread/loaded/list":
                result = {"data": ["thread"]}
            elif method in {"thread/read", "thread/resume", "thread/start"}:
                result = {"thread": thread}
            elif method == "turn/start":
                self.active = True
                await socket.send(
                    json.dumps(
                        {
                            "method": "turn/started",
                            "params": {
                                "threadId": "thread",
                                "turn": {"id": "turn", "status": "inProgress"},
                            },
                        }
                    )
                )
                result = {"turn": {"id": "turn", "status": "inProgress"}}
            elif method == "turn/steer":
                if params["expectedTurnId"] != "turn":
                    await socket.send(
                        json.dumps(
                            {
                                "id": value["id"],
                                "error": {"code": -32000, "message": "active turn mismatch"},
                            }
                        )
                    )
                    continue
                result = {"turnId": "turn"}
            elif method == "thread/queue/add":
                result = {
                    "queuedSubmission": {
                        "id": "queued",
                        "clientUserMessageId": params["clientUserMessageId"],
                    }
                }
            await socket.send(json.dumps({"id": value["id"], "result": result}))


def command(
    action: str, *, execution: str | None = None, identity: str = "operation", **args: Any
) -> dict[str, Any]:
    return {
        "id": identity,
        "action": action,
        "session_id": "thread",
        "execution_id": execution,
        "arguments": args,
    }


@pytest.mark.asyncio
async def test_original_turn_steer_queue_cancel_and_detach_have_distinct_effects(
    tmp_path: Path,
) -> None:
    owner = Owner()
    events: list[dict[str, Any]] = []
    completed = asyncio.Event()

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append({"kind": kind, **value})
        if kind == "completed":
            completed.set()

    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver = CodexDriver(
            Instance(
                "codex",
                "codex",
                "Codex",
                "unused",
                cwd=str(tmp_path),
                endpoint=f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}",
            ),
            emit,
        )
        await driver.start()
        try:
            assert (await driver.execute(command("list")))["sessions"][0]["id"] == "thread"
            assert not any(c.get("method") == "turn/start" for c in owner.calls)
            watching = await driver.execute(command("watch", identity="watch-one"))
            assert watching["pending"] is True and watching["status"] == "unknown"
            assert (await driver.execute(command("prompt", prompt="original")))[
                "execution_id"
            ] == "turn"
            with pytest.raises(DriverError, match="active"):
                await driver.execute(command("prompt", identity="another", prompt="duplicate"))
            await driver.execute(command("steer", execution="turn", prompt="new requirement"))
            before = len(owner.calls)
            with pytest.raises(DriverError, match="mismatch"):
                await driver.execute(command("steer", execution="wrong", prompt="conflict"))
            assert len(owner.calls) == before + 1
            assert owner.calls[-1]["method"] == "turn/steer"
            assert (await driver.execute(command("queue", identity="queue-op", prompt="later")))[
                "queued_submission_id"
            ] == "queued"
            await driver.execute(command("cancel", operation_id="queue-op"))
            assert owner.calls[-1]["method"] == "thread/queue/delete"
            assert events[-1]["operation_id"] == "queue-op"
            assert events[-1]["payload"]["status"] == "cancelled"
            assert "queue-op" not in driver.queue_operations
            cancelled = await driver.execute(command("cancel", execution="turn"))
            assert cancelled["pending"] is True and cancelled["status"] == "running"
            assert owner.calls[-1]["method"] == "turn/interrupt"
            before = len(owner.calls)
            await driver.execute(command("detach", operation_id="watch-one"))
            assert len(owner.calls) == before and "watch-one" not in driver.watches
            completed.clear()
            await owner.socket.send(
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread",
                            "turn": {"id": "turn", "status": "interrupted"},
                        },
                    }
                )
            )
            await asyncio.wait_for(completed.wait(), 2)
            assert events[-1]["operation_id"] == "operation"
            assert events[-1]["payload"]["status"] == "cancelled"
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_native_request_exact_original_and_cannot_answer_twice(tmp_path: Path) -> None:
    owner = Owner()
    events: list[dict[str, Any]] = []
    question = asyncio.Event()

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append({"kind": kind, **value})
        question.set()

    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver = CodexDriver(
            Instance(
                "codex",
                "codex",
                "Codex",
                "unused",
                cwd=str(tmp_path),
                endpoint=f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}",
            ),
            emit,
        )
        await driver.start()
        try:
            await driver.execute(command("watch"))
            await owner.socket.send(
                json.dumps(
                    {
                        "id": 17,
                        "method": "item/tool/requestUserInput",
                        "params": {"threadId": "thread", "turnId": "turn", "questions": []},
                    }
                )
            )
            await asyncio.wait_for(question.wait(), 2)
            assert events[-1]["payload"]["native_request_id"] == 17
            await driver.execute(
                command(
                    "respond",
                    execution="turn",
                    permission_id="17",
                    response={"answers": {"q": {"answers": ["yes"]}}},
                )
            )
            assert await asyncio.wait_for(owner.responses.get(), 2) == {
                "id": 17,
                "result": {"answers": {"q": {"answers": ["yes"]}}},
            }
            with pytest.raises(DriverError, match="resolved"):
                await driver.execute(
                    command("respond", execution="turn", permission_id="17", answers={})
                )
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_unrelated_turns_and_token_deltas_are_not_durable_progress(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append(value)

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)
    driver.turn_operations["old"] = "original-prompt"
    await driver._notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "same",
                "turnId": "new",
                "item": {"type": "agentMessage", "text": "unrelated"},
            },
        }
    )
    await driver._notification(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "same", "turnId": "old", "delta": "token"},
        }
    )
    assert not events


@pytest.mark.asyncio
async def test_concurrent_local_turn_before_start_response_is_not_owned_by_remote(
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append(value)

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in {"thread/read", "thread/resume"}:
            return {"thread": {"id": "thread", "status": {"type": "idle"}}}
        assert method == "turn/start"
        for identity in ["local-race", "original-native"]:
            await driver._notification(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": identity, "status": "inProgress"},
                    },
                }
            )
            await driver._notification(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": identity,
                        "item": {
                            "id": identity + "-item",
                            "type": "agentMessage",
                            "text": identity,
                        },
                    },
                }
            )
        return {"turn": {"id": "original-native", "status": "inProgress"}}

    driver.rpc = SimpleNamespace(call=call)  # type: ignore[assignment]
    started = await driver.execute(command("prompt", prompt="remote intent"))
    assert started["execution_id"] == "original-native"
    assert len(events) == 2
    assert all(event["execution_id"] == "original-native" for event in events)
    assert "local-race" not in driver.turn_operations
    assert not driver.buffered


@pytest.mark.asyncio
async def test_original_watch_ends_at_terminal_and_native_private_history_stays_local(
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append({"kind": kind, **value})

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)
    driver.watches["watch"] = "session"
    driver.watch_turns["watch"] = "original"
    await driver._notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "session",
                "turn": {
                    "id": "original",
                    "status": "completed",
                    "items": [
                        {
                            "type": "reasoning",
                            "content": ["PRIVATE_THINKING"],
                            "encryptedContent": "SIGNATURE",
                        },
                        {"type": "agentMessage", "text": "necessary final result"},
                        {"type": "commandExecution", "aggregatedOutput": "ENTIRE_STDOUT"},
                    ],
                },
            },
        }
    )
    assert events[0]["payload"]["text"] == "necessary final result"
    encoded = json.dumps(events)
    assert (
        "PRIVATE_THINKING" not in encoded
        and "SIGNATURE" not in encoded
        and "ENTIRE_STDOUT" not in encoded
    )
    await driver._notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "session",
                "turn": {"id": "future-local-turn", "status": "inProgress"},
            },
        }
    )
    assert len(events) == 1 and not driver.watches


@pytest.mark.asyncio
async def test_explicit_old_execution_watch_does_not_attach_the_current_different_turn(
    tmp_path: Path,
) -> None:
    async def emit(kind: str, value: dict[str, Any]) -> None:
        raise AssertionError("No native execution is created or event fabricated")

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "thread/loaded/list":
            return {"data": ["thread"]}
        assert method == "thread/resume"
        return {
            "thread": {
                "id": "thread",
                "status": {"type": "active"},
                "turns": [
                    {"id": "A", "status": "completed"},
                    {"id": "B", "status": "inProgress"},
                ],
            }
        }

    driver.rpc = SimpleNamespace(call=call)  # type: ignore[assignment]
    original = await driver.execute(command("watch", execution="A"))
    assert original["execution_id"] == "A" and original["status"] == "completed"
    assert original["pending"] is False
    assert not driver.watches and not driver.watch_turns


@pytest.mark.asyncio
async def test_consumed_queue_cancels_exact_original_turn_even_with_historical_queue_id(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        pass

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        return (
            {"queuedSubmission": {"id": "historical-queue"}} if method == "thread/queue/add" else {}
        )

    driver.rpc = SimpleNamespace(call=call)  # type: ignore[assignment]
    await driver.execute(command("queue", identity="queue-op", prompt="queued"))
    await driver._notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": "actual-native-turn",
                "item": {"type": "userMessage", "clientId": "queue-op", "id": "native-item"},
            },
        }
    )
    assert "queue-op" not in driver.queue_operations
    await driver.execute(
        command(
            "cancel",
            execution="actual-native-turn",
            queued_submission_id="historical-queue",
            operation_id="queue-op",
        )
    )
    assert calls[-1] == ("turn/interrupt", {"threadId": "thread", "turnId": "actual-native-turn"})
    assert all(method != "thread/queue/delete" for method, params in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["prompt", "queue"])
async def test_recover_exact_original_turn_cannot_take_current_other_turn_state(
    action: str, tmp_path: Path
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append((kind, value))

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "thread/read"
        return {
            "thread": {
                "id": "thread",
                "status": {"type": "active"},
                "turns": [
                    {
                        "id": "A",
                        "status": "completed",
                        "items": [{"type": "userMessage", "clientId": "original"}],
                    },
                    {"id": "B", "status": "inProgress", "items": []},
                ],
            }
        }

    driver.rpc = SimpleNamespace(call=call)  # type: ignore[assignment]
    await driver.recover(
        {"id": "original", "action": action},
        {
            "session_id": "thread",
            **({"execution_id": "A"} if action == "prompt" else {"queued_submission_id": "q"}),
        },
    )
    assert len(events) == 1
    assert events[0][0] == "completed" and events[0][1]["execution_id"] == "A"
    assert events[0][1]["payload"]["status"] == "completed"
    assert not driver.turn_operations


@pytest.mark.asyncio
async def test_unbound_watch_recovery_keeps_gap_unknown_without_adopting_current_turn(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.append((kind, value))

    driver = CodexDriver(Instance("c", "codex", "c", "unused", cwd=str(tmp_path)), emit)

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("An unrelated current turn cannot identify the missed original")

    driver.rpc = SimpleNamespace(call=call)  # type: ignore[assignment]
    await driver.recover({"id": "original-watch", "action": "watch"}, {"session_id": "thread"})
    assert events[0][1]["payload"]["status"] == "unknown"
    assert events[0][1]["payload"]["stream_gap"] is True
    assert not driver.watches and not driver.turn_operations
