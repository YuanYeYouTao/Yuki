"""ACP: mid-turn steering, rejection by the agent's own outcome, resume cwd (0.9.4).

Real stdio ACP driver against the scripted agent (``fakes/acp_scope_agent.py``).
Failure cases written before the implementation:

- S1: an agent advertising ``_meta.steering.supported`` is not declared ``steer``, or
  a steer of Yuki's running prompt does not reach ``_session/steering`` (refused as
  unsupported), or the steered text does not join that prompt's result;
- S2: an agent that does not advertise steering is declared ``steer`` or receives a
  steer; a second prompt during a running one stops being a conflict;
- S3: a steer the agent did not take (``promptRequired``) is reported as admitted;
  a steer naming another execution is sent; a steer may start a detached turn (no
  ``idleBehavior: promptRequired``);
- R1: a codex-acp reject (option ``cancel``) is labelled from the option ID
  (``reject_ends_prompt``) instead of the prompt's own ``stopReason: cancelled``;
- R2: a Gemini-style reject (option ``cancel`` too, the tool fails and the turn goes
  on) is labelled as ending the prompt, or the turn is not reported completed;
- R3: Yuki's ``deny`` of a wait nobody received fails as ``invalid_option`` instead of
  selecting the agent's one-time reject;
- C1: an agent without session/list cannot resume a session this connector created
  once its process restarted (``cwd_unknown``), or resumes it in the configured
  default or a command-supplied cwd instead of the cwd recorded at creation;
- C2: the recorded cwd widens a narrowed scope (``workspace-write`` although the
  scope does not cover the cwd);
- C3: a session this connector never created resumes without a known cwd.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import test_acp_phase2 as acp

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.journal import Journal

GEMINI_OPTIONS = [
    {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "proceed_always", "name": "Allow always", "kind": "allow_always"},
    {"optionId": "cancel", "name": "Reject", "kind": "reject_once"},
]


def answer(value: dict[str, Any], option: str, operation: str) -> dict[str, Any]:
    return {
        "id": f"answer-{option}",
        "action": "respond",
        "session_id": value["session_id"],
        "execution_id": value["execution_id"],
        "arguments": {
            "operation_id": operation,
            "connection_id": value["payload"]["connection_id"],
            "permission_id": value["payload"]["permission_id"],
            "option_id": option,
        },
    }


def steer(prompt: dict[str, Any], text: str, execution: str | None = None) -> dict[str, Any]:
    return {
        "id": f"steer-{text}",
        "action": "steer",
        "session_id": prompt["session_id"],
        "execution_id": execution or prompt["execution_id"],
        "arguments": {"prompt": text, "operation_id": "owner"},
    }


@pytest.mark.asyncio
async def test_steer_joins_the_running_prompt_through_the_extension(tmp_path: Path) -> None:
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_STEERING="1")
    script = {"during": [{"say": "working "}, {"steer": "injected"}, {"say": " done"}]}
    try:
        assert "steer" in driver.capabilities and "queue" not in driver.capabilities
        prompt = await driver.execute(acp.prompt("owner", script))
        with pytest.raises(DriverError) as mismatch:
            await driver.execute(steer(prompt, "elsewhere", execution="another-execution"))
        assert mismatch.value.code == "execution_mismatch"
        steered = await driver.execute(steer(prompt, "also lint"))
        assert steered == {
            "session_id": prompt["session_id"],
            "execution_id": prompt["execution_id"],
            "status": "running",
            "pending": True,
            "admitted": True,
        }
        _, completed = await acp.until(events, lambda kind, value: kind == "completed")
        assert completed["operation_id"] == "owner"
        assert completed["payload"]["status"] == "completed"
        assert completed["payload"]["text"] == "working steered:also lint done"
        [row] = acp.traced(trace, "_session/steering")
        assert row["text"] == "also lint"
        # Never a detached turn: an idle agent hands the input back instead.
        assert row["meta"] == {"steering": {"idleBehavior": "promptRequired"}}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_a_steer_the_agent_did_not_take_is_not_admitted(tmp_path: Path) -> None:
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_STEERING="1")
    script = {"during": [{"say": "working"}, {"steer": "promptRequired"}]}
    try:
        prompt = await driver.execute(acp.prompt("owner", script))
        with pytest.raises(DriverError) as refused:
            await driver.execute(steer(prompt, "too late"))
        assert refused.value.code == "turn_not_running" and not refused.value.uncertain
        _, completed = await acp.until(events, lambda kind, value: kind == "completed")
        assert completed["payload"]["text"] == "working"
        assert [row["text"] for row in acp.traced(trace, "_session/steering")] == ["too late"]
        with pytest.raises(DriverError) as idle:
            await driver.execute(steer(prompt, "after the end"))
        # No running prompt: refused here, nothing is sent to the agent.
        assert idle.value.code == "execution_mismatch"
        assert len(acp.traced(trace, "_session/steering")) == 1
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_without_the_extension_steer_and_queue_stay_unsupported(tmp_path: Path) -> None:
    driver, _, trace = await acp.started(tmp_path)
    script = {"during": [{"say": "working"}, acp.permission("hold", "execute")]}
    try:
        assert "steer" not in driver.capabilities
        prompt = await driver.execute(acp.prompt("owner", script))
        for action in ("steer", "queue"):
            with pytest.raises(DriverError) as refused:
                await driver.execute({**steer(prompt, action), "action": action})
            assert refused.value.code == "unsupported_action"
        with pytest.raises(DriverError) as busy:
            await driver.execute(
                {**acp.prompt("second", {}), "session_id": prompt["session_id"]}
            )
        assert busy.value.code == "conflict"
        assert acp.traced(trace, "_session/steering") == []
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_codex_style_reject_ends_the_prompt_by_its_own_stop_reason(tmp_path: Path) -> None:
    driver, events, _ = await acp.started(tmp_path)
    step = acp.permission("make", "execute", raw={"command": "make"}, reject="cancel")
    script = {"during": [{**step, "rejected": "cancel_turn"}, {"say": "never"}]}
    try:
        await driver.execute(acp.prompt("owner", script))
        _, waiting = await acp.until(events, lambda kind, value: kind == "permission")
        assert "reject_ends_prompt" not in waiting["payload"]
        await driver.execute(answer(waiting, "cancel", "owner"))
        _, completed = await acp.until(events, lambda kind, value: kind == "completed")
        assert completed["payload"]["status"] == "cancelled"
        assert completed["payload"]["native"] == {"stopReason": "cancelled"}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_gemini_style_reject_fails_only_that_tool(tmp_path: Path) -> None:
    driver, events, _ = await acp.started(tmp_path)
    tool = {"toolCallId": "shell", "title": "shell", "kind": "execute"}
    step = {"permission": {"toolCall": tool, "options": GEMINI_OPTIONS}, "rejected": "fail_tool"}
    script = {"during": [step, {"say": "carried on"}]}
    try:
        await driver.execute(acp.prompt("owner", script))
        _, waiting = await acp.until(events, lambda kind, value: kind == "permission")
        assert "reject_ends_prompt" not in waiting["payload"]
        await driver.execute(answer(waiting, "cancel", "owner"))
        _, failed = await acp.until(
            events,
            lambda kind, value: kind == "progress"
            and value["payload"].get("native", {}).get("sessionUpdate") == "tool_call_update",
        )
        assert failed["payload"]["native"]["status"] == "failed"
        assert failed["payload"]["native"]["toolCallId"] == "shell"
        _, completed = await acp.until(events, lambda kind, value: kind == "completed")
        assert completed["payload"]["status"] == "completed"
        assert completed["payload"]["text"] == "carried on"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_yuki_deny_of_an_unowned_wait_selects_the_agent_reject(tmp_path: Path) -> None:
    driver, events, trace = await acp.started(tmp_path)
    tool = {"toolCallId": "shell", "title": "shell", "kind": "execute"}
    script = {"during": [{"permission": {"toolCall": tool, "options": GEMINI_OPTIONS}}]}
    try:
        task = asyncio.create_task(driver.execute(acp.prompt("owner", script)))
        _, owned = await acp.until(events, lambda kind, value: kind == "permission")
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": owned["session_id"],
                "arguments": {"operation_id": "owner"},
            }
        )
        _, unowned = await acp.until(events, lambda kind, value: kind == "permission")
        assert unowned["payload"]["options"] == ["deny"]
        await driver.execute(answer(unowned, "deny", "owner"))
        await task
        [row] = await acp.traced_until(trace, "session/request_permission")
        assert row["outcome"] == {"outcome": "selected", "optionId": "cancel"}
    finally:
        await driver.stop()


def connector_for(tmp_path: Path, journal: Journal) -> Connector:
    return Connector(
        Config(
            "ws://localhost/",
            "device",
            "token",
            [
                Instance(
                    "gemini",
                    "acp",
                    "Gemini-like",
                    sys.executable,
                    args=[str(acp.AGENT)],
                    cwd=str(tmp_path),
                    env={
                        "FAKE_ACP_TRACE": str(tmp_path / "trace.jsonl"),
                        "FAKE_ACP_NO_LIST": "1",
                        "FAKE_ACP_MODES": acp.CODEX_MODES,
                    },
                )
            ],
        ),
        journal,
    )


async def running(connector: Connector) -> None:
    await connector.start_drivers()
    async with asyncio.timeout(5):
        # A driver is registered while it still starts (error "starting").
        while "gemini" not in connector.drivers or "gemini" in connector.errors:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def receipt(journal: Journal, key: str) -> dict[str, Any]:
    rows = journal.db.execute(
        "SELECT event FROM events WHERE json_extract(event, '$.kind')='receipt' "
        "AND json_extract(event, '$.operation_id')=? ORDER BY sequence",
        (key,),
    ).fetchall()
    return json.loads(rows[-1][0])["payload"]["result"]  # type: ignore[no-any-return]


async def finished(journal: Journal, key: str) -> None:
    async with asyncio.timeout(5):
        while journal.operation(key).get("status") != "completed":  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_resume_without_session_list_uses_the_cwd_recorded_at_creation(
    tmp_path: Path,
) -> None:
    project, work = tmp_path / "project", tmp_path / "project" / "work"
    work.mkdir(parents=True)
    trace = tmp_path / "trace.jsonl"
    journal = Journal(tmp_path / "private" / "journal.db", namespace="server/device")
    first = connector_for(tmp_path, journal)
    try:
        await running(first)
        assert "list" not in first.drivers["gemini"].capabilities
        await first.dispatch(
            {
                "type": "command",
                "id": "created",
                "instance_id": "gemini",
                "action": "prompt",
                "arguments": {"prompt": "{}", "cwd": str(project)},
            }
        )
        created = receipt(journal, "created")
        assert created["cwd"] == str(project)
        await finished(journal, "created")
    finally:
        await asyncio.gather(*(driver.stop() for driver in first.drivers.values()))
    # The agent process restarted: this owner no longer holds the session.
    second = connector_for(tmp_path, journal)
    try:
        await running(second)
        await second.dispatch(
            {
                "type": "command",
                "id": "resumed",
                "instance_id": "gemini",
                "action": "prompt",
                "session_id": created["session_id"],
                "arguments": {
                    "prompt": "{}",
                    "resume": True,
                    # A command-supplied cwd never replaces the session's own.
                    "cwd": str(tmp_path),
                    "scope": {"write_paths": [str(work)]},
                },
            }
        )
        resumed = receipt(journal, "resumed")
        assert "error" not in resumed, resumed
        assert resumed["session_id"] == created["session_id"]
        [loaded] = acp.traced(trace, "session/load")
        # The scoped load also carries the `ask` override (0.9.8, #650).
        assert loaded.pop("meta")["claudeCode"]["options"]["settings"]["permissions"]["ask"]
        assert loaded == {
            "method": "session/load",
            "cwd": str(project),
            "session": created["session_id"],
        }
        # The scope covers only project/work, not the cwd: an asking mode, never
        # workspace-write over the whole recorded cwd.
        [mode] = acp.traced(trace, "session/set_mode")
        assert mode["mode"] == "read-only"
        await finished(journal, "resumed")
        await second.dispatch(
            {
                "type": "command",
                "id": "unknown",
                "instance_id": "gemini",
                "action": "prompt",
                "session_id": "never-created-here",
                "arguments": {"prompt": "{}", "resume": True},
            }
        )
        assert receipt(journal, "unknown")["error"]["code"] == "cwd_unknown"
    finally:
        await asyncio.gather(*(driver.stop() for driver in second.drivers.values()))
        journal.close()
