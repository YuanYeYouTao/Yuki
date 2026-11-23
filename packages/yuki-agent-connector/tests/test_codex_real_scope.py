"""A delegation's scope on a real Codex app-server (#650; task book §16.8, A4).

Until now the narrowing through ``thread/settings/update``, its restore and the
``scope_restore_failed`` report were exercised only against an app-server double.
These opt-in runs drive the real CodexDriver against a real ``codex app-server``
that the test starts itself on loopback, with an explicit, separate CODEX_HOME; the
user's own home, daemon and threads are never touched.

Failure cases written before the runs:
- K1: a scoped Yuki turn writes outside the scope without a reported request (the
  narrowed settings did not take effect, or the outside request was auto-approved),
  or the thread keeps the narrowed settings after Yuki's turn instead of the
  original ones;
- K2: a restore that loses the connection is not reported as ``scope_restore_failed``
  with the original settings, or the next scoped end on that thread restores the
  narrowed settings instead of the original ones;
- K3 (the recorded race, §16.2 A4): a user turn that starts between Yuki's narrowing
  and Yuki's queued input runs with the thread's original (wider) settings while
  Yuki's turn is narrowed, or its request becomes answerable by Yuki instead of
  ``not_owner``; the original settings are not back after Yuki's turn.

Run (each run makes a few short paid model turns):
    YUKI_REAL_CODEX_HOME=<a CODEX_HOME holding only auth.json> \
    YUKI_REAL_CODEX_BINARY=<codex executable> pytest tests/test_codex_real_scope.py
Artifacts go to ``test-results/codex-real-scope/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.codex import CodexDriver

pytestmark = pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CODEX_HOME") or not os.environ.get("YUKI_REAL_CODEX_BINARY"),
    reason="paid real Codex runs; set YUKI_REAL_CODEX_HOME and YUKI_REAL_CODEX_BINARY",
)
ARTIFACT = Path(__file__).resolve().parents[1] / "test-results" / "codex-real-scope"
ORIGINAL = {"approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}
Events = asyncio.Queue[tuple[str, dict[str, Any]]]


@contextlib.asynccontextmanager
async def app_server(project: Path) -> AsyncIterator[str]:
    """The test's own app-server: full access and no approvals, as a user might run."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"ws://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(
        os.environ["YUKI_REAL_CODEX_BINARY"],
        "app-server",
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="danger-full-access"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        f'projects."{project}".trust_level="trusted"',
        "--listen",
        endpoint,
        env={**os.environ, "CODEX_HOME": os.environ["YUKI_REAL_CODEX_HOME"]},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                await asyncio.to_thread(
                    urllib.request.urlopen, f"http://127.0.0.1:{port}/readyz", timeout=1
                )
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.2)
        yield endpoint
    finally:
        process.terminate()
        await process.wait()


class UserClient:
    """A second app-server client standing in for the user's own TUI."""

    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.notes: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def start(self, endpoint: str) -> None:
        self.ws = await connect(endpoint, max_size=None, proxy=None)
        self.reader = asyncio.create_task(self._read())
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "user", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.ws.send(json.dumps({"method": "initialized"}))

    async def _read(self) -> None:
        async for raw in self.ws:
            message = json.loads(raw)
            if "method" in message:
                (self.requests if "id" in message else self.notes).put_nowait(message)
            elif (future := self.pending.pop(str(message["id"]), None)) is not None:
                if "error" in message:
                    future.set_exception(RuntimeError(message["error"]))
                else:
                    future.set_result(message.get("result"))

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        key = str(uuid4())
        self.pending[key] = asyncio.get_running_loop().create_future()
        await self.ws.send(json.dumps({"id": key, "method": method, "params": params}))
        return await self.pending[key]

    async def settings(self, thread: str) -> dict[str, Any]:
        resumed = await self.call("thread/resume", {"threadId": thread, "excludeTurns": True})
        return {"approvalPolicy": resumed["approvalPolicy"], "sandbox": resumed["sandbox"]}

    async def turn(self, thread: str, text: str) -> str:
        started = await self.call(
            "turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}]}
        )
        return str(started["turn"]["id"])

    async def completed(self, thread: str) -> None:
        while True:
            note = await self.notes.get()
            if note["method"] == "turn/completed" and note["params"]["threadId"] == thread:
                return

    async def stop(self) -> None:
        await self.ws.close()
        self.reader.cancel()


