"""E5 with codex-acp 2.7.0 (existing Codex login, gpt-6-luna low): client fs/terminal usage, rejection reaction, permission shape."""
import asyncio, os
from acpc import HERE, ACPClient, Log
WS = os.path.join(HERE, "acpws"); OUTSIDE = "~/p2acp-exp-never-created.txt"
ARGV = [os.path.join(HERE, "acp/node_modules/.bin/codex-acp"), "-c", 'model="gpt-6-luna"', "-c", 'model_reasoning_effort="low"']
def is_req(m): return lambda e: e.get("method") == m and "id" in e
async def main():
    log = Log(os.path.join(HERE, "artifacts", "E5", f"frames-codex-acp{'-mode-' + os.environ['P2_MODE'] if os.environ.get('P2_MODE') else ''}.jsonl"))
    log.note("codex-acp 2.7.0 (@agentclientprotocol, existing Codex login), model gpt-6-luna low")
    c = ACPClient("codex-acp", log, ARGV, WS, dict(os.environ), [WS]); await c.start()
    r = await c.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True}, "clientInfo": {"name": "p2exp", "version": "0"}}, timeout=60)
    log.frame("codex-acp", "local", method="agentCapabilities", caps=r.get("agentCapabilities"), authMethods=[a.get("id") for a in r.get("authMethods", [])], agentInfo=r.get("agentInfo"))
    async def handler(cl, msg):
        m = msg["method"]
        if m == "session/request_permission":
            opts = msg["params"].get("options", [])
            want = os.environ.get("P2_PERM", "allow")
            pick = next((o for o in opts if o.get("kind","").startswith(want)), opts[0])
            await cl.respond(msg["id"], {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}, decision=pick.get("kind"))
        elif m == "fs/write_text_file":
            await cl.respond(msg["id"], error={"code": -32000, "message": "write outside delegated scope"}, reason="scope")
        elif m == "fs/read_text_file":
            await cl.respond(msg["id"], error={"code": -32000, "message": "no"})
        elif m.startswith("terminal/"):
            await cl.respond(msg["id"], error={"code": -32000, "message": "command outside delegated scope"}, reason="scope")
    c.handler = handler
    s = await c.call("session/new", {"cwd": WS, "mcpServers": []}, timeout=60)
    sid = s["sessionId"]; open(os.path.join(HERE, "acp_sessions_codex.txt"), "a").write(sid + "\n")
    if os.environ.get("P2_MODE"):
        await c.call("session/set_mode", {"sessionId": sid, "modeId": os.environ["P2_MODE"]}, timeout=30)
    log.note(f"modes={[m.get('id') for m in (s.get('modes') or {}).get('availableModes',[])]} current={(s.get('modes') or {}).get('currentModeId')}")
    for name, text in [("fs", "Create the file e5c-fs.txt in the current directory containing the single word x. If it fails, do not retry another way; say failed."),
                       ("term", "Run the shell command `touch e5c-term.txt` in the current directory. If it fails, do not retry; say failed."),
                       ("outside", f"Run the shell command `touch {OUTSIDE}` exactly as written; if blocked, request escalated permissions. Then say done.")]:
        os.environ["P2_PERM"] = "reject" if name == "outside" else "allow"
        mark = len(c.events)
        r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}, timeout=240)
        log.frame("harness", "local", method="step_result", step=name, stopReason=r.get("stopReason"),
                  fs_write_requests=c.count(is_req("fs/write_text_file"), mark), terminal_create_requests=c.count(is_req("terminal/create"), mark),
                  permission_requests=c.count(is_req("session/request_permission"), mark),
                  ws_file_exists=os.path.exists(os.path.join(WS, f"e5c-{name}.txt")), outside_exists=os.path.exists(os.path.expanduser(OUTSIDE)))
    await c.stop()
asyncio.run(main())
