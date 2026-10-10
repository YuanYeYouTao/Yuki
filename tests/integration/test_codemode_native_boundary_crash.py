"""Real Monty worker and Host process death; inert independently logged business IO."""

import asyncio
import json
import sys
from dataclasses import asdict

import pytest
from tests.integration.test_codemode_lifecycle_recovery import environment
from tests.integration.test_codemode_output_recovery import recreate
from tests.support.codemode_cases import (
    effect_rows,
    outer_call,
    requires_worker,
)

from qq_ai_bot.codemode.driver import CodeModeDriver
from qq_ai_bot.domain.messages import ChatMessage

pytestmark = requires_worker

CRASH_CHILD = r"""
import asyncio, json, os, sys
from tests.support.codemode_cases import build_host, outer_call
from qq_ai_bot.codemode.driver import CodeModeDriver
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript

url, lease_json, identity, chain_id, code, log, stop_tool = sys.argv[1:]

def record(value):
    with open(log, 'a') as downstream:
        downstream.write(json.dumps(value) + '\n')
        downstream.flush()
        os.fsync(downstream.fileno())

async def main():
    repository = WorkRepository(Database(url))
    lease = WorkLease(**json.loads(lease_json))
    current = await repository.get(identity)
    async def validate():
        assert await repository.valid(lease)
    control = WorkControl(repository, lease, current['source_key'],
                          json.loads(current['source_json']), validate)
    control.current = current
    owner = WorkSession(control, 'contract')
    control.session = owner
    owner.transcript = TurnTranscript((ChatMessage('system', 'test'),
                                      ChatMessage('user', 'compose')))
    owner.transcript.chain_id = chain_id
    async def domain(name, arguments):
        args = json.loads(arguments)
        record([name, args])
        return json.dumps({'ok': True, 'data': args})
    env = build_host(owner, domain)
    outer = outer_call(env, code)
    owner.transcript.append(ChatMessage('assistant', tool_calls=(outer.call,)))
    original = WorkRepository.record_effect
    async def save_then_die(self, key, state, receipt, **kwargs):
        await original(self, key, state, receipt, **kwargs)
        if state == 'accepted' and receipt.get('outcome', {}).get('tool') == stop_tool:
            record(['accepted', key])
            os._exit(91)
    WorkRepository.record_effect = save_then_die
    await CodeModeDriver(env.host, outer).run()
    raise AssertionError('the accepted receipt did not terminate the process')

asyncio.run(main())
"""


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
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    downstream = tmp_path / "independent-downstream.jsonl"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CRASH_CHILD,
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
