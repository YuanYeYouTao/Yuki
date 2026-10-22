"""Host controls preserve original order, receipts and valid program tails."""

import asyncio
import json
import sys
from dataclasses import asdict

import pytest
from tests.support.codemode_cases import (
    effect_rows,
    environment,
    outer_call,
    requires_worker,
    run_code,
)

from yuki.codemode.driver import CodeModeDriver

pytestmark = [requires_worker, pytest.mark.native_worker]

CRASH_CHILD = r"""
import asyncio, json, os, sys
from tests.support.codemode_cases import build_host, outer_call
from yuki.persistence.database import Database
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkLease, WorkRepository
from yuki.runtime.work_session import WorkSession
from yuki.services.turn_transcript import TurnTranscript
from yuki.domain.messages import ChatMessage
from yuki.codemode.driver import CodeModeDriver

url, lease_json, identity, chain_id, arguments, log = sys.argv[1:]

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
    owner.transcript = TurnTranscript((ChatMessage('user', 'compose'),))
    owner.transcript.chain_id = chain_id
    calls = 0
    async def domain(name, arguments):
        nonlocal calls
        calls += 1
        args = json.loads(arguments)
        with open(log, 'a') as downstream:
            downstream.write(json.dumps([name, args]) + '\n')
            downstream.flush()
            os.fsync(downstream.fileno())
        if calls == 2:
            os._exit(9)
        return json.dumps({'ok': True, 'data': args})
    env = build_host(owner, domain)
    outer = outer_call(env, json.loads(arguments)['code'])
    await CodeModeDriver(env.host, outer).run()
    raise AssertionError('the second real dispatch did not terminate the process')

asyncio.run(main())
"""


@pytest.mark.parametrize(
    "args,ending",
    [
        ({"action": "complete", "result": "internal result"}, "completed"),
        ({"action": "need_input", "reason": "which file?"}, "waiting_user"),
        (
            {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 60}]},
            "waiting_external",
        ),
    ],
)
async def test_lifecycle_proposal_keeps_tools_calculation_and_cleanup(
    database, tmp_path, args, ending
):
    from tests.integration.test_codemode_lifecycle_recovery import (
        environment as sourced_environment,
    )

    env = await sourced_environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "await yuki_lookup({'q': 1})\n"
        f"c = await yuki_task_control({args!r})\n"
        "try:\n"
        "    s = await yuki_send({'text': 'after control'})\n"
        "    value = 6 * 7\n"
        "finally:\n"
        "    await yuki_workspace_write({'path': 'cleanup'})\n"
        "    print('cleanup complete')\n"
        "[c['ok'], s['ok'], value]",
    )
    assert body["status"] == "completed" and body["result"] == [True, True, 42]
    assert body["stdout"] == "cleanup complete\n"
    assert env.control.ending == ending
    assert [name for name, _ in env.domain.log] == ["lookup", "send", "workspace_write"]
    _, tools, _ = await effect_rows(database, env.control.current["id"])
    assert tools == 3  # Lifecycle control does not use the business allowance.


@pytest.mark.parametrize(
    "status,action,ending",
    [
        ("succeeded", "complete", "completed"),
        ("failed", "complete", "completed"),
        ("uncertain", "complete", "completed"),
        ("uncertain", "fail", "failed"),
        ("uncertain", "cancel", "cancelled"),
    ],
)
async def test_send_terminal_control_and_write_are_paired_in_original_order(
    database, tmp_path, status, action, ending
):
    env = await environment(database, tmp_path)
    env.domain.replies["send"] = {"ok": status == "succeeded", "data": {"status": status}}
    body, _ = await run_code(
        env,
        "import asyncio\n"
        "await asyncio.gather(yuki_send({'text': 'before complete'}), "
        f"yuki_task_control({{'action': {action!r}}}), "
        "yuki_workspace_write({'path': 'never'}))",
    )
    assert env.control.ending == ending
    if action == "complete":
        assert body["status"] == "completed"
        assert [name for name, _ in env.domain.log] == ["send", "workspace_write"]
        assert body["operations"][-1]["status"] == "succeeded"
    else:
        assert body["stop_reason"] == "admission_closed"
        assert [name for name, _ in env.domain.log] == ["send"]
        assert body["operations"][-1]["status"] == "not_executed"
    await env.control.settle(pending_inputs=False)
    assert env.control.current["state"] == ending


async def test_control_waits_for_peers_to_settle_then_runs_alone(database, tmp_path):
    env = await environment(database, tmp_path)
    order = []
    original = env.host.execute_control

    async def tracked(call, key):
        order.append(("control", [name for name, _ in env.domain.log]))
        return await original(call, key)

    env.host.execute_control = tracked
    body, _ = await run_code(
        env,
        "import asyncio\n"
        "r, c = await asyncio.gather(yuki_lookup({'q': 1}),"
        " yuki_task_control({'action': 'need_input', 'reason': 'x'}))\n"
        "await yuki_send({'text': 'after need_input'})\n",
    )
    # The read settles before the control barrier; the valid tail then continues.
    assert order == [("control", ["lookup"])]
    assert body["status"] == "completed"
    assert [name for name, _ in env.domain.log] == ["lookup", "send"]


async def test_query_controls_keep_original_paging_and_continue(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "l = await yuki_task_control({'action': 'list', 'status': 'all', 'limit': 2})\n"
        "[l['ok'], 'items' in l['data'] or 'works' in l['data'] or l['data'] is not None]",
    )
    assert body["status"] == "completed", body
    assert body["result"][0] is True
    assert env.controls and json.loads(env.controls[0][0])["limit"] == 2


