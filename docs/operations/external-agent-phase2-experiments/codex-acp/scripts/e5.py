"""E5 / E6(ACP) / E8(ACP) with a real ACP agent (claude-agent-acp 0.108.0 on the existing Claude login, haiku).

Scenarios (argv[1]):
  perm     permission request left unanswered for 120 s, then session/cancel; tool_call kind/locations
  fs       client rejects fs/write_text_file with an error; reaction of the agent
  term     client rejects terminal/create with an error (if the agent uses client terminals)
  bg       session/update and permission requests arriving with no prompt in flight
  load     second process loads a session that is live in the first; resume vs load after exit
"""
import asyncio
import json
import os
import sys

from acpc import HERE, ACPClient, Log

WS = os.path.join(HERE, "acpws")
ART = os.path.join(HERE, "artifacts", "E5")
OUTSIDE = "~/p2acp-exp-never-created.txt"
ARGV = [os.path.join(HERE, "acp/node_modules/.bin/claude-agent-acp")]
ENV = dict(os.environ, ANTHROPIC_MODEL="haiku")
SESSIONS = os.path.join(HERE, "acp_sessions.txt")


def is_req(m: str):
    return lambda e: e.get("method") == m and "id" in e


async def new_client(log: Log, name: str) -> ACPClient:
    c = ACPClient(name, log, ARGV, WS, ENV, [WS])
    await c.start()
    await c.call("initialize", {"protocolVersion": 1,
                                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True},
                                "clientInfo": {"name": "p2exp", "version": "0"}}, timeout=60)
    return c


async def new_session(c: ACPClient) -> str:
    s = await c.call("session/new", {"cwd": WS, "mcpServers": []}, timeout=60)
    sid = s["sessionId"]
    c.log.note(f"session/new currentModeId={(s.get('modes') or {}).get('currentModeId')}")
    mode = os.environ.get("P2_MODE")
    if mode:
        await c.call("session/set_mode", {"sessionId": sid, "modeId": mode}, timeout=30)
    with open(SESSIONS, "a") as fh:
        fh.write(sid + "\n")
    return sid


def prompt(c: ACPClient, sid: str, text: str):
    return asyncio.create_task(c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}))


async def allow_first(c: ACPClient, msg: dict) -> None:
    opts = msg["params"].get("options", [])
    allow = next((o for o in opts if o.get("kind") == "allow_once"), opts[0])
    await c.respond(msg["id"], {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}}, decision=allow.get("kind"))


async def scenario_perm(log: Log) -> None:
    c = await new_client(log, "acp1")
    pending: list[dict] = []

    async def handler(cl, msg):
        if msg["method"] == "session/request_permission":
            pending.append(msg)  # never answered until cancel
        elif msg["method"] in ("fs/read_text_file",):
            await cl.respond(msg["id"], error={"code": -32000, "message": "not in this experiment"})
    c.handler = handler
    sid = await new_session(c)
    p = prompt(c, sid, f"Use the Bash tool to run exactly `touch {OUTSIDE}`. Then say done.")
    ev = await c.wait_for(is_req("session/request_permission"), 120)
    log.note(f"permission request received: {ev is not None}; leaving it unanswered for 120 s")
    if ev is not None:
        await asyncio.sleep(120)
    log.note(f"prompt finished while unanswered: {p.done()}; extra notifications in that window recorded above")
    await c.notify("session/cancel", {"sessionId": sid})
    await asyncio.sleep(3)
    log.note(f"prompt finished 3 s after session/cancel (permission still unanswered): {p.done()}")
    for msg in pending:
        await c.respond(msg["id"], {"outcome": {"outcome": "cancelled"}}, decision="cancelled")
    try:
        r = await asyncio.wait_for(p, 30)
        log.note(f"prompt stopReason after cancel: {r.get('stopReason')}")
    except Exception as e:  # noqa: BLE001
        log.note(f"prompt after cancel: {type(e).__name__}")
    log.note(f"outside file created: {os.path.exists(os.path.expanduser(OUTSIDE))}")
    await c.stop()


