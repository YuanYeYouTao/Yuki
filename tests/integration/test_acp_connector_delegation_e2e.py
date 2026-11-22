"""Real Bot + real Connector + ACP driver + a scripted stdio ACP agent: one delegation.

The agent (``packages/yuki-agent-connector/tests/fakes/acp_scope_agent.py``)
advertises ``_session/steering`` like codex-acp 2.9 / claude-agent-acp 0.109 and, like
Gemini CLI, only ``loadSession`` (no session/list, no session/close). Failure cases
written first:

- a ``message_task(mode=steer)`` of the running delegation is refused (the instance
  declares no ``steer``) or does not reach ``_session/steering``; the steer's receipt is
  not admitted, or the steered text does not join the delegation's turn;
- after ``release`` (no session/close: the agent process restarts) a ``resume`` of the
  same session fails with ``cwd_unknown``, or loads it in another cwd than the one the
  delegation created it in.

No model, no paid call. Artifact: ``test-results/acp-connector-delegation/run.json``.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from websockets.asyncio.client import connect
from yuki_agent_connector import PROTOCOL_VERSION
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal

from yuki.external_agents import service as service_module
from yuki.external_agents.db_models import AgentOperationModel
from yuki.runtime.work_schema_v1 import work

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "packages" / "yuki-agent-connector" / "tests" / "fakes" / "acp_scope_agent.py"
ARTIFACT = ROOT / "test-results" / "acp-connector-delegation"

pytestmark = pytest.mark.skipif(
    service_module.CONNECTOR_PROTOCOL != PROTOCOL_VERSION,
    reason="Q4 hard switch: different protocols never pair",
)


async def until(predicate):
    async with asyncio.timeout(10):
        while not (value := await predicate()):  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    return value


def traced(trace: Path, method: str) -> list[dict]:
    rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    return [row for row in rows if row["method"] == method]


async def test_acp_delegation_steers_its_turn_and_resumes_in_its_own_cwd(
    bridge, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    trace = tmp_path / "agent-trace.jsonl"
    monkeypatch.setattr(
        connector_module,
        "connect",
        lambda url, **kw: connect(url, ssl=bridge.ssl, proxy=None, **kw),
    )
    connector = Connector(
        Config(
            bridge.websocket_url,
            bridge.device["device_id"],
            bridge.device["token"],
            instances=[
                Instance(
                    "acp-one",
                    "acp",
                    "ACP",
                    sys.executable,
                    args=[str(AGENT)],
                    # The configured default is not the delegation's cwd.
                    cwd=str(tmp_path),
                    env={
                        "FAKE_ACP_TRACE": str(trace),
                        "FAKE_ACP_STEERING": "1",
                        "FAKE_ACP_NO_LIST": "1",
                        "FAKE_ACP_NO_CLOSE": "1",
                    },
                )
            ],
        ),
        Journal(tmp_path / "journal.sqlite3", namespace=bridge.device["device_id"]),
    )
    running = asyncio.create_task(connector.run())
    try:

        async def online():
            catalog = await bridge.service.catalog(bridge.env.person)
            return next((item for item in catalog["instances"] if item["online"]), None)

        instance = await until(online)

        async def operations(record):
            async with bridge.database.read_session() as reader:
                return list(
                    await reader.scalars(
                        select(AgentOperationModel)
                        .where(AgentOperationModel.work_id == record)
                        .order_by(AgentOperationModel.created_at)
                    )
                )

        def state_is(record, expected):
            async def check():
                async with bridge.database.read_session() as reader:
                    state = await reader.scalar(select(work.c.state).where(work.c.id == record))
                return state == expected

            return check

        async with accepted_work(bridge) as control:
            script = {"during": [{"say": "working "}, {"steer": "injected"}, {"say": " done"}]}
            started = await control.tool_outcome(
                "start_subtask",
                {
                    "goal": json.dumps(script),
                    "executor": {"agent": instance["id"]},
                    "cwd": str(project),
                },
                "acp-start",
            )
            assert started.ok, started
            record = started.data["id"]

            async def session_known():
                [start] = await operations(record)
                return start.session_id is not None

            await until(session_known)
            steered = await control.tool_outcome(
                "message_task", {"id": record, "text": "also lint", "mode": "steer"}, "acp-steer"
            )
            assert steered.ok, steered

            async def steer_receipt():
                rows = [row for row in await operations(record) if row.action == "steer"]
                return json.loads(rows[0].result_json or "null") if rows else None

            steer_result = await until(steer_receipt)
            assert steer_result["admitted"] is True and "error" not in steer_result
            await until(state_is(record, "completed"))
            start_result = json.loads((await operations(record))[0].result_json)
            assert start_result["text"] == "working steered:also lint done"
            assert start_result["cwd"] == str(project)
            assert [row["text"] for row in traced(trace, "_session/steering")] == ["also lint"]

            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": record}, "acp-release"
            )
            assert released.ok, released

            async def restarted():
                return len(traced(trace, "started")) == 2

            await until(restarted)
            resumed = await control.tool_outcome(
                "manage_task", {"action": "resume", "id": record, "text": "{}"}, "acp-resume"
            )
            assert resumed.ok, resumed
            await until(state_is(record, "completed"))
            loads = traced(trace, "session/load")
            [created] = traced(trace, "session/new")
            assert loads == [
                {"method": "session/load", "cwd": str(project), "session": created["session"]}
            ]
            final = await operations(record)
        ARTIFACT.mkdir(parents=True, exist_ok=True)
        (ARTIFACT / "run.json").write_text(
            json.dumps(
                {
                    "connector_protocol": PROTOCOL_VERSION,
                    "declared": instance.get("capabilities"),
                    "operations": [
                        {
                            "action": row.action,
                            "receipt": {
                                key: value
                                for key, value in json.loads(row.result_json or "{}").items()
                                if key
                                in {"status", "admitted", "pending", "text", "cwd", "error"}
                            },
                        }
                        for row in final
                    ],
                    "agent_trace": [
                        json.loads(line)
                        for line in trace.read_text().splitlines()
                        if json.loads(line)["method"] != "started"
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    finally:
        running.cancel()
        if connector.stream is not None:
            await connector.stream.close()
        await asyncio.gather(running, return_exceptions=True)
        await asyncio.gather(
            *(driver.stop() for driver in connector.drivers.values()), return_exceptions=True
        )
        connector.journal.close()
