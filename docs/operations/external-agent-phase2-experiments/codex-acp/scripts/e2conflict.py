import asyncio, os
from cx import Client, Log, text
async def main():
    log = Log(os.path.join("artifacts","E2","frames-conflict.jsonl"))
    log.note("second independent app-server (srv2) resumes the thread held by srv1 (TUI + script clients)")
    c = Client("S2@srv2", log, sock=os.path.realpath("codex2.sock")); await c.connect()
    tid = os.environ["TUI_THREAD"]
    try:
        await c.call("thread/resume", {"threadId": tid, "excludeTurns": True})
        log.note("resume on srv2: accepted")
        try:
            await c.call("turn/start", {"threadId": tid, "input": text("Reply with the single word SRV2."), "effort":"low","model":"gpt-6-luna"})
            log.note("turn/start on srv2: accepted")
        except RuntimeError: log.note("turn/start on srv2: rejected")
    except RuntimeError: log.note("resume on srv2: rejected")
    await asyncio.sleep(10)
    await c.close()
asyncio.run(main())