async def yuki(endpoint: str, project: Path) -> tuple[CodexDriver, Events, list[Any]]:
    events: Events = asyncio.Queue()
    seen: list[Any] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        seen.append((kind, value))
        events.put_nowait((kind, value))

    driver = CodexDriver(
        Instance("codex", "codex", "Codex", "unused", cwd=str(project), endpoint=endpoint), emit
    )
    await driver.start()
    return driver, events, seen


def scope(project: Path) -> dict[str, Any]:
    return {"write_paths": [str(project)], "commands": ["ls"], "network": False}


async def until_done(driver: CodexDriver, events: Events, operation: str) -> list[dict[str, Any]]:
    """Decline every request of Yuki's turn (the owner's refusal) until it completes."""
    requests: list[dict[str, Any]] = []
    async with asyncio.timeout(300):
        while True:
            kind, value = await events.get()
            if kind == "permission" and value["operation_id"] == operation:
                requests.append(value["payload"]["native_request"])
                await driver.execute(
                    {
                        "id": f"decline-{uuid4()}",
                        "action": "respond",
                        "session_id": value["session_id"],
                        "execution_id": value["execution_id"],
                        "arguments": {
                            "connection_id": value["payload"]["connection_id"],
                            "permission_id": value["payload"]["permission_id"],
                            "option_id": "decline",
                        },
                    }
                )
            if kind == "completed" and value.get("operation_id") == operation:
                return requests


async def restored(user: UserClient, thread: str) -> dict[str, Any]:
    """The thread's settings once Yuki's background restore had its chance."""
    for _ in range(50):
        current = await user.settings(thread)
        if current == ORIGINAL:
            return current
        await asyncio.sleep(0.2)
    return current


def write_artifact(name: str, value: dict[str, Any]) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / name).write_text(json.dumps(value, indent=2, default=str) + "\n")