async def test_memory_write_receipt_allows_later_send_in_same_program(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "m = await yuki_memory_change({'op': 'remember'})\n"
        "await yuki_send({'text': 'unobserved'})\n",
    )
    assert body["status"] == "completed"
    assert env.domain.log == [
        ("memory_change", {"op": "remember"}),
        ("send", {"text": "unobserved"}),
    ]


async def test_memory_and_send_in_one_step_follow_original_order(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "import asyncio\n"
        "m, s = await asyncio.gather(yuki_memory_change({'op': 'r'}),"
        " yuki_send({'text': 'x'}))\n",
    )
    assert body["status"] == "completed"
    assert [name for name, _ in env.domain.log] == ["memory_change", "send"]
    send = next(op for op in body["operations"] if op["tool"] == "send")
    assert send["status"] == "succeeded"


async def test_memory_after_another_side_effect_runs_and_returns_original_receipt(
    database, tmp_path
):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "await yuki_send({'text': 'first'})\n"
        "m = await yuki_memory_change({'op': 'r'})\n"
        "m['ok']",
    )
    assert body["status"] == "completed" and body["result"] is True
    assert [name for name, _ in env.domain.log] == ["send", "memory_change"]


async def test_new_input_settles_partial_and_stops(database, tmp_path, monkeypatch):
    env = await environment(database, tmp_path)
    calls = 0
    from yuki.runtime.work_control import WorkControl

    original = WorkControl.has_pending_business_inputs

    async def pending(self):
        return bool(calls) or await original(self)

    monkeypatch.setattr(WorkControl, "has_pending_business_inputs", pending)

    original_business = env.host.execute_business

    async def execute_business(invocation, side_effecting):
        nonlocal calls
        result = await original_business(invocation, side_effecting)
        calls += 1
        return result

    env.host.execute_business = execute_business
    body, _ = await run_code(
        env,
        "await yuki_lookup({'q': 1})\nawait yuki_workspace_write({'path': 'stale-goal'})\n",
    )
    assert body["stop_reason"] == "new_input" and body["status"] == "partial"
    assert [name for name, _ in env.domain.log] == ["lookup"]


async def test_hard_kill_mid_script_resumes_without_redispatch(database, tmp_path):
    """F05/F06 with a real child process and an independent downstream log."""
    env = await environment(database, tmp_path)
    code = (
        "a = await yuki_lookup({'q': 1})\n"
        "b = await yuki_lookup({'q': 2})\n"
        "c = await yuki_lookup({'q': 3})\n"
        "[a['status'], b['status'], c['status']]"
    )
    outer = outer_call(env, code)
    log = tmp_path / "downstream.log"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CRASH_CHILD,
        database.url,
        json.dumps(asdict(env.control.lease)),
        env.control.current["id"],
        env.owner.transcript.chain_id,
        json.dumps({"code": code}),
        str(log),
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 9, stderr.decode()[-2000:]
    downstream = [json.loads(line) for line in log.read_text().splitlines()]
    assert downstream == [["lookup", {"q": 1}], ["lookup", {"q": 2}]]

    # Restart: the original owner resumes the same composition (no new script).
    rows, tools, _ = await effect_rows(database, env.control.current["id"])
    parent = rows[outer.identity.operation_id]
    assert parent["kind"] == "code_composition" and parent["state"] == "prepared"
    body = json.loads(await CodeModeDriver(env.host, outer).resume())
    # The child whose dispatch was admitted before the kill is unknown, never resent.
    assert body["stop_reason"] == "unknown_effect", body
    assert env.domain.log == []  # The restarted Host dispatched nothing new.
    _, after, _ = await effect_rows(database, env.control.current["id"])
    assert after == tools == 2


@pytest.mark.parametrize("uncertain", [True, False])
async def test_code_host_closing_uses_typed_child_fact_not_display(database, tmp_path, uncertain):
    env = await environment(database, tmp_path)
    env.domain.replies["send"] = {
        "ok": not uncertain,
        "uncertain": uncertain,
        "mutation_committed": not uncertain,
    }
    original = env.host.execute_business

    async def misleading_display(invocation, side_effecting):
        await original(invocation, side_effecting)
        return json.dumps({"ok": uncertain, "uncertain": not uncertain, "executed": False})

    env.host.execute_business = misleading_display
    body, _ = await run_code(
        env,
        "await yuki_send({'text': 'first'})\nawait yuki_send({'text': 'second'})",
    )
    assert len(env.domain.log) == 2
    assert body["operations"][0]["status"] == ("unknown" if uncertain else "succeeded")
    assert body["status"] == "completed"


async def test_unknown_memory_receipt_is_preserved_and_does_not_veto_complete(database, tmp_path):
    env = await environment(database, tmp_path)
    env.domain.replies["memory_change"] = {"ok": False, "uncertain": True}
    body, _ = await run_code(
        env,
        "await yuki_memory_change({'op': 'remember'})\n"
        "await yuki_task_control({'action': 'complete'})\n"
        "await yuki_send({'text': 'after complete'})",
    )
    assert body["status"] == "completed" and body["ok"] is True
    assert body["operations"][0]["status"] == "unknown"
    assert [name for name, _ in env.domain.log] == ["memory_change", "send"]
