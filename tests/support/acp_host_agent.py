"""Actual stdio peer fixture; no imported Yuki implementation or network."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


async def main() -> None:
    root = Path(sys.argv[1])
    pending: dict[int, asyncio.Future[Any]] = {}
    active: dict[str, asyncio.Task[None]] = {}
    counter = 100

    def send(value: dict[str, Any]) -> None:
        sys.stdout.buffer.write(
            (json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        sys.stdout.buffer.flush()

    async def call(method: str, params: dict[str, Any]) -> Any:
        nonlocal counter
        counter += 1
        key = counter
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        pending[key] = future
        send({"id": key, "method": method, "params": {"sessionId": "fixture-session", **params}})
        return await future

    async def prompt(message: dict[str, Any]) -> None:
        text = message["params"]["prompt"][0]["text"]
        try:
            if text == "crash":
                os._exit(7)
            if text == "permission":
                for _ in range(2):
                    result = await call(
                        "session/request_permission",
                        {
                            "toolCall": {"toolCallId": "same-tool", "title": "Same question"},
                            "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}],
                        },
                    )
                    if result.get("outcome", {}).get("outcome") == "cancelled":
                        raise asyncio.CancelledError
            elif text == "callbacks":
                await call(
                    "fs/write_text_file",
                    {"path": str(root / "callback.txt"), "content": "真实回调\n"},
                )
                result = await call("fs/read_text_file", {"path": str(root / "callback.txt")})
                assert result["content"] == "真实回调\n"
                terminal = await call(
                    "terminal/create",
                    {
                        "command": sys.executable,
                        "args": [
                            "-c",
                            "import time; print('background', flush=True); time.sleep(30)",
                        ],
                    },
                )
                (root / "terminal-id").write_text(terminal["terminalId"])
            elif text == "wait":
                (root / "prompt-active").write_text("active")
                await asyncio.Event().wait()
            elif text == "large":
                send(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": "fixture-session",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "雪" * 100000},
                            },
                        },
                    }
                )
            elif text == "text":
                send(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": "fixture-session",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": "EXTERNAL_AGENT_ORIGINAL_RESULT",
                                },
                            },
                        },
                    }
                )
            send({"id": message["id"], "result": {"stopReason": "end_turn"}})
        except asyncio.CancelledError:
            send({"id": message["id"], "result": {"stopReason": "cancelled"}})

    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        message = json.loads(line)
        method = message.get("method")
        if not method:
            future = pending.pop(message["id"])
            if future.cancelled():
                continue  # The callback's prompt was cancelled before this response arrived.
            if "error" in message:
                future.set_exception(RuntimeError(str(message["error"])))
            else:
                future.set_result(message["result"])
            continue
        with (root / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"method": method, "params": message.get("params")}) + "\n")
        if method == "initialize":
            print("separate stderr diagnostic", file=sys.stderr, flush=True)
            send(
                {
                    "id": message["id"],
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {},
                        "agentInfo": {"name": "fixture", "version": "1"},
                    },
                }
            )
        elif method == "session/new":
            send({"id": message["id"], "result": {"sessionId": "fixture-session"}})
        elif method == "session/prompt":
            task = asyncio.create_task(prompt(message))
            active["fixture-session"] = task
        elif method == "session/cancel":
            active["fixture-session"].cancel()


if __name__ == "__main__":
    asyncio.run(main())
