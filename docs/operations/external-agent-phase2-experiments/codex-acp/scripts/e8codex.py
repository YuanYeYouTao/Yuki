import asyncio, os, subprocess, time
from cx import Client, Log, is_method, text
R1 = os.path.realpath("codex.sock"); R2 = os.path.realpath("codex2.sock")
def tmux(*a): return subprocess.run(["tmux","-L","p2codex",*a],capture_output=True,text=True).stdout
async def loaded(log, tid):
    c = Client("probe@srv1", log, sock=R1); await c.connect()
    r = await c.call("thread/loaded/list", {}, quiet=True)
    log.note(f"thread still loaded on srv1: {tid in r.get('data',[])}")
    await c.close()
async def main():
    log = Log(os.path.join("artifacts","E8","frames-codex.jsonl"))
    tid = os.environ["TUI_THREAD"]
    log.note("TUI exited (user stopped). Yuki-side script takes over on the same daemon (srv1)")
    s = Client("S@srv1", log, sock=R1); await s.connect()
    r = await s.call("thread/read", {"threadId": tid, "includeTurns": True})
    before = len(r["thread"]["turns"])
    await s.call("thread/resume", {"threadId": tid, "excludeTurns": True})
    await s.call("turn/start", {"threadId": tid, "input": text("Reply with the single word HANDOFF7."), "effort":"low","model":"gpt-6-luna","clientUserMessageId":"e8-yuki-turn"})
    await s.wait_for(is_method("turn/completed", threadId=tid), 90)
    await s.call("thread/unsubscribe", {"threadId": tid})
    await s.close()
    log.note("script released (unsubscribe + disconnect)")
    await asyncio.sleep(3)
    await loaded(log, tid)
    c2 = Client("S2@srv2", log, sock=R2); await c2.connect()
    try:
        await c2.call("thread/resume", {"threadId": tid, "excludeTurns": True}); log.note("independent writer (srv2) resume after release: accepted")
        await c2.call("thread/unsubscribe", {"threadId": tid})
    except RuntimeError: log.note("independent writer (srv2) resume after release: rejected")
    await c2.close()
    await asyncio.sleep(2)
    tmux("new-session","-d","-s","tui2","-x","200","-y","60","-c",os.path.join(os.getcwd(),"ws"),
         f"codex resume {tid} --remote unix://{R1} -m gpt-6-luna; sleep 99999")
    await asyncio.sleep(12)
    screen = tmux("capture-pane","-p","-t","tui2","-S","-400")
    log.frame("tui2","local",method="screen",marker="HANDOFF7",visible="HANDOFF7" in screen, earlier_marker_TUIHELLO1="TUIHELLO1" in screen, conflict_banner="open in another app" in screen)
    p = Client("probe@srv1", log, sock=R1); await p.connect()
    r = await p.call("thread/read", {"threadId": tid, "includeTurns": True})
    after = r["thread"]["turns"]
    log.note(f"turns before takeover {before}, after handback {len(after)}; last turn clientIds={[i.get('clientId') for i in after[-1].get('items',[]) if i.get('clientId')]}")
    lst = await p.call("thread/list", {"cwd": os.path.join(os.getcwd(),"ws")}, quiet=True)
    ids = [t.get("id") for t in lst.get("data",[])]
    log.note(f"threads in this cwd: {len(ids)}; handed-back id unchanged and present: {tid in ids}")
    await p.close()
asyncio.run(main())
