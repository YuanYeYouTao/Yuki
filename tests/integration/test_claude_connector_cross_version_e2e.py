# ruff: noqa: E402
"""Real Bot + real Connector + Claude driver (real SDK, controlled CLI wire), any version.

Run against the connector in this tree (0.8.0) and, for cross-version evidence, against
the deployed 0.7.0 package by overriding pytest's ``pythonpath`` with its ``src``.
No model, no paid call: the CLI side is a deterministic stream-json peer.
The Claude SDK belongs to the connector's environment, not the Bot's, so run it with
``uv run --with claude-agent-sdk==<connector pin> pytest <this file>``.
Artifact: ``test-results/claude-connector-cross-version/<connector version>.json``.
"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from websockets.asyncio.client import connect
from yuki_agent_connector import __version__ as connector_version
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.drivers import claude as claude_module
from yuki_agent_connector.journal import Journal

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.errors import ExternalAgentArgumentError

REPLAYS_INPUT = connector_version >= "0.8.0"
ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "claude-connector-cross-version"


def record(**facts: Any) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    facts = {"connector_version": connector_version, "connector": claude_module.__file__, **facts}
    (ARTIFACT / f"{connector_version}.json").write_text(json.dumps(facts, indent=2) + "\n")


class CliWire(Transport):
    """The CLI side of stream-json: answers control requests; replays inputs on demand."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []
        self.queued: list[dict[str, Any]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        message = json.loads(data)
        self.writes.append(message)
        if message["type"] == "control_request":
            await self.incoming.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {},
                    },
                }
            )
        elif message["type"] == "user":
            # Held like a CLI that has not taken the input up yet.
            self.queued.append(message)

    async def take_up(self) -> None:
        await self.incoming.put(self.queued.pop(0))

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self.incoming.get()) is not None:
            yield message

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)

    def is_ready(self) -> bool:
        return not self.closed

    async def end_input(self) -> None:
        pass


async def until(predicate):
    async with asyncio.timeout(8):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_silent_claude_cli_never_holds_the_work_on_any_connector_version(
    bridge, tmp_path, monkeypatch
):
    wires: list[CliWire] = []

    def factory(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        wire = CliWire()
        wires.append(wire)
        return ClaudeSDKClient(options=options, transport=wire)

    monkeypatch.setattr(claude_module, "ClaudeSDKClient", factory)
    monkeypatch.setattr(
        connector_module,
        "connect",
        lambda url, **kw: connect(url, ssl=bridge.ssl, proxy=None, **kw),
    )
    journal = Journal(
        tmp_path / "connector" / "journal.sqlite3", namespace=bridge.device["device_id"]
    )
    connector = Connector(
        Config(
            bridge.websocket_url,
            bridge.device["device_id"],
            bridge.device["token"],
            instances=[
                Instance("claude-one", "claude", "Claude", sys.executable, cwd=str(tmp_path))
            ],
        ),
        journal,
    )
    running = asyncio.create_task(connector.run())
    try:

        async def online():
            if running.done():
                await running
            catalog = await bridge.service.catalog(bridge.env.person)
            return any(item["online"] for item in catalog["instances"])

        await until(online)
        catalog = await bridge.service.catalog(bridge.env.person)
        instance = next(item for item in catalog["instances"] if item["online"])
        assert ("queue" in instance["capabilities"]) is REPLAYS_INPUT
        async with accepted_work(bridge):
            operation = str(uuid4())
            # The CLI never takes the input up: on 0.7.0 the connector itself still waits
            # for admission, yet the Work's tool call returns at once on both versions.
            async with asyncio.timeout(5):
                result = await bridge.service.execute(
                    {"action": "prompt", "instance_id": instance["id"], "prompt": "work"},
                    operation,
                    bridge.source,
                )
            assert result["operation_id"] == operation and result["pending"] is True

            async def written():
                return bool(wires) and bool(wires[0].queued)

            await until(written)
            wire = wires[0]
            await wire.take_up()

            async def admitted():
                async with bridge.database.read_session() as reader:
                    row = await reader.get(AgentOperationModel, operation)
                    return (json.loads(row.result_json or "{}")).get("admitted") is True

            await until(admitted)
            async with bridge.database.read_session() as reader:
                row = await reader.get(AgentOperationModel, operation)
            session_id = row.session_id
            queued = {
                "action": "queue",
                "session_id": session_id,
                "prompt": "also this",
            }
            if REPLAYS_INPUT:
                appended = str(uuid4())
                await bridge.service.execute(queued, appended, bridge.source)

                async def written_again():
                    return bool(wire.queued)

                await until(written_again)
            else:
                # 0.7.0 declares no queue for Claude: refused before any device round trip.
                with pytest.raises(ExternalAgentArgumentError, match="unsupported_action"):
                    await bridge.service.execute(queued, str(uuid4()), bridge.source)
        native_session = json.loads(row.result_json)["session_id"]
        await wire.incoming.put(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": native_session,
                "result": "done",
            }
        )
        # The CLI wakes itself for a finished background task and asks for Bash.
        await wire.incoming.put(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "native",
                    "content": [{"type": "text", "text": "background finished"}],
                },
                "session_id": native_session,
            }
        )
        await wire.incoming.put(
            {
                "type": "control_request",
                "request_id": "self-started",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "tool_use_id": "unseen-bash",
                    "input": {"command": "gh run view 1 > /Users/someone/private.txt"},
                },
            }
        )

        async def listed():
            view = await bridge.service.execute(
                {"action": "get", "session_id": session_id}, str(uuid4()), bridge.source
            )
            return view if view["pending_unowned_requests"] else None

        if not REPLAYS_INPUT:
            # 0.7.0 never surfaces a wait nobody owns; nothing new reaches the server.
            await asyncio.sleep(0.5)
            view = await bridge.service.execute(
                {"action": "get", "session_id": session_id}, str(uuid4()), bridge.source
            )
            assert view["pending_unowned_requests"] == []
            record(
                capabilities=instance["capabilities"],
                prompt_returned_pending=True,
                queue="refused_before_device:unsupported_action",
                unowned_requests_surfaced=[],
            )
            return
        await until(listed)
        view = await listed()
        [waiting] = view["pending_unowned_requests"]
        assert waiting["tool"] == "Bash" and "private.txt" not in json.dumps(view)
        await bridge.service.execute(
            {
                "action": "respond",
                "session_id": session_id,
                "permission_id": waiting["permission_id"],
                "option_id": "deny",
            },
            str(uuid4()),
            bridge.source,
        )

        async def denied():
            return any(
                item["type"] == "control_response"
                and item["response"].get("request_id") == "self-started"
                for item in wire.writes
            )

        await until(denied)
        answer = next(
            item
            for item in wire.writes
            if item["type"] == "control_response"
            and item["response"].get("request_id") == "self-started"
        )
        assert answer["response"]["response"]["behavior"] == "deny"
        async with bridge.database.read_session() as reader:
            stored = list(
                await reader.scalars(
                    select(AgentOperationModel.id).where(AgentOperationModel.id == operation)
                )
            )
        assert stored == [operation]
        record(
            capabilities=instance["capabilities"],
            prompt_returned_pending=True,
            queue="written_to_cli",
            unowned_requests_surfaced=view["pending_unowned_requests"],
            cli_answer=answer["response"]["response"],
        )
    finally:
        running.cancel()
        if connector.stream is not None:
            await connector.stream.close()
        await asyncio.gather(running, return_exceptions=True)
        journal.close()
