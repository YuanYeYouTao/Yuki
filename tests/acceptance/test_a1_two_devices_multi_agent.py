"""A1 (taskbook §10.3): two devices × three real native executors in one group.

Device 1 (connector pairing "Mac A"): Claude Code + Codex. Device 2 (pairing "Mac B",
behind a loopback network gate): claude-agent-acp (a real ACP agent) + a second Codex
instance (online, not delegated). Both pairings run on this Mac (the taskbook allows
two connector pairings as two devices). Only the Main model is scripted.

Pass facts recorded in ``test-results/external-agent-phase2-acceptance/A1.json``:
three delegations run in parallel, each with a scope; group chatter keeps getting
replies; an out-of-scope Write wakes the parent with a card, Yuki tells the group,
the person answers through the Client API and the native agent continues; device 2's
network drops mid-task (record keeps running with ``device_offline``, parent told
once), comes back and settles by the real terminal fact; every completion wakes the
parent once; parent activations are compared with wake inputs + user inputs.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.acceptance.harness import (
    SCRATCH,
    Facts,
    NetworkGate,
    Stack,
    StateRecorder,
    acp_instance,
    allow_option,
    by_local,
    call,
    claude_instance,
    codex_instance,
    journal_commands,
    now,
    project,
    run_root,
    say,
    started,
    until,
    write_artifact,
)
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent

OWNER, MEMBERS = 42, (43, 44)


async def permission_events(stack: Stack, work_id: str) -> list[dict[str, Any]]:
    """Pending permission facts of a record: tool and target path, never content."""
    assert stack.database is not None
    async with stack.database.read_session() as reader:
        rows = (
            await reader.execute(
                select(AgentEventModel.id, AgentEventModel.payload_json)
                .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
                .where(
                    AgentOperationModel.work_id == work_id,
                    AgentEventModel.kind.in_(("permission", "question")),
                )
                .order_by(AgentEventModel.id)
            )
        ).all()
    result = []
    for identity, payload_json in rows:
        payload = json.loads(payload_json).get("payload") or {}
        native = payload.get("native_request") or {}
        result.append(
            {
                "permission_id": str(identity),
                "tool": payload.get("tool")
                if not isinstance(payload.get("tool"), dict)
                else payload["tool"].get("kind"),
                "path": native.get("file_path") if isinstance(native, dict) else None,
                "command_head": (str(native.get("command", "")).split() or [None])[0]
                if isinstance(native, dict)
                else None,
            }
        )
    return result


async def test_a1_two_devices_three_executors_in_one_group(tmp_path):
    facts = Facts()
    stack = Stack(run_root("test_a1_two_devices_multi_agent"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    gate = NetworkGate(stack.port)
    await gate.start()
    artifact: dict[str, Any] = {"scenario": "A1", "status": "incomplete"}
    outside = SCRATCH / "proj" / "a1-outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "claude-outside.txt").unlink(missing_ok=True)
    projects = {name: project(f"a1-{name}") for name in ("claude", "codex", "acp", "codex2")}
    for path in projects.values():
        for name in ("a.txt", "b.txt", "c.txt"):
            (path / name).unlink(missing_ok=True)
    chatter: list[dict[str, Any]] = []
    plan_state: dict[str, Any] = {
        "parent": None,
        "children": {},
        "told": set(),
        "yuki_answered": set(),
        "offline_told": set(),
        "summary_sent": False,
        "sends": [],
    }
    try:
        mac_a = await stack.pair_device("Mac A")
        mac_b = await stack.pair_device("Mac B")
        await stack.start_connector(
            "mac-a",
            mac_a,
            [
                claude_instance("claude-1", projects["claude"]),
                codex_instance("codex-1", projects["codex"]),
            ],
        )
        await stack.start_connector(
            "mac-b",
            mac_b,
            [
                acp_instance("acp-1", projects["acp"]),
                codex_instance("codex-2", projects["codex2"]),
            ],
            url=gate.url(),
        )
        ids = by_local(await stack.online_instances(mac_a["device_id"], 2))
        ids.update(by_local(await stack.online_instances(mac_b["device_id"], 2)))
        plans = [
            (
                "claude",
                {
                    "goal": (
                        "Use the Write tool to create the file a.txt containing ok in the "
                        "current directory. Then use the Write tool to create the file "
                        f"{outside}/claude-outside.txt containing ok. Do nothing else."
                    ),
                    "executor": {"agent": ids["claude-1"], "device": mac_a["device_id"]},
                    "cwd": str(projects["claude"]),
                    "scope": {
                        "write_paths": [str(projects["claude"])],
                        "network": False,
                        "commands": ["ls", "pwd", "cat"],
                    },
                },
            ),
            (
                "codex",
                {
                    "goal": (
                        "Create a file named b.txt containing the text ok in the current "
                        "directory. Do nothing else."
                    ),
                    "executor": {"agent": ids["codex-1"], "device": mac_a["device_id"]},
                    "cwd": str(projects["codex"]),
                    "scope": {"write_paths": [str(projects["codex"])], "network": False},
                },
            ),
            (
                "acp",
                {
                    "goal": (
                        "Step 1: run exactly the shell command `ping -c 25 127.0.0.1` on its "
                        "own (not chained with anything, not in the background). Step 2: "
                        "after it has finished, create the file c.txt containing ok in the "
                        "current directory with your file writing tool. Do nothing else."
                    ),
                    "executor": {"agent": ids["acp-1"], "device": mac_b["device_id"]},
                    "cwd": str(projects["acp"]),
                    "scope": {
                        "write_paths": [str(projects["acp"])],
                        "network": False,
                        "commands": ["ping"],
                    },
                },
            ),
        ]

        async def parent_turn(turn) -> Any:
            children: dict[str, str] = plan_state["children"]
            tool = turn.last_tool()
            if tool and "answered" in tool:
                facts.mark("yuki_answer_result", result=tool)
            if turn.work_id is None:
                return call("manage_task", {"action": "start", "goal": "A1 three delegations"})
            plan_state["parent"] = turn.work_id
            if started(tool) and started(tool) not in children.values():
                name = plans[len(children)][0]
                children[name] = tool["id"]
                recorder.watch(tool["id"])
                facts.mark(
                    "delegated", executor=name, work_id=tool["id"], dispatch=tool["dispatch"]
                )
            if len(children) < len(plans):
                return call("start_subtask", plans[len(children)][1])
            # Decision facts only (cards); progress never reaches here.
            states = {name: await stack.work_row(identity) for name, identity in children.items()}
            for name, identity in children.items():
                row = states[name]
                if row["state"] in TERMINAL:
                    continue
                for request in await permission_events(stack, identity):
                    pid = request["permission_id"]
                    pending = {
                        item["permission_id"]: item
                        for item in (await stack.card(identity))["pending"]
                    }
                    if (
                        pid not in pending
                        or pid in plan_state["told"] | plan_state["yuki_answered"]
                    ):
                        continue
                    if request["path"] and str(outside) in request["path"]:
                        plan_state["told"].add(pid)
                        facts.mark("told_user_out_of_scope", work_id=identity, permission_id=pid)
                        plan_state["sends"].append("out_of_scope_notice")
                        return say(f"{name} 请求在委派范围外写入，请在 Desktop「等你回答」里处理。")
                    if row["reason"] == "device_offline":
                        continue  # nothing can reach the device now; the person may answer later
                    # Yuki may approve on its own (Q1); recorded on the timeline.
                    plan_state["yuki_answered"].add(pid)
                    facts.mark("yuki_answer", work_id=identity, permission_id=pid)
                    return call(
                        "message_task",
                        {
                            "id": identity,
                            "reply_to": pid,
                            "answer": allow_option(pending[pid].get("options")),
                            "text": "与委派目标一致的请求，批准这一次。",
                        },
                    )
                if row["reason"] == "device_offline" and identity not in plan_state["offline_told"]:
                    plan_state["offline_told"].add(identity)
                    facts.mark("told_user_offline", work_id=identity)
                    plan_state["sends"].append("offline_notice")
                    return say(f"{name} 所在设备离线了，原生可能仍在运行；恢复后按真实结果结算。")
            remaining = [
                identity
                for name, identity in children.items()
                if states[name]["state"] not in TERMINAL
            ]
            if remaining:
                return call("wait", {"for": [{"run": identity} for identity in remaining]})
            if not plan_state["summary_sent"]:
                plan_state["summary_sent"] = True
                plan_state["sends"].append("summary")
                return say(
                    "三个委派都已结束：" + "，".join(f"{n}={states[n]['state']}" for n in children)
                )
            return call("manage_task", {"action": "finish", "result": "A1 done"})

        async def decide(turn):
            if "A1-START" in turn.trigger_text or (
                plan_state["parent"] is not None and turn.work_id == plan_state["parent"]
            ):
                return await parent_turn(turn)
            if "chat-" in turn.trigger_text:
                if turn.last_role == "tool":
                    return None
                tag = turn.trigger_text.split("chat-", 1)[1].split()[0]
                # Which synthetic chatter tags this one turn carries (a quiet-window batch).
                facts.mark(
                    "chat_turn",
                    trigger=f"chat-{tag}",
                    batch=sorted(set(re.findall(r"chat-\d+", turn.last_text))),
                )
                return say(f"回复 chat-{tag}")
            return None

        stack.decide = decide
        facts.mark("owner_request")
        await stack.group(OWNER, "!ai A1-START 请分别让三个 Agent 各做一件事")

        all_done = asyncio.Event()

        async def chat_loop():
            n = 0
            while not all_done.is_set():
                n += 1
                sender = MEMBERS[n % 2]
                entry = {
                    "tag": f"chat-{n}",
                    "sender": sender,
                    "t_sent": round(now() - facts.started, 3),
                }
                chatter.append(entry)
                await stack.group(sender, f"!ai chat-{n} 大家好", direct=False)
                await asyncio.sleep(4)

        async def three_started():
            return len(plan_state["children"]) == 3 and plan_state["children"]

        # Chatter starts once the request has its own activation (two messages arriving
        # together in one group are one turn by the group's own batching).
        children = await until(three_started, 120)
        chatting = asyncio.create_task(chat_loop())

        # Out-of-scope request: the person answers it through the Client API (Q2).
        async def outside_request():
            for request in await permission_events(stack, children["claude"]):
                if request["permission_id"] in plan_state["told"]:
                    return request
            return None

        request = await until(outside_request, 240)
        answer = await stack.http_client.post(
            f"/client/v1/works/{children['claude']}/native-requests/{request['permission_id']}/answer",
            headers=stack.client_headers,
            json={"request_id": str(uuid4()), "option_id": "allow"},
        )
        facts.mark(
            "desktop_answer", status=answer.status_code, permission_id=request["permission_id"]
        )
        assert answer.status_code == 200, answer.text
        artifact["desktop_answer"] = answer.json()["data"]

        # Device 2's network drops while its ACP turn is still running: once its sleep
        # command was let through (in scope, or answered) and nothing is pending.
        async def acp_running():
            if (await stack.work_row(children["acp"]))["state"] != "waiting_external":
                return False
            if (await stack.card(children["acp"]))["pending"]:
                return False
            events = await stack.events(children["acp"])
            last_request = max(
                (e["cursor"] for e in events if e["kind"] == "permission"), default=0
            )
            return (
                len([e for e in events if e["kind"] == "progress" and e["cursor"] > last_request])
                >= 2
            )

        await until(acp_running, 180)
        await asyncio.sleep(4)
        acp_row = await stack.work_row(children["acp"])
        artifact["acp_state_before_cut"] = acp_row["state"]
        facts.mark("network_cut", device="mac-b")
        await gate.cut()

        async def offline_seen():
            row = await stack.work_row(children["acp"])
            return row["reason"] == "device_offline"

        await until(offline_seen, 30)
        facts.mark("device_offline_seen")
        await asyncio.sleep(12)
        artifact["acp_state_while_offline"] = dict(
            (k, v)
            for k, v in (await stack.work_row(children["acp"])).items()
            if k in ("state", "reason")
        )
        gate.restore()
        facts.mark("network_restored", device="mac-b")

        async def back_online():
            return (await stack.work_row(children["acp"]))["reason"] != "device_offline"

        await until(back_online, 120)
        facts.mark("device_online_seen")
        # A request that arrived while offline is still pending for the person (Desktop).
        for item in (await stack.card(children["acp"]))["pending"]:
            late = await stack.http_client.post(
                f"/client/v1/works/{children['acp']}/native-requests/{item['permission_id']}/answer",
                headers=stack.client_headers,
                json={"request_id": str(uuid4()), "option_id": allow_option(item.get("options"))},
            )
            facts.mark(
                "desktop_answer_after_reconnect",
                status=late.status_code,
                permission_id=item["permission_id"],
            )

        async def finished():
            rows = [await stack.work_row(identity) for identity in children.values()]
            return all(row["state"] in TERMINAL for row in rows)

        await until(finished, 420)
        facts.mark("all_children_terminal")

        async def parent_done():
            row = await stack.work_row(plan_state["parent"])
            return row["state"] in TERMINAL

        await until(parent_done, 120)
        all_done.set()
        await chatting
        await asyncio.sleep(3)
        await stack.drain_group()

        # ---- facts --------------------------------------------------------------------
        parent = plan_state["parent"]
        sent = stack.sent()
        batches = [m for m in facts.marks if m["mark"] == "chat_turn"]
        for entry in chatter:
            entry["replied"] = any(item["text"] == f"回复 {entry['tag']}" for item in sent)
            entry["answered_in_turn_of"] = next(
                (m["trigger"] for m in batches if entry["tag"] in m["batch"]), None
            )
        parent_inputs = await stack.inputs_of(parent)
        parent_activations = sorted(
            {item["activation"] for item in facts.model if item["work_id"] == parent}
        )
        delegations = {}
        for name, identity in children.items():
            delegations[name] = {
                "work_id": identity,
                "final": {
                    k: v
                    for k, v in (await stack.work_row(identity)).items()
                    if k in ("state", "reason", "parent_work_id")
                },
                "card": await stack.card(identity),
                "operations": await stack.operations(identity),
                "events": await stack.events(identity),
                "permissions": [
                    {k: v for k, v in item.items() if k != "path"}
                    | {"outside_scope": bool(item["path"] and str(outside) in item["path"])}
                    for item in await permission_events(stack, identity)
                ],
                "state_sequence": recorder.of(identity),
            }
        wake_inputs = [item for item in parent_inputs if item["kind"] == "subagent"]
        user_inputs = [item for item in parent_inputs if item["kind"] == "message"]
        activations = []
        seen_inputs: set[int] = set()
        parent_activations = sorted(
            parent_activations,
            key=lambda key: next(item["t"] for item in facts.model if item["activation"] == key),
        )
        for key in parent_activations:
            rows = [item for item in facts.model if item["activation"] == key]
            activations.append(
                {
                    "activation": key,
                    "t_first": rows[0]["t"],
                    "t_last": rows[-1]["t"],
                    "requests": len(rows),
                    "input_ids": sorted({i for row in rows for i in row["input_ids"]}),
                    "tools": [tool for row in rows for tool in row["tools"]],
                }
            )
            ids = set(activations[-1]["input_ids"])
            activations[-1]["new_inputs"] = sorted(ids - seen_inputs)
            activations[-1]["new_wake_inputs"] = sorted(
                i["input_id"] for i in wake_inputs if i["input_id"] in ids - seen_inputs
            )
            seen_inputs |= ids
        chat_activations = {}
        for item in facts.model:
            if item["work_id"] is None and item["activation"]:
                chat_activations.setdefault(item["activation"], []).append(item["t"])
        artifact.update(
            status="completed",
            agents={
                "device_1": {
                    "device_id": mac_a["device_id"],
                    "instances": [
                        "claude-1 (Claude Code 2.1.311, haiku)",
                        "codex-1 (codex-cli 0.169, own app-server)",
                    ],
                },
                "device_2": {
                    "device_id": mac_b["device_id"],
                    "instances": [
                        "acp-1 (claude-agent-acp 0.108.0, haiku)",
                        "codex-2 (codex-cli 0.169, online, not delegated)",
                    ],
                    "network": (
                        "loopback gate; cut() aborts the WSS and refuses reconnects while "
                        "the connector and ACP process keep running"
                    ),
                },
            },
            parent_work_id=parent,
            parent_final_state=(await stack.work_row(parent))["state"],
            delegations=delegations,
            parent_inputs=parent_inputs,
            parent_activations=activations,
            equation={
                "parent_activations": len(activations),
                "wake_class_inputs": len(wake_inputs),
                "user_inputs": len(user_inputs) + 1,  # + the owner request that created it
                "own_wait_expiries": 0,
                "holds": len(activations) == len(wake_inputs) + len(user_inputs) + 1,
                # Inputs that arrive together are delivered in one activation; each wake
                # input is consumed by exactly one activation.
                "coalesced_activations": [
                    a["activation"] for a in activations if len(a.get("new_wake_inputs", [])) > 1
                ],
                "each_wake_input_consumed_once": sorted(
                    i for a in activations for i in a.get("new_wake_inputs", [])
                )
                == sorted(i["input_id"] for i in wake_inputs),
            },
            group_chatter=chatter,
            chatter_all_replied=all(entry["replied"] for entry in chatter),
            chat_activation_spans_s=[round(v[-1] - v[0], 3) for v in chat_activations.values()],
            parent_sends=plan_state["sends"],
            model_admission=facts.admission,
            journals={
                name: journal_commands(stack.root / "connectors" / name / "journal.sqlite3")
                for name in ("mac-a", "mac-b")
            },
            files={
                "claude_a": (projects["claude"] / "a.txt").exists(),
                "claude_outside_after_desktop_answer": (outside / "claude-outside.txt").exists(),
                "codex_b": (projects["codex"] / "b.txt").exists(),
                "acp_c": (projects["acp"] / "c.txt").exists(),
            },
            marks=facts.marks,
        )
        # A message whose in-progress turn was superseded by a newer group message is
        # answered by that newer turn (the group's own latest-wins rule).
        for index, entry in enumerate(chatter):
            later = next((e["tag"] for e in chatter[index + 1 :] if e["replied"]), None)
            entry["superseded_by"] = None if entry["replied"] else later
        assert all(
            e["replied"] or e["answered_in_turn_of"] or e["superseded_by"] for e in chatter
        ), chatter
        assert artifact["equation"]["each_wake_input_consumed_once"], artifact["equation"]
        assert len([s for s in plan_state["sends"] if s == "offline_notice"]) == 1
        assert artifact["files"]["claude_outside_after_desktop_answer"]
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A1", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await gate.close()
        await stack.stop()
