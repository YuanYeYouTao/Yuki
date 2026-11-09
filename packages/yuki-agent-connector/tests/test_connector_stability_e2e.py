"""Real Connector processes against a real uvicorn ASGI server (task book §7.5, §8 phase 0).

Failure cases are listed in docs/operations/connector-stability-20261109.md §1. The server
plays one scripted reply per WebSocket attempt; the artifact records what each process saw.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from test_desktop_stdio_lifecycle import runtime

from yuki_agent_connector.config import Config

ARTIFACTS = Path(__file__).resolve().parents[3] / "test-results" / "connector-stability"
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ScriptedServer:
    """One reply per attempt; the last reply repeats if the Connector keeps trying."""

    def __init__(self, replies: list[tuple[Any, ...]]) -> None:
        self.replies = replies
        self.attempts: list[dict[str, Any]] = []
        self.hello: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        assert scope["type"] == "websocket"
        reply = self.replies[min(len(self.attempts), len(self.replies) - 1)]
        self.attempts.append(
            {
                "at": time.monotonic(),
                "reply": list(reply),
                "authorization": dict(scope["headers"]).get(b"authorization", b"").decode(),
            }
        )
        assert (await receive())["type"] == "websocket.connect"
        kind = reply[0]
        if kind == "http":
            await send({"type": "websocket.http.response.start", "status": reply[1], "headers": []})
            await send({"type": "websocket.http.response.body", "body": b"scripted"})
        elif kind == "refuse":
            # The production route closes before accept(); uvicorn answers HTTP 403.
            await send({"type": "websocket.close", "code": 4401})
        else:
            await send({"type": "websocket.accept"})
            message = await receive()
            self.hello.append(json.loads(message["text"]))
            if kind == "close":
                await send({"type": "websocket.close", "code": reply[1], "reason": reply[2]})
            else:
                assert kind == "send"
                await send({"type": "websocket.send", "text": reply[1]})
                await receive()


async def serving(app: ScriptedServer) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ws="websockets-sansio",
            lifespan="off",
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    # uvicorn exposes only this flag for "listening"; the Connector must not see a refusal.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    return server, task, server.servers[0].sockets[0].getsockname()[1]


async def connector_run(
    name: str,
    data: Path,
    replies: list[tuple[Any, ...]],
    until: Callable[[list[dict[str, Any]]], bool],
) -> dict[str, Any]:
    """Run a Desktop-owned Connector until `until(state lines)` or exit, then close stdin."""
    app = ScriptedServer(replies)
    server, serve_task, port = await serving(app)
    Config(f"ws://127.0.0.1:{port}/agents/v1/connect", "original-device", "fixture").save(
        data / "config.json"
    )
    executable, environment = runtime()
    # websockets reads its backoff from the environment; shorten it for the test only.
    environment |= {
        "WEBSOCKETS_BACKOFF_INITIAL_DELAY": "0.1",
        "WEBSOCKETS_BACKOFF_MIN_DELAY": "0.1",
        "WEBSOCKETS_BACKOFF_MAX_DELAY": "0.2",
    }
    process = await asyncio.create_subprocess_exec(
        executable,
        "-B",
        "-m",
        "yuki_agent_connector",
        "--data-dir",
        str(data),
        "run",
        "--desktop-stdin",
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert process.stdin is not None and process.stdout is not None
    lines: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        while not until(lines):
            raw = await asyncio.wait_for(process.stdout.readline(), 15)
            if not raw:
                break
            lines.append({"after": round(time.monotonic() - started, 3), **json.loads(raw)})
        exited_alone = process.returncode is not None or not until(lines)
        attempts_at_stop = len(app.attempts)
        if not exited_alone:
            # A stopped Connector must not keep reconnecting while it waits for Desktop.
            await asyncio.sleep(1)
        attempts_after_wait = len(app.attempts)
        process.stdin.close()
        rest, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await serve_task
    log = (data / "connector.log").read_text(encoding="utf-8")
    return {
        "name": name,
        "exit": process.returncode,
        "exited_before_stdin_closed": exited_alone,
        "state_lines": lines,
        "stdout_after_stdin_closed": rest.decode(),
        "stderr": stderr.decode(),
        "attempts": [{**a, "at": round(a["at"] - app.attempts[0]["at"], 3)} for a in app.attempts],
        "attempts_at_stop": attempts_at_stop,
        "attempts_after_wait": attempts_after_wait,
        "hello_count": len(app.hello),
        "connector_log": log,
    }


def states(run: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(line["state"], line.get("reason")) for line in run["state_lines"]]


def last(state: str) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda lines: bool(lines) and lines[-1]["state"] == state


async def test_handshake_and_close_classification_with_real_connector_process(
    tmp_path: Path,
) -> None:
    runs = []
    # S1/S2/S3/S3a/S3b: retry 502/404/426, reconnect after 1011 and other 1008, stop on
    # the explicit protocol mismatch.
    retry = await connector_run(
        "retry_then_protocol_mismatch",
        tmp_path / "retry",
        [
            ("http", 502),
            ("http", 404),
            ("http", 426),
            ("close", 1011, ""),
            ("close", 1008, "connector_hello_required"),
            ("close", 4401, ""),
            ("close", 1008, "unsupported_connector_protocol"),
        ],
        last("protocol_unsupported"),
    )
    runs.append(retry)
    # S4: handshake 403 (the production route's close before accept) and 401.
    refused = await connector_run(
        "handshake_403", tmp_path / "refused", [("refuse",)], last("authorization_lost")
    )
    runs.append(refused)
    unauthorized = await connector_run(
        "handshake_401",
        tmp_path / "unauthorized",
        [("http", 502), ("http", 401)],
        last("authorization_lost"),
    )
    runs.append(unauthorized)
    # S5: an unexpected exception inside a connection ends the process with a logged stack.
    crashed = await connector_run(
        "unexpected_exception",
        tmp_path / "crashed",
        [("send", json.dumps({"type": "ack"}))],
        lambda lines: False,
    )
    runs.append(crashed)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for run in runs:
        (ARTIFACTS / f"{run['name']}.connector.log").write_text(
            run["connector_log"], encoding="utf-8"
        )
    (ARTIFACTS / "classification.json").write_text(
        json.dumps(
            {
                "platform": sys.platform,
                "runs": [{k: v for k, v in run.items() if k != "connector_log"} for run in runs],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert states(retry) == [
        ("disconnected", "http_502"),
        ("connected", None),
        ("disconnected", "close_1011"),
        ("connected", None),
        ("disconnected", "close_1008:connector_hello_required"),
        ("connected", None),
        ("disconnected", "close_4401"),
        ("connected", None),
        ("protocol_unsupported", None),
    ]
    assert [a["reply"] for a in retry["attempts"]] == [
        ["http", 502],
        ["http", 404],
        ["http", 426],
        ["close", 1011, ""],
        ["close", 1008, "connector_hello_required"],
        ["close", 4401, ""],
        ["close", 1008, "unsupported_connector_protocol"],
    ]
    assert retry["attempts_after_wait"] == retry["attempts_at_stop"] == 7
    assert retry["hello_count"] == 4
    assert all(a["authorization"] == "Bearer fixture" for a in retry["attempts"])
    assert not retry["exited_before_stdin_closed"] and retry["exit"] == 0, retry["stderr"]
    log = retry["connector_log"]
    for status in (502, 404, 426):
        # websockets logs each retried handshake with its own backoff delay.
        assert f"HTTP {status}" in log and "reconnecting in" in log
    assert "connector_state disconnected reason=close_1011" in log
    assert "connector_state protocol_unsupported" in log
    assert "unsupported_connector_protocol: Connector and Yuki protocol" in log
    assert "authorization ended" not in log

    for run in (refused, unauthorized):
        assert states(run)[-1] == ("authorization_lost", None)
        assert run["attempts_after_wait"] == run["attempts_at_stop"]
        assert not run["exited_before_stdin_closed"] and run["exit"] == 0, run["stderr"]
        assert "Server authorization ended" in run["connector_log"]
        assert "connector_state authorization_lost" in run["connector_log"]
    assert states(refused) == [("authorization_lost", None)]
    assert len(refused["attempts"]) == 1 and refused["hello_count"] == 0
    assert states(unauthorized) == [("disconnected", "http_502"), ("authorization_lost", None)]

    assert crashed["exited_before_stdin_closed"] and crashed["exit"] != 0
    assert states(crashed) == [("connected", None)]
    assert "Connector run failed" in crashed["connector_log"]
    assert "Traceback" in crashed["connector_log"] and "KeyError" in crashed["connector_log"]
    assert "KeyError" in crashed["stderr"]


async def test_plain_run_prints_no_state_lines(tmp_path: Path) -> None:
    """S5b: ordinary CLI/user-service `run` keeps stdout free of Desktop state lines."""
    app = ScriptedServer([("refuse",)])
    server, serve_task, port = await serving(app)
    Config(f"ws://127.0.0.1:{port}/agents/v1/connect", "original-device", "fixture").save(
        tmp_path / "config.json"
    )
    executable, environment = runtime()
    process = await asyncio.create_subprocess_exec(
        executable,
        "-B",
        "-m",
        "yuki_agent_connector",
        "--data-dir",
        str(tmp_path),
        "run",
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        log = tmp_path / "connector.log"
        deadline = time.monotonic() + 15
        while "connector_state authorization_lost" not in (
            log.read_text(encoding="utf-8") if log.exists() else ""
        ):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        process.terminate()
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        assert stdout == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await serve_task
