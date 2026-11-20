"""#645 natural E2E: real Bot service + real connector + a real Claude Code CLI.

Opt-in only (it starts a real Claude process and spends a little model usage):
``YUKI_TEST_REAL_CLAUDE=/path/to/claude``. Nothing is driven by hand: no manual
maintenance tick and no waiting helper; the release and its settlement run through
the normal paths (the unowned wait's own arrival starts the settlement).

Evidence, each from its own source:
- native consumption: Claude's own session transcript holds a ``tool_result`` for the
  pending ``tool_use_id`` carrying the connector's deny text (a connector receipt
  alone is not taken as proof that the native callback consumed the answer);
- process: the CLI pid this delegation started is gone once the turn ends;
- server: the deny is the release's (``answered_via: release``) and no deny-only card
  reached the parent.

Isolation: a fresh temporary cwd, a new session (no resume), ``ANTHROPIC_MODEL=haiku``,
and no setting sources: the connector's options are built as in production except
``setting_sources=[]``, so the user's hooks (e.g. SessionStart), settings env, model
and CLAUDE.md are not loaded; the delegation scope's own ``settings`` still applies.
Nothing is deleted: Claude keeps its transcript where it writes it; a copy and its
path go into the artifact. Artifact: ``test-results/claude-release-natural/``.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("claude_agent_sdk")
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import _inputs
from websockets.asyncio.client import connect
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.drivers import claude as claude_module
from yuki_agent_connector.journal import Journal

from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel

CLAUDE = os.environ.get("YUKI_TEST_REAL_CLAUDE")
DENY = "User declined the original native request"
ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "claude-release-natural"
pytestmark = pytest.mark.skipif(not CLAUDE, reason="set YUKI_TEST_REAL_CLAUDE to opt in")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def until(predicate, seconds=240):
    async with asyncio.timeout(seconds):
        while not (found := await predicate()):  # noqa: ASYNC110
            await asyncio.sleep(0.5)
        return found


def transcript(session_id: str) -> Path | None:
    found = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return found[0] if len(found) == 1 else None


def native_denial(path: Path, tool_use_id: str) -> dict | None:
    """The CLI's own record of the tool result for this call, if it carries the deny."""
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
                and DENY in json.dumps(block.get("content"), ensure_ascii=False)
            ):
                return block
    return None


async def test_release_denies_a_real_claude_permission_and_the_cli_exits(
    bridge, tmp_path, monkeypatch
):
    assert CLAUDE is not None
    work = tmp_path / "work"
    work.mkdir()
    outside = Path("/tmp") / f"yuki-645-natural-{uuid4()}"
    monkeypatch.setattr(
        connector_module,
        "connect",
        lambda url, **kw: connect(url, ssl=bridge.ssl, proxy=None, **kw),
    )
    built: list[dict] = []
    production_options = claude_module.ClaudeAgentOptions

    def isolated_options(**kwargs):
        # The only deviation from production: no user/project/local setting sources.
        built.append(
            {"setting_sources": kwargs.get("setting_sources"), "settings": kwargs.get("settings")}
        )
        return production_options(**{**kwargs, "setting_sources": []})

    monkeypatch.setattr(claude_module, "ClaudeAgentOptions", isolated_options)
    connector = Connector(
        Config(
            bridge.websocket_url,
            bridge.device["device_id"],
            bridge.device["token"],
            instances=[
                Instance(
                    "claude-real",
                    "claude",
                    "Claude",
                    CLAUDE,
                    cwd=str(work),
                    env={"ANTHROPIC_MODEL": "haiku"},
                )
            ],
        ),
        Journal(tmp_path / "connector" / "journal.sqlite3", namespace=bridge.device["device_id"]),
    )
    running = asyncio.create_task(connector.run())
    facts: dict = {"claude": CLAUDE, "outside": str(outside)}
    session_id = None
    try:

        async def online():
            if running.done():
                await running
            catalog = await bridge.service.catalog(bridge.env.person)
            return any(item["online"] for item in catalog["instances"])

        await until(online, 60)
        catalog = await bridge.service.catalog(bridge.env.person)
        instance = next(item for item in catalog["instances"] if item["online"])
        async with accepted_work(bridge) as control:
            started = await control.tool_outcome(
                "start_subtask",
                {
                    "goal": (
                        f"Use the Bash tool to run exactly this one command: touch {outside} . "
                        "If it is declined, do not retry and do not try anything else; "
                        "reply with the single word STOPPED."
                    ),
                    "executor": {"agent": instance["id"]},
                    "scope": {"write_paths": [str(work)]},
                },
                "natural-start",
            )
            assert started.ok, started
            identity = started.data["id"]

            async def asked():
                async with bridge.database.read_session() as reader:
                    return await reader.scalar(
                        select(AgentEventModel)
                        .join(
                            AgentOperationModel,
                            AgentOperationModel.id == AgentEventModel.operation_id,
                        )
                        .where(
                            AgentOperationModel.work_id == identity,
                            AgentEventModel.kind == "permission",
                        )
                    )

            request = await until(asked)
            stored = json.loads(request.payload_json)
            tool_use_id = stored["payload"]["native_request_id"]
            session_id = stored["session_id"]
            driver = connector.drivers["claude-real"]
            owner = driver.sessions[session_id]
            pid = owner.client._transport._process.pid
            facts.update(session_id=session_id, tool_use_id=tool_use_id, cli_pid=pid)
            assert alive(pid)
            before = len(await _inputs(bridge, control.current["id"]))
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "natural-release"
            )
            assert released.ok, released

            async def denied_natively():
                path = transcript(session_id)
                return native_denial(path, tool_use_id) if path is not None else None

            facts["native_tool_result"] = await until(denied_natively)

            async def exited():
                return owner.stopped.is_set() and not alive(pid)

            await until(exited)
            facts["cli_exited_after_turn"] = True
            async with bridge.database.read_session() as reader:
                answers = list(
                    await reader.scalars(
                        select(AgentOperationModel).where(
                            AgentOperationModel.work_id == identity,
                            AgentOperationModel.action == "respond",
                        )
                    )
                )
            sources = [json.loads(item.source_json) for item in answers]
            commands = [json.loads(item.command_json) for item in answers]
            facts["answers"] = [
                {"id": a.id, "receipt": json.loads(a.result_json or "null")} for a in answers
            ]
            assert answers and {s.get("answered_via") for s in sources} == {"release"}
            assert {c["arguments"]["option_id"] for c in commands} == {"deny"}
            assert tool_use_id in {c["arguments"]["native_request_id"] for c in commands}
            later = (await _inputs(bridge, control.current["id"]))[before:]
            facts["parent_inputs_after_release"] = [row["source_key"] for row in later]
            assert not any("deny_only" in row["payload_json"] for row in later)
            facts["outside_file_created"] = outside.exists()
            assert not outside.exists()
    finally:
        running.cancel()
        if connector.stream is not None:
            await connector.stream.close()
        await asyncio.gather(running, return_exceptions=True)
        await asyncio.gather(
            *(driver.stop() for driver in connector.drivers.values()), return_exceptions=True
        )
        connector.journal.close()
        ARTIFACT.mkdir(parents=True, exist_ok=True)
        if session_id is not None and (path := transcript(session_id)) is not None:
            # Kept in place (no deletion of anything under the user's directory).
            shutil.copyfile(path, ARTIFACT / "transcript.jsonl")
            facts["transcript_kept_at"] = str(path)
        if facts.get("cli_pid") is not None:
            facts["cli_alive_at_end"] = alive(facts["cli_pid"])
        (ARTIFACT / "result.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2, default=str) + "\n"
        )
