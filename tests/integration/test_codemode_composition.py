"""P05 Unit A: full-tool controlled calls through the real worker and Work path."""

import json

import pytest
from sqlalchemy.dialects.sqlite import insert
from tests.support.codemode_cases import effect_rows, environment, requires_worker, run_code

from qq_ai_bot.codemode.driver import CodeModeDriver
from qq_ai_bot.runtime.work_budget_schema import budgets

pytestmark = requires_worker


def ops(body):
    return [(item["tool"], item["status"]) for item in body["operations"]]


async def test_filtered_summary_omits_child_bodies_but_keeps_original_receipts(database, tmp_path):
    env = await environment(database, tmp_path, max_parallel=2)
    marker = "original-child-evidence-" * 400
    env.domain.replies["lookup"] = lambda args: {
        "ok": True,
        "data": {"rows": [{"id": args["q"], "amount": args["q"] * 10}], "raw": marker},
    }
    body, outer = await run_code(
        env,
        """
import asyncio
receipts = await asyncio.gather(*[yuki_lookup({'q': q}) for q in [1, 2, 3]])
rows = [row for r in receipts if r['ok'] for row in r['data']['rows'] if row['amount'] >= 20]
{'ids': [row['id'] for row in rows], 'total': sum(row['amount'] for row in rows),
 'evidence': [r['operation_id'] for r in receipts]}
""",
    )
    assert body["result"]["ids"] == [2, 3] and body["result"]["total"] == 50
    assert "original-child-evidence-" not in json.dumps(body)
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 3
    children = [row for key, row in rows.items() if key != outer.identity.operation_id]
    assert len(children) == 3 and all(
        marker in json.loads(row["receipt_json"])["result"] for row in children
    )
    assert len(json.dumps(body)) < min(len(row["receipt_json"]) for row in children) // 4
    assert env.domain.peak <= 2


async def test_mixed_script_preserves_order_barriers_and_bounded_parallel(database, tmp_path):
    env = await environment(database, tmp_path, max_parallel=2)
    code = """
import asyncio
a = await yuki_lookup({'q': 'a'})
b, c, d = await asyncio.gather(
    yuki_lookup({'q': 'b'}), yuki_search_chat_history({'q': 'c'}), yuki_lookup({'q': 'd'}))
w = await yuki_workspace_write({'path': 'x', 'text': a['data']['q']})
s = await yuki_send_message({'text': 'done'})
e = await yuki_lookup({'q': 'e'})
[a['status'], b['ok'], c['ok'], d['ok'], w['status'], s['status'], e['data']['q']]
"""
    body, _ = await run_code(env, code)
    assert body["status"] == "completed", body
    assert body["result"] == ["succeeded", True, True, True, "succeeded", "succeeded", "e"]
    names = [name for name, _ in env.domain.log]
    # The parallel stretch may complete in any order, but stays between its barriers.
    assert names[0] == "lookup" and set(names[1:4]) == {"lookup", "search_chat_history"}
    assert names[4:] == ["workspace_write", "send_message", "lookup"]
    assert env.domain.peak <= 2  # C01: bounded by the Host, not by the script.
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    # B01: composition is not a business call; each real child is admitted once.
    assert tools == root == 7 == body["usage"]["business_admitted"]
    assert sum(row["kind"] == "code_composition" for row in rows.values()) == 1


async def test_two_equal_argument_sends_are_two_original_operations(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "a = await yuki_send_message({'text': 'same'})\n"
        "b = await yuki_send_message({'text': 'same'})\n"
        "[a['operation_id'], b['operation_id']]",
    )
    first, second = body["result"]
    assert first != second  # D01: identity is the Host ordinal, never the arguments.
    assert env.domain.log == [("send_message", {"text": "same"})] * 2


async def test_reentry_of_the_same_outer_call_returns_original_receipt(database, tmp_path):
    env = await environment(database, tmp_path)
    body, outer = await run_code(env, "await yuki_send_message({'text': 'once'})")
    again = json.loads(await CodeModeDriver(env.host, outer).run())
    assert again == body
    assert env.domain.log == [("send_message", {"text": "once"})]
    _, tools, _ = await effect_rows(database, env.control.current["id"])
    assert tools == 1  # B02: reading the original receipt is not a new admission.


async def test_unknown_child_stops_the_script_and_is_never_resent(database, tmp_path):
    env = await environment(database, tmp_path)
    env.domain.replies["send_message"] = {"ok": False, "uncertain": True, "error": "timeout"}
    body, outer = await run_code(
        env,
        "s = await yuki_send_message({'text': 'x'})\n"
        "await yuki_send_message({'text': 'after-unknown'})\n",
    )
    assert body["status"] == "partial" and body["stop_reason"] == "unknown_effect"
    assert env.domain.log == [("send_message", {"text": "x"})]
    again = json.loads(await CodeModeDriver(env.host, outer).run())
    assert again == body  # Settled partial: never resumes the VM.
    assert len(env.domain.log) == 1


