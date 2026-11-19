"""E1: Codex unowned turns and approvals (own app-server only).

Scenarios:
  a  client A starts a turn that needs approval, disconnects while the approval is pending;
     client B connects later and resumes.
  a2 client A starts the turn and disconnects BEFORE the approval is emitted.
  b  queued item with no client subscribed (A queues then disconnects; turn start + approval routing)
  d  the subscribed client never answers: does the app-server wait or reroute? (A stays, silent)
"""
import asyncio
import json
import os
import sys

from cx import WS, Client, Log, is_method, text

MODEL = "gpt-6-luna"
APPROVE_PROMPT = "Run the shell command `touch {name}` in the current directory, then reply with the single word done."
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts", "E1")
os.makedirs(ART, exist_ok=True)
THREADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "threads.txt")


def remember(tid: str) -> None:
    with open(THREADS, "a") as fh:
        fh.write(tid + "\n")


async def start_thread(c: Client, approval: str = "untrusted") -> str:
    r = await c.call("thread/start", {"cwd": WS, "approvalPolicy": approval,
                                       "sandbox": "workspace-write", "model": MODEL})
    tid = r["thread"]["id"]
    remember(tid)
    return tid


def is_req(ev: dict) -> bool:
    return "id" in ev and "method" in ev and "requestApproval" in ev["method"]


async def status(c: Client, tid: str) -> None:
    r = await c.call("thread/read", {"threadId": tid, "includeTurns": True})
    turns = r["thread"].get("turns", [])
    c.log.frame(c.name, "local", method="thread/read:summary",
                turns=[{"id": t.get("id"), "status": t.get("status"),
                        "items": [i.get("type") for i in t.get("items", [])]} for t in turns])


async def scenario_a(log: Log, early: bool) -> None:
    a = Client("A", log)
    await a.connect()
    tid = await start_thread(a)
    await a.call("turn/start", {"threadId": tid, "input": text(APPROVE_PROMPT.format(name="e1a.txt")),
                                 "effort": "low", "model": MODEL, "clientUserMessageId": "e1-a-turn"})
    if not early:
        ev = await a.wait_for(is_req, 120)
        log.note(f"A got approval request: {ev is not None}")
    else:
        await asyncio.sleep(0.3)
    await a.close()
    log.note("A disconnected without answering")
    await asyncio.sleep(15)
    b = Client("B", log)
    await b.connect()
    await status(b, tid)
    await b.call("thread/resume", {"threadId": tid, "excludeTurns": True})
    ev = await b.wait_for(is_req, 45)
    log.note(f"B received a (re)sent approval request after resume: {ev is not None}")
    if ev is None:
        await status(b, tid)
        # see whether the thread is still waiting / active
        await asyncio.sleep(30)
        await status(b, tid)
        r = await b.call("thread/read", {"threadId": tid, "includeTurns": True})
        turns = r["thread"].get("turns", [])
        if turns and turns[-1].get("status") == "inProgress":
            await b.call("turn/interrupt", {"threadId": tid, "turnId": turns[-1]["id"]})
    else:
        await b.respond(ev["id"], {"decision": "decline"})
        await b.wait_for(is_method("serverRequest/resolved"), 20)
    await b.wait_for(is_method("turn/completed", threadId=tid), 60)
    await status(b, tid)
    await b.close()