async def test_k1_scoped_turn_reports_outside_writes_and_restores(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    inside_file, outside_file = project / "inside.txt", outside / "outside.txt"
    async with app_server(project) as endpoint:
        driver, events, _ = await yuki(endpoint, project)
        user = UserClient()
        await user.start(endpoint)
        try:
            prompt = await driver.execute(
                {
                    "id": "k1",
                    "action": "prompt",
                    "arguments": {
                        "cwd": str(project),
                        "scope": scope(project),
                        "prompt": (
                            "Run these two shell commands as separate tool calls, exactly:\n"
                            f"1. touch {inside_file}\n2. touch {outside_file}\n"
                            "Run the second with escalated sandbox permissions (it is "
                            "outside the workspace). If it is denied, do not retry or work "
                            "around it; reply done."
                        ),
                    },
                }
            )
            requests = await until_done(driver, events, "k1")
            after = await restored(user, prompt["session_id"])
        finally:
            await user.stop()
            await driver.stop()
    write_artifact(
        "k1.json",
        {
            "before": prompt["thread_settings_before"],
            "reported": requests,
            "inside_written": inside_file.exists(),
            "outside_written": outside_file.exists(),
            "after": after,
        },
    )
    assert prompt["thread_settings_before"]["approvalPolicy"] == "never"
    assert requests and all(str(outside_file) in str(item["command"]) for item in requests)
    assert inside_file.exists() and not outside_file.exists()
    assert after == ORIGINAL


async def test_k2_lost_restore_is_reported_and_the_next_scoped_end_restores(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    async with app_server(project) as endpoint:
        driver, events, seen = await yuki(endpoint, project)
        user = UserClient()
        await user.start(endpoint)
        first = driver.rpc
        call = first.call

        async def lose_restore(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "thread/settings/update" and params["approvalPolicy"] == "never":
                await first.ws.close()  # the owner restarts right at the restore
            return await call(method, params)

        first.call = lose_restore  # type: ignore[method-assign]
        try:
            prompt = await driver.execute(
                {
                    "id": "k2-first",
                    "action": "prompt",
                    "arguments": {
                        "cwd": str(project),
                        "scope": scope(project),
                        "prompt": "Reply with the single word ok.",
                    },
                }
            )
            thread = prompt["session_id"]
            await until_done(driver, events, "k2-first")
            # The failed restore is reported, then the driver reconnects to the owner.
            waiting = {"scope_restore_failed", "available"}
            async with asyncio.timeout(30):
                while waiting:
                    kind, value = await events.get()
                    if value.get("payload", {}).get("scope_restore_failed"):
                        waiting.discard("scope_restore_failed")
                    if kind == "connection" and value["status"] == "available":
                        waiting.discard("available")
            stuck = await user.settings(thread)
            await driver.execute(
                {
                    "id": "k2-next",
                    "action": "prompt",
                    "session_id": thread,
                    "arguments": {
                        "scope": scope(project),
                        "prompt": "Reply with the single word ok.",
                    },
                }
            )
            await until_done(driver, events, "k2-next")
            after = await restored(user, thread)
        finally:
            await user.stop()
            await driver.stop()
    [failed] = [value for _, value in seen if value.get("payload", {}).get("scope_restore_failed")]
    write_artifact("k2.json", {"failed": failed, "stuck": stuck, "after": after})
    assert failed["operation_id"] == "k2-first"
    assert failed["payload"]["thread_settings"]["approvalPolicy"] == "never"
    assert stuck["approvalPolicy"] == "on-request"
    assert after == ORIGINAL


async def test_k3_user_turn_in_the_race_is_narrowed_and_not_yukis(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    target = outside / "user.txt"
    async with app_server(project) as endpoint:
        user = UserClient()
        await user.start(endpoint)
        thread = (await user.call("thread/start", {"cwd": str(project)}))["thread"]["id"]
        await user.turn(thread, "Reply with the single word ok.")
        await user.completed(thread)
        driver, events, _ = await yuki(endpoint, project)
        try:
            await driver.execute(
                {
                    "id": "k3-watch",
                    "action": "watch",
                    "session_id": thread,
                    "observation_scope": "session",
                    "arguments": {},
                }
            )
            call = driver.rpc.call
            user_turn: list[str] = []

            async def user_starts_after_narrowing(
                method: str, params: dict[str, Any]
            ) -> dict[str, Any]:
                result = await call(method, params)
                if method == "thread/settings/update":
                    user_turn.append(
                        await user.turn(
                            thread,
                            f"Run exactly this shell command with escalated sandbox "
                            f"permissions (it is outside the workspace): touch {target}\n"
                            "If it is denied, do not retry; reply done.",
                        )
                    )
                return result

            driver.rpc.call = user_starts_after_narrowing  # type: ignore[method-assign]
            await driver.execute(
                {
                    "id": "k3",
                    "action": "prompt",
                    "session_id": thread,
                    "arguments": {
                        "scope": scope(project),
                        "prompt": "Reply with the single word ok.",
                    },
                }
            )
            driver.rpc.call = call  # type: ignore[method-assign]
            async with asyncio.timeout(240):
                user_request = await user.requests.get()
                while True:
                    kind, value = await events.get()
                    if kind == "permission":
                        break
            with pytest.raises(DriverError) as refused:
                await driver.execute(
                    {
                        "id": "k3-answer",
                        "action": "respond",
                        "session_id": thread,
                        "execution_id": value["execution_id"],
                        "arguments": {
                            "connection_id": driver.rpc.connection_id,
                            "permission_id": str(value["payload"]["native_request_id"]),
                            "option_id": "accept",
                        },
                    }
                )
            await user.ws.send(
                json.dumps({"id": user_request["id"], "result": {"decision": "decline"}})
            )
            await until_done(driver, events, "k3")
            after = await restored(user, thread)
        finally:
            await user.stop()
            await driver.stop()
    write_artifact(
        "k3.json",
        {
            "user_turn": user_turn,
            "user_request": user_request,
            "yuki_event": value["payload"],
            "refused": refused.value.code,
            "user_file_written": target.exists(),
            "after": after,
        },
    )
    # The race: the user's own turn ran narrowed, so it asked; Yuki may not answer it.
    assert user_request["params"]["turnId"] == user_turn[0]
    assert value["payload"]["answer_at"] == "native_terminal"
    assert refused.value.code == "not_owner"
    assert not target.exists()
    assert after == ORIGINAL
