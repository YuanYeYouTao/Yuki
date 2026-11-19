import asyncio, os, time
from cx import WS, Client, Log, is_method, text
async def main():
    log = Log(os.path.join("artifacts","E8","frames-codex-unload.jsonl"))
    s=Client("S@srv1",log); await s.connect()
    r=await s.call("thread/start",{"cwd":WS,"model":"gpt-6-luna"}); tid=r["thread"]["id"]; open("threads.txt","a").write(tid+"\n")
    await s.call("turn/start",{"threadId":tid,"input":text("Reply with the single word ok."),"effort":"low","model":"gpt-6-luna"})
    await s.wait_for(is_method("turn/completed",threadId=tid),90)
    await s.call("thread/unsubscribe",{"threadId":tid}); await s.close()
    t0=time.monotonic()
    p=Client("probe@srv1",log); await p.connect()
    while time.monotonic()-t0 < 600:
        r=await p.call("thread/loaded/list",{},quiet=True)
        if tid not in r["data"]:
            log.note(f"thread unloaded {round(time.monotonic()-t0)} s after the last client unsubscribed"); break
        await asyncio.sleep(10)
    else:
        log.note("thread still loaded after 600 s")
    await p.close()
asyncio.run(main())