async def test_catching_a_closing_denial_cannot_continue_side_effects(database, tmp_path):
    env = await environment(database, tmp_path)
    env.domain.denied.add("workspace_write")
    body, _ = await run_code(
        env,
        "try:\n"
        "    r = await yuki_workspace_write({'path': 'p'})\n"
        "except Exception:\n"
        "    pass\n"
        "await yuki_send_message({'text': 'sneaky'})\n",
    )
    # C05: the Host closed admission; the catch never reached the send.
    assert body["stop_reason"] == "admission_closed"
    assert [name for name, _ in env.domain.log] == []


async def test_ordinary_business_failure_is_a_receipt_the_script_handles(database, tmp_path):
    env = await environment(database, tmp_path)
    env.domain.replies["lookup"] = {"ok": False, "error": "not_found", "executed": True}
    body, _ = await run_code(
        env,
        "r = await yuki_lookup({'q': 1})\n[r['ok'], r['status'], r['error']['code']]",
    )
    assert body["result"] == [False, "failed", "not_found"]


async def test_permission_revoked_mid_script_closes_admission(database, tmp_path):
    env = await environment(database, tmp_path)
    original = env.domain.__call__

    async def revoke_after_first(name, arguments):
        if env.domain.log:
            env.domain.denied.add("send_message")
        return await original(name, arguments)

    env.host.execute_business = _wrap(env, revoke_after_first)
    body, _ = await run_code(
        env,
        "await yuki_send_message({'text': 'one'})\n"
        "await yuki_send_message({'text': 'two'})\n"
        "await yuki_send_message({'text': 'three'})\n",
    )
    assert body["stop_reason"] == "admission_closed"
    assert env.domain.log == [("send_message", {"text": "one"})]


def _wrap(env, domain):
    from qq_ai_bot.services.invocation_service import InvocationService

    service = InvocationService()

    async def execute_business(invocation, side_effecting):
        async def invoke():
            return await domain(invocation.call.function.name, invocation.call.function.arguments)

        return await service.invoke(invocation, invoke, side_effecting=side_effecting)

    return execute_business


async def test_root_budget_rejection_stops_without_dispatch(database, tmp_path):
    env = await environment(database, tmp_path)
    identity = env.control.current["id"]
    async with database.sessions() as writer, writer.begin():
        await writer.execute(insert(budgets).values(root_id=identity, tool_limit=1))
    body, _ = await run_code(
        env,
        "await yuki_lookup({'q': 1})\nawait yuki_lookup({'q': 2})\n",
    )
    assert body["status"] == "partial", body
    assert env.domain.log == [("lookup", {"q": 1})]
    _, tools, root = await effect_rows(database, identity)
    assert tools == root == 1  # B04: atomic root limit; rejection admits nothing.


async def test_segment_allowance_yields_and_resumes_same_composition(database, tmp_path):
    env = await environment(database, tmp_path, tool_limit=1)
    code = "a = await yuki_lookup({'q': 1})\nb = await yuki_lookup({'q': 2})\n[a['ok'], b['ok']]"
    from tests.support.codemode_cases import outer_call

    from qq_ai_bot.codemode.driver import CodeCompositionYield

    outer = outer_call(env, code)
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    assert env.domain.log == [("lookup", {"q": 1})]
    # B05: the next segment continues the same script and the same undispatched child.
    env.control.tools_started = 0
    body = json.loads(await CodeModeDriver(env.host, outer).run())
    assert body["status"] == "completed" and body["result"] == [True, True]
    assert env.domain.log == [("lookup", {"q": 1}), ("lookup", {"q": 2})]
    _, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 2  # Root counts accumulate; nothing re-charged.


async def test_wrapper_misuse_is_a_script_error_not_a_business_call(database, tmp_path):
    env = await environment(database, tmp_path)
    body, _ = await run_code(
        env,
        "try:\n    await yuki_lookup('not-a-dict')\nexcept ValueError as e:\n    r = str(e)\nr",
    )
    assert body["status"] == "completed" and "dict" in body["result"]
    assert env.domain.log == []


async def test_script_cannot_name_an_undeclared_or_host_path(database, tmp_path):
    env = await environment(database, tmp_path)
    for probe in (
        "__yuki_invoke('send_message', {})",
        "yuki_execute_code({'code': '1'})",
        "host.call('db.execute')",
    ):
        body, _ = await run_code(env, probe, call_id=f"probe-{len(probe)}")
        assert body["status"] == "failed" and "NameError" in body["detail"], body
    assert env.domain.log == []


async def test_oversized_program_result_is_a_bounded_view(database, tmp_path):
    env = await environment(database, tmp_path)
    archived = []

    async def archive(text):
        archived.append(text)
        return "artifact-1"

    env.host.archive = archive
    body, _ = await run_code(env, "['x' * 100] * 200")
    assert body["complete"] is False and body["result_ref"] == "artifact-1"
    assert json.loads(archived[0]) == ["x" * 100] * 200  # The full value, once.
    assert len(body["result_preview"]) < len(archived[0])