async def scenario_fs(log: Log) -> None:
    c = await new_client(log, "acp1")

    async def handler(cl, msg):
        m = msg["method"]
        if m == "session/request_permission":
            await allow_first(cl, msg)
        elif m == "fs/write_text_file":
            await cl.respond(msg["id"], error={"code": -32000, "message": "write outside delegated scope"}, reason="scope")
        elif m == "fs/read_text_file":
            await cl.respond(msg["id"], error={"code": -32000, "message": "no such file"})
        elif m.startswith("terminal/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "terminal not allowed"})
    c.handler = handler
    sid = await new_session(c)
    r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        "Use the Write tool to create the file e5-fs.txt in the current directory containing the single word x. "
        "If the write fails, do not try any other way; just say failed."}]}, timeout=180)
    log.note(f"stopReason: {r.get('stopReason')}")
    log.note(f"fs/write_text_file requests: {c.count(is_req('fs/write_text_file'))}; terminal/create requests: {c.count(is_req('terminal/create'))}")
    log.note(f"file exists in workspace (agent bypassed the client fs): {os.path.exists(os.path.join(WS, 'e5-fs.txt'))}")
    await c.stop()


async def scenario_term(log: Log) -> None:
    c = await new_client(log, "acp1")

    async def handler(cl, msg):
        m = msg["method"]
        if m == "session/request_permission":
            await allow_first(cl, msg)
        elif m.startswith("terminal/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "command outside delegated scope"}, reason="scope")
        elif m.startswith("fs/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "no"})
    c.handler = handler
    sid = await new_session(c)
    r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        "Use the Bash tool to run `touch e5-term.txt` in the current directory. If it fails, do not retry; just say failed."}]}, timeout=180)
    log.note(f"stopReason: {r.get('stopReason')}")
    log.note(f"terminal/create requests: {c.count(is_req('terminal/create'))}")
    log.note(f"file exists (command ran natively despite client): {os.path.exists(os.path.join(WS, 'e5-term.txt'))}")
    await c.stop()


async def scenario_bg(log: Log) -> None:
    c = await new_client(log, "acp1")

    async def handler(cl, msg):
        m = msg["method"]
        if m == "session/request_permission":
            log.note("permission request arrived (allowing)")
            await allow_first(cl, msg)
        elif m.startswith("fs/") or m.startswith("terminal/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "no"})
    c.handler = handler
    sid = await new_session(c)
    mark = len(c.events)
    r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        "Use the Bash tool with run_in_background=true to run `sleep 10; echo bgdone`. Do not wait for it; end your turn immediately after starting it."}]}, timeout=180)
    log.note(f"prompt returned: {r.get('stopReason')}; now idle 60 s with no prompt in flight")
    after = len(c.events)
    await asyncio.sleep(60)
    ups = [e for e in c.events[after:] if e.get("method") == "session/update"]
    reqs = [e for e in c.events[after:] if "id" in e]
    log.frame("harness", "local", method="idle_window_summary", session_updates=len(ups),
              update_types=sorted({(e['params'].get('update') or {}).get('sessionUpdate') for e in ups}),
              requests=len(reqs))
    await c.stop()


async def scenario_bg2(log: Log) -> None:
    c = await new_client(log, "acp1")
    state = {"prompt_in_flight": True}

    async def handler(cl, msg):
        m = msg["method"]
        if m == "session/request_permission":
            log.note(f"permission request arrived; prompt in flight: {state['prompt_in_flight']}; rejecting")
            opts = msg["params"].get("options", [])
            rej = next((o for o in opts if o.get("kind", "").startswith("reject")), opts[-1])
            await cl.respond(msg["id"], {"outcome": {"outcome": "selected", "optionId": rej["optionId"]}}, decision=rej.get("kind"))
        elif m.startswith("fs/") or m.startswith("terminal/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "no"})
    c.handler = handler
    sid = await new_session(c)
    r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        f"Use the Bash tool with run_in_background=true to run `sleep 8; echo bgdone`. End your turn immediately. "
        f"Later, when you are notified that the background command finished, use the Bash tool to run exactly `touch {OUTSIDE}`."}]}, timeout=180)
    state["prompt_in_flight"] = False
    log.note(f"prompt returned: {r.get('stopReason')}; idle 60 s")
    after = len(c.events)
    await asyncio.sleep(60)
    reqs = [e for e in c.events[after:] if "id" in e]
    log.frame("harness", "local", method="idle_window_summary", requests=len(reqs),
              request_methods=[e["method"] for e in reqs],
              session_updates=sum(1 for e in c.events[after:] if e.get("method") == "session/update"))
    log.note(f"outside file created: {os.path.exists(os.path.expanduser(OUTSIDE))}")
    await c.stop()


