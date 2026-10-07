"""Real Monty worker and Host process death; inert independently logged business IO."""

import asyncio
import json
import sys
from dataclasses import asdict

import pytest
from tests.integration.test_codemode_output_recovery import recreate
from tests.support.codemode_cases import (
    BINARY,
    effect_rows,
    environment,
    outer_call,
    requires_worker,
)

from qq_ai_bot.codemode.driver import CodeModeDriver

pytestmark = requires_worker


@pytest.mark.parametrize("stop_tool", ["task_control", "lookup"])
async def test_host_death_after_accepted_receipt_restores_original_boundary(
    database, tmp_path, stop_tool
):
    env = await environment(database, tmp_path)
    env.audit_native = True
    code = (
        "await yuki_task_control({'action':'need_input','reason':'Which file?'})\n"
        "await yuki_workspace_write({'path':'must-not-execute'})"
        if stop_tool == "task_control"
        else "print('DURABLE_STDOUT_ONCE')\nawait yuki_lookup({'q':1})\nawait yuki_lookup({'q':2})"
    )
    outer = outer_call(env, code)
    downstream = tmp_path / "independent-downstream.jsonl"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.codemode_boundary_crash",
        str(BINARY),
        database.url,
        json.dumps(asdict(env.control.lease)),
        env.control.current["id"],
        env.owner.transcript.chain_id,
        code,
        str(downstream),
        stop_tool,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 91, stderr.decode()[-3000:]
    log = [json.loads(line) for line in downstream.read_text().splitlines()]
    assert sum(row[0] == "accepted" for row in log) == 1
    env = await recreate(env, outer)
    outer = env.outer
    body = json.loads(await CodeModeDriver(env.host, outer).resume())
    if stop_tool == "task_control":
        assert env.domain.log == [] and len(log) == 1
        assert env.control.ending == "waiting_user" and body["stop_reason"] == "host_control"
    else:
        assert [row for row in log if row[0] != "accepted"] == [["lookup", {"q": 1}]]
        assert env.domain.log == [("lookup", {"q": 2})]
        assert body["stdout"] == "DURABLE_STDOUT_ONCE\n" and body["status"] == "completed"
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert rows[outer.identity.operation_id]["state"] == "accepted"
    assert tools == (root or 0) == (2 if stop_tool == "lookup" else 0)
    assert json.loads(await CodeModeDriver(env.host, outer).resume())["stdout"] == body["stdout"]
    assert len(env.domain.log) == (1 if stop_tool == "lookup" else 0)
