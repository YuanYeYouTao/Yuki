"""Codex connector 0.9.0 against an app-server double (task book §4.3, §4.4, §4.11, §15 E1/E2/E6).

Failure cases written before the implementation:
- F12: Yuki's input uses ``turn/start`` (merging into the user's running turn) instead
  of ``thread/queue/add``; a request of a turn the user's TUI started is answerable;
- F1: an observe watch receives an answerable request;
- F9: a scoped turn runs without the thread settings narrowed first, the narrowed
  settings are left on the thread after Yuki's turn (leaking into user turns), an
  approval outside the scope is answered, or a scope is accepted when the thread
  settings cannot be set and restored;
- F13/G9: release leaves Yuki's subscription on the thread (or touches the daemon);
- §4.16: a read-only view hides queued inputs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.codex import CodexDriver

ORIGINAL = {
    "approvalPolicy": "never",
    "approvalsReviewer": "auto_review",
    "sandbox": {"type": "dangerFullAccess"},
}


class AppServer:
    """One thread owned by the user's daemon; turns are started by queue items."""

    def __init__(
        self,
        *,
        settings_supported: bool = True,
        profile: str | None = None,
        cwd: str = "/user/project",
    ) -> None:
        self.cwd = cwd
        # 1-based thread/settings/update calls the owner rejects.
        self.settings_failures: set[int] = set()
        self.calls: list[dict[str, Any]] = []
        self.answers: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.socket: Any = None
        self.active: str | None = None
        self.turns = 0
        self.queue: list[dict[str, Any]] = []
        self.settings_supported = settings_supported
        self.profile = profile

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls if "method" in call and "id" in call]

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.socket.send(json.dumps({"method": method, "params": params}))

    async def start_turn(self, client_id: str | None) -> str:
        self.turns += 1
        turn = f"turn-{self.turns}"
        self.active = turn
        await self.notify(
            "turn/started",
            {"threadId": "thread", "turn": {"id": turn, "status": "inProgress"}},
        )
        item = {"id": f"user-{turn}", "type": "userMessage", "content": []}
        if client_id:
            item["clientId"] = client_id
        await self.notify("item/started", {"threadId": "thread", "turnId": turn, "item": item})
        return turn

    async def complete(self, turn: str) -> None:
        self.active = None
        await self.notify(
            "turn/completed",
            {"threadId": "thread", "turn": {"id": turn, "status": "completed", "items": []}},
        )

    async def request(self, identity: int, method: str, turn: str, **params: Any) -> None:
        await self.socket.send(
            json.dumps(
                {
                    "id": identity,
                    "method": method,
                    "params": {"threadId": "thread", "turnId": turn, **params},
                }
            )
        )

    async def handler(self, socket: Any) -> None:
        self.socket = socket
        async for raw in socket:
            value = json.loads(raw)
            self.calls.append(value)
            if "method" not in value:
                self.answers.put_nowait(value)
                continue
            if "id" not in value:
                continue
            method, params = value["method"], value.get("params", {})
            thread = {
                "id": "thread",
                "cwd": self.cwd,
                "status": {"type": "active" if self.active else "idle"},
            }
            result: Any = {}
            if method == "initialize":
                result = {"userAgent": "fake-codex-0.169.0"}
            elif method == "thread/loaded/list":
                result = {"data": ["thread"]}
            elif method in {"thread/resume", "thread/start"}:
                result = {"thread": thread, "cwd": self.cwd, **ORIGINAL}
                if self.profile:
                    result["activePermissionProfile"] = {"id": self.profile}
            elif method == "thread/read":
                result = {"thread": thread}
            elif method == "thread/turns/list":
                result = {"data": []}
            elif method == "thread/settings/update":
                if self.methods().count(method) in self.settings_failures:
                    await socket.send(
                        json.dumps(
                            {
                                "id": value["id"],
                                "error": {"code": -32600, "message": "settings rejected"},
                            }
                        )
                    )
                    continue
                if not self.settings_supported:
                    await socket.send(
                        json.dumps(
                            {
                                "id": value["id"],
                                "error": {"code": -32601, "message": "method not found"},
                            }
                        )
                    )
                    continue
            elif method == "thread/queue/add":
                queued = {
                    "id": f"queued-{len(self.queue) + 1}",
                    "clientUserMessageId": params["clientUserMessageId"],
                }
                await socket.send(
                    json.dumps({"id": value["id"], "result": {"queuedSubmission": queued}})
                )
                if self.active is None:
                    await self.start_turn(params["clientUserMessageId"])
                else:
                    self.queue.append(queued)
                continue
            elif method == "thread/queue/list":
                result = {"data": self.queue}
            elif method == "turn/start":
                result = {"turn": {"id": self.active or "merged", "status": "inProgress"}}
            await socket.send(json.dumps({"id": value["id"], "result": result}))


async def connected(
    owner: AppServer, listener: Any, tmp_path: Path
) -> tuple[CodexDriver, asyncio.Queue[tuple[str, dict[str, Any]]]]:
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.put_nowait((kind, value))

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
    return driver, events


