"""E6b (Codex): writableRoots effectiveness and whether Yuki's per-turn scope leaks into a TUI turn on the same thread (srv1, default config)."""
import asyncio, os, subprocess, time
from cx import WS, Client, Log, is_method, text
R1 = os.path.realpath("codex.sock")
ROOT = os.path.expanduser("~/p2codex-exp-root")
def tmux(*a): return subprocess.run(["tmux","-L","p2codex",*a],capture_output=True,text=True).stdout
def is_req(ev): return "id" in ev and "method" in ev and "requestApproval" in ev["method"]
async def settle(c, tid, mark, log, step):
    got = None
    while True:
        ev = await c.wait_for(lambda e: is_req(e) or (e.get("method") == "turn/completed" and e["params"].get("threadId") == tid), 150, start=mark)
        if ev is None: break
        mark = c.events.index(ev) + 1
        if is_req(ev):
            got = ev; await c.respond(ev["id"], {"decision": "decline"}); continue
        break
    return got
async def main():
    log = Log(os.path.join("artifacts","E6","frames-codex-roots-and-leak.jsonl"))
    c = Client("S@srv1", log, sock=R1); await c.connect()
    r = await c.call("thread/start", {"cwd": WS, "model": "gpt-6-luna"})
    tid = r["thread"]["id"]; open("threads.txt","a").write(tid+"\n")
    scoped = {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [WS, ROOT], "networkAccess": False}}
    mark = len(c.events)
    await c.call("turn/start", {"threadId": tid, "effort":"low","model":"gpt-6-luna","clientUserMessageId":"e6b-1", **scoped,
        "input": text("Run the shell command `touch ~/p2codex-exp-root/inside-1.txt` exactly as written, then reply done.")})
    got = await settle(c, tid, mark, log, 1)
    f = os.path.join(ROOT, "inside-1.txt")
    log.frame("harness","local",method="step_result",step="1-yuki-scoped-extra-root",approval_requested=got is not None,file_in_extra_root_created=os.path.exists(f))
    p = Client("probe@srv1", log, sock=R1); await p.connect()
    await p.call("thread/resume", {"threadId": tid, "excludeTurns": True}); await p.close()
    # user's TUI resumes the same thread with default config and asks to write into the extra root
    tmux("new-session","-d","-s","tui3","-x","200","-y","60","-c",WS, f"codex resume {tid} --remote unix://{R1} -m gpt-6-luna; sleep 99999")
    await asyncio.sleep(6); tmux("send-keys","-t","tui3","Escape"); await asyncio.sleep(5)
    mark = len(c.events)
    tmux("send-keys","-t","tui3","-l","Run the shell command `touch ~/p2codex-exp-root/inside-2.txt` exactly as written; if blocked, request escalated permissions. Then reply done.")
    time.sleep(0.8); tmux("send-keys","-t","tui3","Enter")
    log.frame("tui3","out",method="user_prompt(TUI, write into Yuki's extra root)")
    st = await c.wait_for(is_method("turn/started", threadId=tid), 60, start=mark)
    got = await settle(c, tid, mark, log, 2)
    f2 = os.path.join(ROOT, "inside-2.txt")
    log.frame("harness","local",method="step_result",step="2-user-tui-turn-after-yuki-scope",approval_requested=got is not None,file_in_extra_root_created=os.path.exists(f2))
    p = Client("probe@srv1", log, sock=R1); await p.connect()
    await p.call("thread/resume", {"threadId": tid, "excludeTurns": True}); await p.close()
    tmux("kill-session","-t","tui3")
    await c.close()
asyncio.run(main())