async def scenario_b(log: Log) -> None:
    a = Client("A", log)
    await a.connect()
    tid = await start_thread(a)
    # one quick turn without approval so the thread is materialised
    await a.call("turn/start", {"threadId": tid, "input": text("Reply with the single word ready."),
                                 "effort": "low", "model": MODEL})
    await a.wait_for(is_method("turn/completed", threadId=tid), 120)
    r = await a.call("thread/queue/add", {"threadId": tid, "clientUserMessageId": "e1-b-queued",
                                          "input": text(APPROVE_PROMPT.format(name="e1b.txt"))})
    await asyncio.sleep(0.5)
    await a.call("thread/unsubscribe", {"threadId": tid})
    await a.close()
    log.note("A queued on an idle thread, unsubscribed and disconnected")
    await asyncio.sleep(20)
    b = Client("B", log)
    await b.connect()
    await status(b, tid)
    q = await b.call("thread/queue/list", {"threadId": tid})
    log.note(f"queue list after 20s: {len(q.get('data', q.get('queuedSubmissions', [])))} items; keys={list(q.keys())}")
    await b.call("thread/resume", {"threadId": tid, "excludeTurns": True})
    ev = await b.wait_for(is_req, 45)
    log.note(f"B got approval after resume: {ev is not None}")
    await asyncio.sleep(5)
    await status(b, tid)
    if ev is not None:
        await b.respond(ev["id"], {"decision": "decline"})
        await b.wait_for(is_method("turn/completed", threadId=tid), 60)
    else:
        r = await b.call("thread/read", {"threadId": tid, "includeTurns": True})
        turns = r["thread"].get("turns", [])
        if turns and turns[-1].get("status") == "inProgress":
            await b.call("turn/interrupt", {"threadId": tid, "turnId": turns[-1]["id"]})
            await b.wait_for(is_method("turn/completed", threadId=tid), 30)
    await status(b, tid)
    await b.close()


async def scenario_b2(log: Log) -> None:
    a = Client("A", log)
    await a.connect()
    tid = await start_thread(a)
    await a.call("turn/start", {"threadId": tid, "input": text("Reply with the word one, then think briefly and reply with the word two."),
                                 "effort": "low", "model": MODEL})
    await a.wait_for(is_method("turn/started", threadId=tid), 30)
    await a.call("thread/queue/add", {"threadId": tid, "clientUserMessageId": "e1-b2-queued",
                                      "input": text(APPROVE_PROMPT.format(name="e1b2.txt"))})
    await a.call("thread/unsubscribe", {"threadId": tid})
    await a.close()
    log.note("A queued while a turn was running, unsubscribed and disconnected before the queued turn started")
    await asyncio.sleep(30)
    b = Client("B", log)
    await b.connect()
    await status(b, tid)
    await b.call("thread/resume", {"threadId": tid, "excludeTurns": True})
    ev = await b.wait_for(is_req, 30)
    log.note(f"B got approval for the queued turn after resume: {ev is not None}")
    if ev is not None:
        await b.respond(ev["id"], {"decision": "decline"})
    await b.wait_for(is_method("turn/completed", threadId=tid), 60)
    await status(b, tid)
    await b.close()


async def scenario_d(log: Log) -> None:
    a = Client("A", log)
    await a.connect()
    tid = await start_thread(a)
    await a.call("turn/start", {"threadId": tid, "input": text(APPROVE_PROMPT.format(name="e1d.txt")),
                                 "effort": "low", "model": MODEL})
    ev = await a.wait_for(is_req, 120)
    log.note(f"A got approval: {ev is not None}; staying silent 150s")
    done = await a.wait_for(lambda e: e.get("method") in {"turn/completed", "serverRequest/resolved"}, 150)
    log.note(f"anything resolved while A silent: {done.get('method') if done else None}")
    await status(a, tid)
    # second client joins while A is silent: does it get the request?
    b = Client("B", log)
    await b.connect()
    await b.call("thread/resume", {"threadId": tid, "excludeTurns": True})
    evb = await b.wait_for(is_req, 20)
    log.note(f"late joiner B received pending request: {evb is not None}")
    if evb is not None:
        log.note(f"same request id as A's: {str(evb['id']) == str(ev['id'])}")
    await a.respond(ev["id"], {"decision": "decline"})
    await a.wait_for(is_method("serverRequest/resolved"), 20)
    await b.wait_for(is_method("serverRequest/resolved"), 5)
    await a.wait_for(is_method("turn/completed", threadId=tid), 60)
    await a.close()
    await b.close()


async def main() -> None:
    which = sys.argv[1]
    log = Log(os.path.join(ART, f"frames-{which}.jsonl"))
    log.note(f"scenario {which} start")
    if which == "a":
        await scenario_a(log, early=False)
    elif which == "a2":
        await scenario_a(log, early=True)
    elif which == "b":
        await scenario_b(log)
    elif which == "b2":
        await scenario_b2(log)
    elif which == "d":
        await scenario_d(log)
    log.note(f"scenario {which} end")


asyncio.run(main())