def command(
    action: str, identity: str, *, execution: str | None = None, **arguments: Any
) -> dict[str, Any]:
    return {
        "id": identity,
        "action": action,
        "session_id": "thread",
        "execution_id": execution,
        "arguments": arguments,
    }


async def until(
    events: asyncio.Queue[tuple[str, dict[str, Any]]], predicate: Any
) -> tuple[str, dict[str, Any]]:
    while True:
        kind, value = await asyncio.wait_for(events.get(), 3)
        if predicate(kind, value):
            return kind, value


async def settled(owner: AppServer, count: int) -> None:
    async with asyncio.timeout(3):
        while owner.methods().count("thread/settings/update") < count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_codex_declares_scope_observe_and_release(tmp_path: Path) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await connected(owner, listener, tmp_path)
        try:
            assert {"scope", "observe", "release"} <= set(driver.capabilities)
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_yuki_input_is_a_queue_item_attributed_by_client_id_never_turn_start(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            submitted = await driver.execute(command("prompt", "yuki-prompt", prompt="fix"))
            assert submitted["session_id"] == "thread" and submitted["admitted"] is True
            assert "turn/start" not in owner.methods()
            add = next(c for c in owner.calls if c.get("method") == "thread/queue/add")
            assert add["params"]["clientUserMessageId"] == "yuki-prompt"
            _kind, started = await until(events, lambda kind, value: value.get("execution_id"))
            assert started["operation_id"] == "yuki-prompt" and started["execution_id"] == "turn-1"
            # The user's TUI starts its own turn later: it never becomes Yuki's.
            await owner.complete("turn-1")
            await until(events, lambda kind, value: kind == "completed")
            await owner.start_turn(None)
            await owner.request(41, "item/commandExecution/requestApproval", "turn-2", command="ls")
            await asyncio.sleep(0.1)
            assert events.empty()
            assert "turn-2" not in driver.turn_operations
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_requests_of_user_tui_turns_reach_yuki_only_as_terminal_metadata(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(
                {**command("watch", "session-watch"), "observation_scope": "session"}
            )
            await owner.start_turn(None)  # The user's own TUI turn.
            await owner.request(
                7, "item/commandExecution/requestApproval", "turn-1", command="rm -rf x"
            )
            _kind, seen = await until(events, lambda kind, value: kind == "permission")
            payload = seen["payload"]
            assert payload["answer_at"] == "native_terminal"
            assert payload["native_request_id"] == 7
            assert not {"native_request", "permission_id", "connection_id"} & set(payload)
            assert "rm -rf" not in json.dumps(seen)
            with pytest.raises(DriverError) as refused:
                await driver.execute(
                    command(
                        "respond",
                        "answer",
                        execution="turn-1",
                        operation_id="session-watch",
                        permission_id="7",
                        connection_id=driver.rpc.connection_id,
                        response={"decision": "decline"},
                    )
                )
            assert refused.value.code == "not_owner"
            await asyncio.sleep(0.05)
            assert owner.answers.empty()  # The approval stays pending for the user.
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_yuki_turn_request_is_answerable_and_observe_watch_sees_metadata_only(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(
                {
                    **command("watch", "observer", observation="observe"),
                    "observation_scope": "session",
                }
            )
            await driver.execute(command("prompt", "yuki-prompt", prompt="fix"))
            await until(events, lambda kind, value: value["operation_id"] == "yuki-prompt")
            await owner.request(
                9, "item/commandExecution/requestApproval", "turn-1", command="make"
            )
            seen: dict[str, dict[str, Any]] = {}
            while len(seen) < 2:
                _kind, value = await until(events, lambda kind, value: kind == "permission")
                seen[value["operation_id"]] = value["payload"]
            assert "native_request" in seen["yuki-prompt"]
            observed = seen["observer"]
            assert observed["observed"] is True and observed["native_request_id"] == 9
            assert not {"native_request", "permission_id", "connection_id"} & set(observed)
            await driver.execute(
                command(
                    "respond",
                    "answer",
                    execution="turn-1",
                    operation_id="yuki-prompt",
                    permission_id="9",
                    connection_id=driver.rpc.connection_id,
                    response={"decision": "accept"},
                )
            )
            assert (await asyncio.wait_for(owner.answers.get(), 3))["result"] == {
                "decision": "accept"
            }
        finally:
            await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, ":workspace"])
async def test_scope_narrows_thread_settings_for_yuki_turn_and_restores_them(
    tmp_path: Path, profile: str | None
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    # The thread's own cwd lies inside the scope: workspaceWrite always includes it.
    owner = AppServer(profile=profile, cwd=str(work))
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(
                command(
                    "prompt",
                    "scoped",
                    prompt="fix",
                    scope={"write_paths": [str(work)], "commands": ["pytest"]},
                )
            )
            methods = owner.methods()
            assert methods.index("thread/settings/update") < methods.index("thread/queue/add")
            narrowed = next(c for c in owner.calls if c.get("method") == "thread/settings/update")
            assert narrowed["params"] == {
                "threadId": "thread",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(work)],
                    "networkAccess": False,
                    "excludeSlashTmp": True,
                    "excludeTmpdirEnvVar": True,
                },
            }
            await until(events, lambda kind, value: value["operation_id"] == "scoped")
            # Inside the scope: the connector approves Yuki's own turn.
            await owner.request(
                1, "item/commandExecution/requestApproval", "turn-1", command="pytest -q"
            )
            assert await asyncio.wait_for(owner.answers.get(), 3) == {
                "id": 1,
                "result": {"decision": "accept"},
            }
            await owner.notify(
                "item/started",
                {
                    "threadId": "thread",
                    "turnId": "turn-1",
                    "item": {
                        "id": "patch-in",
                        "type": "fileChange",
                        "changes": [{"path": str(work / "a.py"), "kind": "add"}],
                    },
                },
            )
            await owner.request(2, "item/fileChange/requestApproval", "turn-1", itemId="patch-in")
            assert (await asyncio.wait_for(owner.answers.get(), 3))["result"] == {
                "decision": "accept"
            }
            # Outside the scope: reported, never answered by the connector.
            await owner.notify(
                "item/started",
                {
                    "threadId": "thread",
                    "turnId": "turn-1",
                    "item": {
                        "id": "patch-out",
                        "type": "fileChange",
                        "changes": [{"path": str(tmp_path / "b.py"), "kind": "add"}],
                    },
                },
            )
            for identity, method, extra in [
                (3, "item/commandExecution/requestApproval", {"command": "rm -rf ~"}),
                (4, "item/commandExecution/requestApproval", {"command": "pytest && curl x"}),
                (5, "item/fileChange/requestApproval", {"itemId": "patch-out"}),
                (6, "item/permissions/requestApproval", {"permissions": {}}),
            ]:
                await owner.request(identity, method, "turn-1", **extra)
                _kind, reported = await until(events, lambda kind, value: kind == "permission")
                assert reported["payload"]["native_request_id"] == identity
            assert owner.answers.empty()
            await owner.complete("turn-1")
            await settled(owner, 2)
            restored = [c for c in owner.calls if c.get("method") == "thread/settings/update"][-1]
            expected: dict[str, Any] = {
                "threadId": "thread",
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
            }
            if profile:
                expected["permissions"] = profile
            else:
                expected["sandboxPolicy"] = {"type": "dangerFullAccess"}
            assert restored["params"] == expected
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_scope_is_refused_when_thread_settings_cannot_be_set(tmp_path: Path) -> None:
    owner = AppServer(settings_supported=False)
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await connected(owner, listener, tmp_path)
        try:
            with pytest.raises(DriverError) as refused:
                await driver.execute(
                    command("prompt", "scoped", prompt="fix", scope={"network": False})
                )
            assert refused.value.code == "scope_unsupported"
            assert "thread/queue/add" not in owner.methods()
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_release_unsubscribes_only_yuki_client_at_the_turn_boundary(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(command("prompt", "yuki-prompt", prompt="fix"))
            await until(events, lambda kind, value: value["operation_id"] == "yuki-prompt")
            released = await driver.execute(
                command("detach", "release", operation_id="yuki-prompt", release=True)
            )
            assert released["detached"] is True and released["closed"] == "after_turn"
            assert "thread/unsubscribe" not in owner.methods()
            await owner.complete("turn-1")
            async with asyncio.timeout(3):
                while "thread/unsubscribe" not in owner.methods():  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            unsubscribe = next(c for c in owner.calls if c.get("method") == "thread/unsubscribe")
            assert unsubscribe["params"] == {"threadId": "thread"}
            await driver.execute(command("prompt", "second", prompt="more"))
            await until(events, lambda kind, value: value["operation_id"] == "second")
            await owner.complete("turn-2")
            await until(events, lambda kind, value: kind == "completed")
            before = owner.methods().count("thread/unsubscribe")
            closed = await driver.execute(
                command("detach", "release-idle", operation_id="second", release=True)
            )
            assert closed["closed"] == "now"
            assert owner.methods().count("thread/unsubscribe") == before + 1
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_session_view_lists_queued_inputs(tmp_path: Path) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(command("prompt", "first", prompt="fix"))
            await until(events, lambda kind, value: value["operation_id"] == "first")
            await driver.execute(command("queue", "later", prompt="then this"))
            view = await driver.execute(command("get", "read"))
            assert view["queued_inputs"] == [
                {"queued_submission_id": "queued-1", "operation_id": "later"}
            ]
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_yuki_prompt_queues_behind_a_running_user_turn(tmp_path: Path) -> None:
    """Acceptance P2: Yuki's new input waits behind the user's running turn (§4.11)."""
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _events = await connected(owner, listener, tmp_path)
        try:
            await owner.start_turn(None)
            submitted = await driver.execute(command("prompt", "after-user", prompt="next"))
            assert submitted["admitted"] is True
            add = next(c for c in owner.calls if c.get("method") == "thread/queue/add")
            assert add["params"]["clientUserMessageId"] == "after-user"
            assert "turn/start" not in owner.methods()
        finally:
            await driver.stop()
