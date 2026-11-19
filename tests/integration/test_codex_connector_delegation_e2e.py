"""Real Bot + real Connector + Codex driver (controlled app-server wire): one delegation.

Run in the ``claude-cross-version`` CI job's paired configuration (this Bot with this
connector; different protocols never pair, which the Claude pairing test records).
Failure cases written first: Yuki's input to a Codex thread uses ``turn/start`` (which
merges into a running user turn); the delegation record does not settle from the
native terminal; the parent sees progress; a receipt is answered as an RPC.
No model, no paid call. Artifact: ``test-results/codex-connector-delegation/run.json``.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from yuki_agent_connector import PROTOCOL_VERSION
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal

from yuki.external_agents import service as service_module
from yuki.runtime.work_schema_v1 import inputs, work

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "codex-connector-delegation"

pytestmark = pytest.mark.skipif(
    service_module.CONNECTOR_PROTOCOL != PROTOCOL_VERSION,
    reason="Q4 hard switch: different protocols never pair",
)


class AppServer:
    """The Codex app-server side: one idle thread; queue/add starts its own turn."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.socket: Any = None
        self.client_id: str | None = None

    async def handler(self, socket: Any) -> None:
        self.socket = socket
        async for raw in socket:
            value = json.loads(raw)
            if "method" not in value or "id" not in value:
                continue
            self.calls.append({"method": value["method"]})
            method, params = value["method"], value.get("params", {})
            thread = {"id": "thread", "status": {"type": "idle"}, "turns": []}
            result: Any = {}
            if method == "initialize":
                result = {"userAgent": "fake-codex-0.169.0"}
            elif method == "thread/list":
                result = {"data": [thread]}
            elif method in {"thread/read", "thread/resume", "thread/start"}:
                result = {"thread": thread}
            elif method in {"thread/turns/list", "thread/loaded/list", "thread/queue/list"}:
                result = {"data": []}
            elif method == "thread/queue/add":
                self.client_id = params["clientUserMessageId"]
                result = {"queuedSubmission": {"id": "q", "clientUserMessageId": self.client_id}}
            await socket.send(json.dumps({"id": value["id"], "result": result}))
            if method == "thread/queue/add":
                for name, notification in (
                    ("turn/started", {"turn": {"id": "turn", "status": "inProgress"}}),
                    (
                        "item/started",
                        {
                            "turnId": "turn",
                            "item": {"id": "in", "type": "userMessage", "clientId": self.client_id},
                        },
                    ),
                ):
                    await socket.send(
                        json.dumps(
                            {"method": name, "params": {"threadId": "thread", **notification}}
                        )
                    )

    async def finish(self) -> None:
        await self.socket.send(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn", "status": "completed", "items": []},
                    },
                }
            )
        )


async def until(predicate):
    async with asyncio.timeout(10):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_codex_delegation_settles_from_the_native_terminal(bridge, tmp_path, monkeypatch):
    server = AppServer()
    monkeypatch.setattr(
        connector_module,
        "connect",
        lambda url, **kw: connect(url, ssl=bridge.ssl, proxy=None, **kw),
    )
    async with serve(server.handler, "127.0.0.1", 0) as listener:
        endpoint = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        connector = Connector(
            Config(
                bridge.websocket_url,
                bridge.device["device_id"],
                bridge.device["token"],
                instances=[
                    Instance(
                        "codex-one",
                        "codex",
                        "Codex",
                        "unused",
                        cwd=str(tmp_path),
                        endpoint=endpoint,
                    )
                ],
            ),
            Journal(tmp_path / "journal.sqlite3", namespace=bridge.device["device_id"]),
        )
        running = asyncio.create_task(connector.run())
        try:

            async def online():
                catalog = await bridge.service.catalog(bridge.env.person)
                return any(item["online"] for item in catalog["instances"])

            await until(online)
            catalog = await bridge.service.catalog(bridge.env.person)
            instance = next(item for item in catalog["instances"] if item["online"])
            sessions = await bridge.service.execute(
                {"action": "list", "instance_id": instance["id"]}, "list", bridge.source
            )
            session = sessions["sessions"][0]["id"]
            async with accepted_work(bridge) as control:
                outcome = await control.tool_outcome(
                    "start_subtask",
                    {"goal": "fix it", "executor": {"agent": instance["id"]}, "session": session},
                    "codex-start",
                )
                assert outcome.ok, outcome
                record = outcome.data["id"]

                async def turn_owned():
                    return any(call["method"] == "thread/queue/add" for call in server.calls)

                await until(turn_owned)
                assert not any(call["method"] == "turn/start" for call in server.calls)
                await server.finish()

                async def settled():
                    async with bridge.database.read_session() as reader:
                        state = await reader.scalar(select(work.c.state).where(work.c.id == record))
                    return state == "completed"

                await until(settled)
                async with bridge.database.read_session() as reader:
                    parent_inputs = list(
                        (
                            await reader.execute(
                                select(inputs.c.source_key).where(
                                    inputs.c.work_id == control.current["id"]
                                )
                            )
                        ).scalars()
                    )
                # Only the result card; no native progress reached the parent.
                assert parent_inputs == [
                    f"worker-result:{record}:{parent_inputs[0].split(':')[-1]}"
                ]
            ARTIFACT.mkdir(parents=True, exist_ok=True)
            (ARTIFACT / "run.json").write_text(
                json.dumps(
                    {
                        "connector_protocol": PROTOCOL_VERSION,
                        "record_state": "completed",
                        "app_server_methods": [call["method"] for call in server.calls],
                        "parent_inputs": parent_inputs,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            running.cancel()
            if connector.stream is not None:
                await connector.stream.close()
            await asyncio.gather(running, return_exceptions=True)
            connector.journal.close()