async def scenario_load(log: Log) -> None:
    a = await new_client(log, "proc1")

    async def allow(cl, msg):
        if msg["method"] == "session/request_permission":
            await allow_first(cl, msg)
        elif "id" in msg:
            await cl.respond(msg["id"], error={"code": -32000, "message": "no"})
    a.handler = allow
    sid = await new_session(a)
    r = await a.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "Reply with the single word ACPONE."}]}, timeout=120)
    log.note(f"proc1 turn1 {r.get('stopReason')}")
    # single client / live elsewhere: proc2 loads the same session while proc1 still holds it
    b = await new_client(log, "proc2")
    b.handler = allow
    mark = len(b.events)
    try:
        await b.call("session/load", {"sessionId": sid, "cwd": WS, "mcpServers": []}, timeout=60)
        log.note(f"proc2 session/load while proc1 live: accepted; replayed chunks={b.count(lambda e: e.get('method')=='session/update', mark)}")
        r2 = await b.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "Reply with the single word ACPTWO."}]}, timeout=120)
        log.note(f"proc2 prompt on the live session: {r2.get('stopReason')}")
        r3 = await a.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "What single words did you reply with so far in this conversation? List them."}]}, timeout=120)
        txt = "".join((e['params']['update'].get('content') or {}).get('text', '') for e in a.events
                      if e.get('method') == 'session/update' and (e['params'].get('update') or {}).get('sessionUpdate') == 'agent_message_chunk')
        log.note(f"proc1 sees proc2's turn (ACPTWO in proc1 output): {'ACPTWO' in txt}")
    except Exception as e:  # noqa: BLE001
        log.note(f"proc2 session/load while proc1 live: rejected ({str(e)[:120]})")
    await b.stop()
    await a.stop()
    log.note("both processes exited; handoff: new process resumes / loads")
    c = await new_client(log, "proc3")
    c.handler = allow
    mark = len(c.events)
    try:
        await c.call("session/resume", {"sessionId": sid, "cwd": WS, "mcpServers": []}, timeout=60)
        log.note(f"proc3 session/resume: accepted; replayed updates={c.count(lambda e: e.get('method')=='session/update', mark)}")
    except Exception as e:  # noqa: BLE001
        log.note(f"proc3 session/resume failed: {str(e)[:120]}")
    await c.stop()
    d = await new_client(log, "proc4")
    d.handler = allow
    mark = len(d.events)
    await d.call("session/load", {"sessionId": sid, "cwd": WS, "mcpServers": []}, timeout=60)
    ups = [e for e in d.events[mark:] if e.get("method") == "session/update"]
    kinds = [(e['params'].get('update') or {}).get('sessionUpdate') for e in ups]
    txt = "".join((e['params']['update'].get('content') or {}).get('text', '') for e in ups
                  if (e['params'].get('update') or {}).get('sessionUpdate') == 'agent_message_chunk')
    log.frame("harness", "local", method="load_replay_summary", updates=len(ups),
              user_chunks=kinds.count("user_message_chunk"), agent_chunks=kinds.count("agent_message_chunk"),
              has_ACPONE="ACPONE" in txt, has_ACPTWO="ACPTWO" in txt)
    r = await d.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "Reply with the single word ACPTHREE."}]}, timeout=120)
    log.note(f"proc4 continues after handoff: {r.get('stopReason')}")
    await d.stop()


async def main() -> None:
    which = sys.argv[1]
    log = Log(os.path.join(ART, f"frames-{which}{'-mode-' + os.environ['P2_MODE'] if os.environ.get('P2_MODE') else ''}.jsonl"))
    log.note(f"scenario {which} start; agent claude-agent-acp 0.108.0, ANTHROPIC_MODEL=haiku")
    await {"perm": scenario_perm, "fs": scenario_fs, "term": scenario_term, "bg": scenario_bg, "bg2": scenario_bg2,
           "load": scenario_load}[which](log)
    log.note(f"scenario {which} end")


asyncio.run(main())
