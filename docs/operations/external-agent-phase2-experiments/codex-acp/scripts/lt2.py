import asyncio, os
from cx import Client, Log
async def main():
    log = Log(os.path.join("artifacts","E8","frames-codex.jsonl"))
    c=Client("probe@srv1",log); await c.connect()
    r=await c.call("thread/loaded/list",{},quiet=True)
    log.note(f"~25 min after release with no client subscribed: handed-back thread still loaded on srv1: {'01a1eec8-4680-7a80-a518-c513162d95e6' in r['data']}; loaded count={len(r['data'])}")
    await c.close()
asyncio.run(main())
