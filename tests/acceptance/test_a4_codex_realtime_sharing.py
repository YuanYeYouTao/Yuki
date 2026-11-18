"""A4 (taskbook §10.3, D7): a Codex TUI and Yuki share one live thread.

The TUI (``codex --remote`` in ``tmux -L p2acc``) and Yuki's connector are two
clients of OUR app-server on the same thread. Facts recorded: Yuki's inputs enter by
``thread/queue/add`` (never ``turn/start``) and alternate with TUI turns; a Yuki input
sent while a TUI turn runs is queued and starts after it; both sides see each other's
turns (thread/read and the TUI screen); Yuki answers a request of its own turn; a
request of a TUI turn is only a terminal fact: answering it through Yuki returns
``not_owner`` and the person answers in the TUI.
Artifact: ``test-results/external-agent-phase2-acceptance/A4.json``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.acceptance.harness import (
    Facts,
    Stack,
    StateRecorder,
    by_local,
    call,
    codex_instance,
    codex_items,
    codex_rpc,
    codex_tui_prompt,
    journal_commands,
    loaded_threads,
    now,
    project,
    record_codex_methods,
    run_root,
    say,
    started,
    terminal_codex,
    tmux,
    tmux_screen,
    tmux_send,
    tui_thread,
    until,
    write_artifact,
)
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent

ESCALATE = (
    "Run the shell command echo {tag}, requesting escalated sandbox permissions "
    "(require_escalated) so that it must be approved."
)


async def requests_of(stack: Stack, record: str) -> list[dict[str, Any]]:
    async with stack.database.read_session() as reader:
        rows = (
            await reader.execute(
                select(
                    AgentEventModel.id,
                    AgentEventModel.payload_json,
                    AgentEventModel.operation_id,
                    AgentEventModel.created_at,
                )
                .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
                .where(
                    AgentOperationModel.work_id == record,
                    AgentEventModel.kind.in_(("permission", "question")),
                )
                .order_by(AgentEventModel.id)
            )
        ).all()
    result = []
    for identity, payload_json, operation, created in rows:
        payload = json.loads(payload_json)
        inner = payload.get("payload") or {}
        result.append(
            {
                "permission_id": str(identity),
                "operation_id": operation,
                "execution_id": payload.get("execution_id"),
                "answer_at": inner.get("answer_at"),
                "command_head": str((inner.get("native_request") or {}).get("command", "")).split()[
                    :1
                ],
                "received_at": created.isoformat(),
            }
        )
    return result


async def thread(native: str) -> dict[str, Any]:
    (read,) = await codex_rpc([("thread/read", {"threadId": native, "includeTurns": True})])
    return read.get("thread") or {}


async def test_a4_tui_and_yuki_alternate_and_queue_on_one_thread(tmp_path, monkeypatch):
    facts = Facts()
    methods: list[dict[str, Any]] = []
    record_codex_methods(monkeypatch, methods, facts.started)
    stack = Stack(run_root("test_a4_codex_realtime_sharing"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    artifact: dict[str, Any] = {"scenario": "A4", "status": "incomplete"}
    cwd = project("a4")
    state: dict[str, Any] = {
        "record": None,
        "queue": [],
        "handled": set(),
        "told": [],
        "results": {},
        "last_call": None,
    }
    try:
        before = await loaded_threads()
        await terminal_codex("a4-tui", cwd)
        await codex_tui_prompt("a4-tui", "Reply with just the word tui-1.")

        native = await tui_thread("a4-tui", before, cwd)

        async def idle():
            turns = (await thread(native)).get("turns") or []
            return bool(turns) and turns[-1].get("status") == "completed" and turns

        await until(idle, 120, 2)
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [codex_instance("codex-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["codex-1"]
        session = (
            await stack.runtime.external_agents.repository.remember_sessions(
                agent, stack.state["owner"], [native]
            )
        )[native]

        async def decide(turn):
            if "A4-" not in turn.trigger_text and turn.work_id != state.get("parent"):
                return None
            if turn.work_id is None:
                return call("manage_task", {"action": "start", "goal": "A4 shared thread"})
            state["parent"] = turn.work_id
            tool = turn.last_tool()
            if tool is not None and state.get("last_call"):
                state["results"][state["last_call"]] = tool
                state["last_call"] = None
            if started(tool):
                state["record"] = tool["id"]
                recorder.watch(tool["id"])
                facts.mark("delegated", work_id=tool["id"], dispatch=tool.get("dispatch"))
            record = state["record"]
            if record is None:
                return call(
                    "start_subtask",
                    {
                        "goal": "Reply with just the word yuki-1.",
                        "executor": {"agent": agent},
                        "session": session,
                    },
                )
            for request in await requests_of(stack, record):
                if request["permission_id"] in state["handled"]:
                    continue
                pending = {p["permission_id"] for p in (await stack.card(record))["pending"]}
                if request["permission_id"] not in pending:
                    continue
                state["handled"].add(request["permission_id"])
                if request["answer_at"] == "native_terminal":
                    state["told"].append(request["permission_id"])
                    facts.mark("told_terminal_request", permission_id=request["permission_id"])
                    return say("这是你在终端里那一轮的请求，请在终端处理。")
                facts.mark("yuki_answers_own_turn", permission_id=request["permission_id"])
                return call(
                    "message_task",
                    {
                        "id": record,
                        "reply_to": request["permission_id"],
                        "answer": "accept",
                        "text": "Yuki 自己这一轮的请求",
                    },
                )
            if state["queue"]:
                queued = state["queue"].pop(0)
                state["last_call"] = queued.tool_calls[0].function.name
                return queued
            row = await stack.work_row(record)
            waits: list[dict[str, Any]] = [{"message": True}]
            if row["state"] not in TERMINAL:
                waits.append({"run": record})
            return call("wait", {"for": waits})

        stack.decide = decide
        await stack.group(42, "!ai A4-START 帮我在 Codex 那个线程里也说一句")

        async def record_done():
            record = state["record"]
            return record and (await stack.work_row(record))["state"] in TERMINAL

        await until(record_done, 180, 1)
        facts.mark("yuki_turn_1_done")

        async def ops_done(count: int):
            ops = [
                op
                for op in await stack.operations(state["record"])
                if op["action"] in ("prompt", "queue")
            ]
            return len(ops) >= count and all(op["pending"] is False for op in ops[:count])

        async def tool_result_of(name: str) -> dict[str, Any] | None:
            await asyncio.sleep(3)
            return state["results"].get(name)

        async def yuki(tool_call: Any, tag: str) -> None:
            state["queue"].append(tool_call)
            await stack.group(42, f"!ai {tag} 再让 Codex 做一件事")

        async def thread_idle():
            turns = (await thread(native)).get("turns") or []
            return turns and turns[-1].get("status") == "completed"

        async def tui_running():
            turns = (await thread(native)).get("turns") or []
            return turns and turns[-1].get("status") == "inProgress"

        # B. A TUI turn runs. Yuki's record has ended: its follow-up cannot queue behind
        #    the TUI turn (message_task -> refused; manage_task resume -> connector
        #    `conflict`, run 1). Recorded as facts.
        await codex_tui_prompt(
            "a4-tui", "Run the shell command `sleep 20`, then reply with just the word tui-2."
        )
        await until(tui_running, 60, 0.5)
        facts.mark("tui_turn_2_running")
        await yuki(
            call(
                "message_task", {"id": state["record"], "text": "Reply with just the word yuki-x."}
            ),
            "A4-BEHIND-TUI",
        )
        artifact["follow_up_behind_tui_turn"] = await tool_result_of("message_task")
        await until(thread_idle, 120, 1)
        facts.mark("tui_turn_2_done")

        # C. Yuki's own long turn runs (record alive); the person types in the TUI
        #    meanwhile, then Yuki queues one more input on its live record.
        await yuki(
            call(
                "manage_task",
                {
                    "action": "resume",
                    "id": state["record"],
                    "text": "Run the shell command `sleep 20`, then reply "
                    "with just the word yuki-3.",
                },
            ),
            "A4-YUKI-LONG",
        )

        async def yuki_turn_running():
            ops = [op for op in await stack.operations(state["record"]) if op["action"] == "prompt"]
            live = [op for op in ops[1:] if op["pending"] is not False and op["execution_id"]]
            return bool(live) and await tui_running()

        await until(yuki_turn_running, 90, 0.5)
        facts.mark("yuki_turn_3_running")
        # Tab queues the person's input as its own turn (Enter would steer it into
        # Yuki's running turn; run 2 recorded that case: A4-run2-tui-steer-merged.json).
        await codex_tui_prompt("a4-tui", ESCALATE.format(tag="tui-4"), key="Tab")
        facts.mark("tui_queued_during_yuki_turn")
        await asyncio.sleep(2)
        await yuki(
            call(
                "message_task", {"id": state["record"], "text": "Reply with just the word yuki-5."}
            ),
            "A4-QUEUE",
        )
        artifact["queue_on_live_record"] = await tool_result_of("message_task")

        # D. The TUI's own request (its turn, not Yuki's): never answerable by Yuki.
        async def tui_request():
            rows = [
                r
                for r in await requests_of(stack, state["record"])
                if r["answer_at"] == "native_terminal"
            ]
            return rows[0] if rows else None

        tui_req = None
        try:
            tui_req = await until(tui_request, 120, 1)
        except TimeoutError:
            facts.mark("tui_request_not_delivered_to_record")
        refusal = None
        if tui_req is not None:
            response = await stack.http_client.post(
                f"/client/v1/works/{state['record']}/native-requests/"
                f"{tui_req['permission_id']}/answer",
                headers=stack.client_headers,
                json={"request_id": str(uuid4()), "option_id": "accept"},
            )
            refusal = {
                "http": response.status_code,
                "body": response.json().get("data") or response.json().get("problem"),
            }
            await asyncio.sleep(5)
            ops = await stack.operations(state["record"])
            refusal["respond_operations"] = [op for op in ops if op["action"] == "respond"]
        tui_screen_before_answer = "Would you like to run" in tmux_screen("a4-tui")
        if tui_screen_before_answer:
            tmux_send("a4-tui", "y")
            facts.mark("person_answered_in_tui")
        await until(thread_idle, 240, 2)
        await asyncio.sleep(5)
        await until(thread_idle, 240, 2)
        facts.mark("thread_idle_end")

        # E. A request in Yuki's own turn: Yuki answers it (decide answers it).
        await yuki(
            call(
                "manage_task",
                {"action": "resume", "id": state["record"], "text": ESCALATE.format(tag="yuki-6")},
            ),
            "A4-OWN",
        )

        answered_before = len(
            [op for op in await stack.operations(state["record"]) if op["action"] == "respond"]
        )

        async def own_answered():
            ops = await stack.operations(state["record"])
            return (
                len([op for op in ops if op["action"] == "respond" and op["error"] is None])
                > answered_before
            )

        try:
            await until(own_answered, 180, 1)
        except TimeoutError:
            facts.mark("own_request_not_answered")
        await until(thread_idle, 180, 2)
        facts.mark("yuki_turn_6_done")
        await asyncio.sleep(3)
        final_thread = await thread(native)
        items = codex_items(final_thread)
        screen = tmux_screen("a4-tui")
        ops = await stack.operations(state["record"])
        journal = journal_commands(stack.root / "connectors" / "mac-a" / "journal.sqlite3")
        artifact.update(
            status="completed",
            agents={
                "codex": "codex-cli 0.169 (TUI --remote + connector via app-server proxy, "
                "own app-server, gpt-6-luna low)"
            },
            thread_id=native,
            record={
                "work_id": state["record"],
                "final": {
                    k: v
                    for k, v in (await stack.work_row(state["record"])).items()
                    if k in ("state", "reason")
                },
                "operations": ops,
                "events": await stack.events(state["record"]),
                "requests": await requests_of(stack, state["record"]),
                "state_sequence": recorder.of(state["record"]),
            },
            thread_turns=[
                {
                    "turn_id": t.get("id"),
                    "status": t.get("status"),
                    "user_items": [
                        i.get("clientId")
                        for i in t.get("items") or []
                        if i.get("type") == "userMessage"
                    ],
                }
                for t in final_thread.get("turns") or []
            ],
            thread_items=items,
            tui_sees_yuki_turns={tag: tag in screen for tag in ("yuki-1", "yuki-3", "yuki-5")},
            tui_request_refusal=refusal,
            tui_request_visible_in_tui=tui_screen_before_answer,
            told_terminal_requests=state["told"],
            journal_actions=[
                {"action": r["action"], "receipt_status": r["receipt_status"]} for r in journal
            ],
            marks=facts.marks,
            t_end=round(now() - facts.started, 3),
        )
        artifact["connector_app_server_methods"] = methods
        assert not [m for m in methods if m["method"] == "turn/start"], methods
        assert any(m["method"] == "thread/queue/add" for m in methods), methods
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A4", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await stack.stop()
        if "a4-tui" in tmux("ls"):
            tmux("kill-session", "-t", "a4-tui")
