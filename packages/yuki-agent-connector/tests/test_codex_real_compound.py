"""Compound commands under a narrowed scope on a real Codex app-server (#647 D1).

Real Codex sends a command approval as ``/bin/zsh -lc '<script>'``. Failure cases
written before the run:
- R1 ``cd <write path> && ls``, run with escalated permissions so Codex asks, is
  reported (the wrapper or the compound script was not judged by its segments);
- R2 ``cd <outside> && ls`` is auto-approved (a ``cd`` out of ``write_paths`` passed).

The test starts its own ``codex app-server`` on loopback with a separate CODEX_HOME;
the user's home, daemon and threads are never touched. Every reported request is
declined. Run (a few short paid model turns):
    YUKI_REAL_CODEX_HOME=<a CODEX_HOME holding only auth.json> \\
    YUKI_REAL_CODEX_BINARY=<codex executable> pytest tests/test_codex_real_compound.py
Artifact: test-results/scope-compound/codex-real-<platform>.json.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers.codex import CodexDriver

pytestmark = pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CODEX_HOME") or not os.environ.get("YUKI_REAL_CODEX_BINARY"),
    reason="paid real Codex run; set YUKI_REAL_CODEX_HOME and YUKI_REAL_CODEX_BINARY",
)
ARTIFACT = Path(__file__).resolve().parents[3] / "test-results" / "scope-compound"


@contextlib.asynccontextmanager
async def app_server(project: Path) -> AsyncIterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"ws://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(
        os.environ["YUKI_REAL_CODEX_BINARY"],
        "app-server",
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


async def test_real_codex_judges_wrapped_compound_commands(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    judged: list[dict[str, Any]] = []

    async def emit(kind: str, value: dict[str, Any]) -> None:
        events.put_nowait((kind, value))

    inside_command = f"cd {project} && ls"
    outside_command = f"cd {outside} && ls"
    async with app_server(project) as endpoint:
        driver = CodexDriver(
            Instance("codex", "codex", "Codex", "unused", cwd=str(project), endpoint=endpoint),
            emit,
        )
        await driver.start()
        judge = driver._inside

        def spy(method: str, params: dict[str, Any], scope: Any, thread: str) -> bool:
            inside = judge(method, params, scope, thread)
            judged.append({"method": method, "command": params.get("command"), "inside": inside})
            return inside

        driver._inside = spy  # type: ignore[method-assign]
        reported: list[Any] = []
        try:
            await driver.execute(
                {
                    "id": "compound",
                    "action": "prompt",
                    "arguments": {
                        "cwd": str(project),
                        "scope": {
                            "write_paths": [str(project)],
                            "commands": ["ls"],
                            "network": False,
                        },
                        "prompt": (
                            "Run these two shell commands as two separate tool calls, each "
                            "exactly as written and each with escalated sandbox permissions "
                            "(sandbox_permissions require_escalated, justification: test):\n"
                            f"1. {inside_command}\n2. {outside_command}\n"
                            "If one is denied, do not retry or work around it. Reply done."
                        ),
                    },
                }
            )
            async with asyncio.timeout(300):
                while True:
                    kind, value = await events.get()
                    if kind == "permission" and value["operation_id"] == "compound":
                        reported.append(value["payload"]["native_request"].get("command"))
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
                    if kind == "completed" and value.get("operation_id") == "compound":
                        break
        finally:
            await driver.stop()
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / f"codex-real-{sys.platform}.json").write_text(
        json.dumps(
            {
                "codex": os.environ["YUKI_REAL_CODEX_BINARY"],
                "judged": judged,
                "reported": reported,
            },
            indent=2,
        )
        + "\n"
    )
    commands = [item for item in judged if item["method"].startswith("item/commandExecution")]
    assert any(item["inside"] and inside_command in str(item["command"]) for item in commands)
    assert any(outside_command in str(command) for command in reported)
    assert not any(inside_command in str(command) for command in reported)
