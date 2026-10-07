"""P05 Unit B: Host control gate, memory observation and original-Work yield."""

import asyncio
import json
import sys
from dataclasses import asdict

import pytest
from tests.support.codemode_cases import (
    BINARY,
    effect_rows,
    environment,
    outer_call,
    requires_worker,
    run_code,
)

from qq_ai_bot.codemode.driver import CodeModeDriver

pytestmark = requires_worker


async def test_terminal_control_stops_remaining_code(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "await yuki_lookup({'q': 1})\n"
        "c = await yuki_task_control({'action': 'need_input', 'reason': 'which file?'})\n"
        "await yuki_send_message({'text': 'never'})\n",
    )
    assert body["stop_reason"] == "host_control" and body["ok"] is True
    assert env.control.ending == "waiting_user"
    assert [name for name, _ in env.domain.log] == ["lookup"]
    _, tools, _ = await effect_rows(database, env.control.current["id"])
    assert tools == 1  # Lifecycle control does not use the business allowance.


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
        "await yuki_send_message({'text': 'never'})\n",
    )
    # C07/T04: the read settled before the control gate opened; nothing ran after it.
    assert order == [("control", ["lookup"])]
    assert body["stop_reason"] == "host_control"
    assert [name for name, _ in env.domain.log] == ["lookup"]


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


async def test_memory_write_returns_to_model_and_blocks_later_send(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "m = await yuki_memory_change({'op': 'remember'})\n"
        "await yuki_send_message({'text': 'unobserved'})\n",
    )
    assert body["stop_reason"] == "memory_observation_required"
    assert env.domain.log == [("memory_change", {"op": "remember"})]


async def test_memory_and_send_in_one_step_never_dispatch_the_send(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "import asyncio\n"
        "m, s = await asyncio.gather(yuki_memory_change({'op': 'r'}),"
        " yuki_send_message({'text': 'x'}))\n",
    )
    assert body["stop_reason"] == "memory_observation_required"
    assert [name for name, _ in env.domain.log] == ["memory_change"]
    send = next(op for op in body["operations"] if op["tool"] == "send_message")
    assert send["status"] == "not_executed"


async def test_memory_after_another_side_effect_runs_and_returns_to_model(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "await yuki_send_message({'text': 'first'})\n"
        "m = await yuki_memory_change({'op': 'r'})\n"
        "m['error']['code']",
    )
    assert body["stop_reason"] == "memory_observation_required"
    assert [name for name, _ in env.domain.log] == ["send_message", "memory_change"]


async def test_new_input_settles_partial_and_stops(database, tmp_path, monkeypatch):
    env = await environment(database, tmp_path)
    calls = 0
    from qq_ai_bot.runtime.work_control import WorkControl

    original = WorkControl.pending

    async def pending(self):
        return [{"ready": True}] if calls else await original(self)

    monkeypatch.setattr(WorkControl, "pending", pending)

    async def domain(name, arguments):
        nonlocal calls
        calls += 1
        return await env.domain(name, arguments)

    from tests.integration.test_codemode_composition import _wrap

    env.host.execute_business = _wrap(env, domain)
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
        "-m",
        "tests.support.codemode_crash_child",
        str(BINARY),
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
    env.domain.replies["send_message"] = {
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
        "await yuki_send_message({'text': 'first'})\nawait yuki_send_message({'text': 'second'})",
    )
    assert len(env.domain.log) == (1 if uncertain else 2)
    assert body["operations"][0]["status"] == ("unknown" if uncertain else "succeeded")
    assert body["status"] == ("partial" if uncertain else "completed")


async def test_unknown_memory_effect_closes_before_observation_success(database, tmp_path):
    env = await environment(database, tmp_path)
    env.domain.replies["memory_change"] = {"ok": False, "uncertain": True}
    body, _ = await run_code(
        env,
        "await yuki_memory_change({'op': 'remember'})\n"
        "await yuki_send_message({'text': 'must not send'})",
    )
    assert body["stop_reason"] == "unknown_effect"
    assert body["ok"] is False and body["operations"][0]["status"] == "unknown"
    assert [name for name, _ in env.domain.log] == ["memory_change"]
