"""E2 + E1(c): Codex TUI (on our own app-server via --remote) and a script client on one thread.

The TUI runs in `tmux -L p2codex` session `tui`. The harness types into it with send-keys and
reads its screen with capture-pane; only booleans (marker visible or not) are logged.
"""
import asyncio
import os
import subprocess
import sys

from cx import WS, Client, Log, is_method, text

MODEL = "gpt-6-luna"
HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "artifacts", "E2")
os.makedirs(ART, exist_ok=True)
THREADS = os.path.join(HERE, "threads.txt")
OUTSIDE = "~/p2codex-exp-never-created.txt"  # always declined, never created


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", "-L", "p2codex", *args], capture_output=True, text=True).stdout


def tui_type(line: str) -> None:
    tmux("send-keys", "-t", "tui", "-l", line)
    import time; time.sleep(0.8)
    tmux("send-keys", "-t", "tui", "Enter")


def tui_has(marker: str) -> bool:
    return marker in tmux("capture-pane", "-p", "-t", "tui", "-S", "-400")


def is_req(ev: dict) -> bool:
    return "id" in ev and "method" in ev and "requestApproval" in ev["method"]


async def find_tui_thread(c: Client) -> str:
    if os.environ.get("TUI_THREAD"):
        c.log.note("TUI thread id taken from the TUI session (non-ephemeral loaded thread)")
        return os.environ["TUI_THREAD"]
    r = await c.call("thread/loaded/list", {})
    ids = r.get("data") or r.get("threadIds") or []
    c.log.note(f"loaded threads on our app-server: {len(ids)}")
    known = set(open(THREADS).read().split()) if os.path.exists(THREADS) else set()
    cand = [i if isinstance(i, str) else i.get("id") for i in ids]
    cand = [i for i in cand if i not in known]
    assert len(cand) == 1, cand
    c.log.note("found the TUI thread among loaded threads")
    with open(THREADS + ".tui", "a") as fh:
        fh.write(cand[0] + "\n")
    return cand[0]


async def main() -> None:
    log = Log(os.path.join(ART, "frames.jsonl"))
    log.note("E2 start; TUI already running on our app-server")
    tui_type("Reply with the single word TUIHELLO1.")
    log.frame("tui", "out", method="user_prompt(TUI)", marker="TUIHELLO1")
    await asyncio.sleep(12)
    log.frame("tui", "local", method="screen", marker="TUIHELLO1", visible=tui_has("TUIHELLO1"))

    s = Client("S", log)
    await s.connect()
    tid = await find_tui_thread(s)
    await s.call("thread/resume", {"threadId": tid, "excludeTurns": True})

    # 1. idle: script starts a turn, TUI should show it
    await s.call("turn/start", {"threadId": tid, "input": text("Reply with the single word SCRIPTIDLE2."),
                                 "effort": "low", "model": MODEL, "clientUserMessageId": "e2-script-idle"})
    await s.wait_for(is_method("turn/completed", threadId=tid), 90)
    await asyncio.sleep(2)
    log.frame("tui", "local", method="screen", marker="SCRIPTIDLE2", visible=tui_has("SCRIPTIDLE2"))

    # 2. E1(c): TUI turn raises an approval; who gets it; can the script answer it?
    mark = len(s.events)
    tui_type(f"Run the shell command `touch {OUTSIDE}` exactly as written (it is outside the workspace, request approval), then reply done.")
    log.frame("tui", "out", method="user_prompt(TUI, needs approval)")
    ev = await s.wait_for(is_req, 120, start=mark)
    log.note(f"script received approval request of the TUI-started turn: {ev is not None}")
    await asyncio.sleep(2)
    screen = tmux("capture-pane", "-p", "-t", "tui", "-S", "-60")
    tui_prompt = any(w in screen for w in ("Would you like", "approve", "Allow", "Yes, proceed", "allow"))
    log.frame("tui", "local", method="screen", approval_prompt_visible=tui_prompt)
    if ev is not None:
        await s.respond(ev["id"], {"decision": "decline"})
        r = await s.wait_for(is_method("serverRequest/resolved", threadId=tid), 20, start=mark)
        log.note(f"script's decline accepted by app-server (serverRequest/resolved): {r is not None}")
        await asyncio.sleep(3)
        screen = tmux("capture-pane", "-p", "-t", "tui", "-S", "-60")
        log.frame("tui", "local", method="screen",
                  approval_prompt_still_visible=any(w in screen for w in ("Would you like", "Yes, proceed")))
    await s.wait_for(is_method("turn/completed", threadId=tid), 120, start=mark)
    await asyncio.sleep(3)

    # 3. TUI turn running: script steer + queue + turn/start conflict
    mark = len(s.events)
    tui_type("Run the shell command `sleep 20`, then reply with the single word SLEPT3.")
    log.frame("tui", "out", method="user_prompt(TUI, long turn)")
    started = await s.wait_for(is_method("turn/started", threadId=tid), 60, start=mark)
    tui_turn = started["params"]["turn"]["id"]
    # wait until the command is running
    await s.wait_for(lambda e: e.get("method") == "item/started"
                     and e["params"].get("item", {}).get("type") == "commandExecution", 60, start=mark)
    try:
        await s.call("turn/start", {"threadId": tid, "input": text("Reply with the single word CONFLICT4."),
                                     "effort": "low", "model": MODEL})
        log.note("turn/start while TUI turn running: accepted")
    except RuntimeError:
        log.note("turn/start while TUI turn running: rejected")
    try:
        await s.call("turn/steer", {"threadId": tid, "expectedTurnId": tui_turn,
                                     "input": text("Also include the word STEERED5 in your final reply."),
                                     "clientUserMessageId": "e2-steer"})
        log.note("steer with correct expectedTurnId: accepted")
    except RuntimeError:
        log.note("steer with correct expectedTurnId: rejected")
    try:
        await s.call("turn/steer", {"threadId": tid, "expectedTurnId": "00000000-0000-0000-0000-000000000000",
                                     "input": text("ignored")})
        log.note("steer with wrong expectedTurnId: accepted")
    except RuntimeError:
        log.note("steer with wrong expectedTurnId: rejected")
    await s.call("thread/queue/add", {"threadId": tid, "clientUserMessageId": "e2-queued",
                                      "input": text("Reply with the single word QUEUED6.")})
    done = await s.wait_for(is_method("turn/completed", threadId=tid), 120, start=mark)
    log.note(f"TUI turn completed: {done is not None}")
    q_started = await s.wait_for(lambda e: e.get("method") == "item/started"
                                 and e["params"].get("item", {}).get("clientId") == "e2-queued", 60, start=mark)
    log.note(f"queued item started its own turn after the TUI turn: {q_started is not None}")
    await s.wait_for(is_method("turn/completed", threadId=tid), 90, start=len(s.events) - 1)
    await asyncio.sleep(4)
    for m in ("SLEPT3", "STEERED5", "QUEUED6"):
        log.frame("tui", "local", method="screen", marker=m, visible=tui_has(m))
    r = await s.call("thread/read", {"threadId": tid, "includeTurns": True})
    turns = r["thread"].get("turns", [])
    log.frame("S", "local", method="thread/read:summary",
              turns=[{"id": t["id"], "status": t.get("status"),
                      "items": [i.get("type") for i in t.get("items", [])],
                      "clientIds": [i.get("clientId") for i in t.get("items", []) if i.get("clientId")]}
                     for t in turns])
    with open(os.path.join(HERE, "e2_thread.txt"), "w") as fh:
        fh.write(tid)
    await s.close()
    log.note("E2 end")


asyncio.run(main())
