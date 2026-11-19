"""Minimal Codex app-server JSON-RPC client over the app-server unix socket (websocket).

Logs frames WITHOUT bodies: only method, ids, item types, statuses and native error codes.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Callable

from websockets.asyncio.client import unix_connect

SCRATCH = os.path.dirname(os.path.abspath(__file__))
SOCK = os.path.realpath(os.path.join(SCRATCH, "codex.sock"))
T0 = time.monotonic()
WS = os.path.join(SCRATCH, "ws")

ID_KEYS = ("threadId", "turnId", "itemId", "requestId", "expectedTurnId", "clientUserMessageId")


def _ids(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(obj, dict):
        return out
    for k in ID_KEYS:
        if k in obj:
            out[k] = obj[k]
    thread = obj.get("thread")
    if isinstance(thread, dict):
        out["thread.id"] = thread.get("id")
        st = thread.get("status")
        if st is not None:
            out["thread.status"] = st.get("type") if isinstance(st, dict) else st
    turn = obj.get("turn")
    if isinstance(turn, dict):
        out["turn.id"] = turn.get("id")
        if "status" in turn:
            out["turn.status"] = turn["status"]
    item = obj.get("item")
    if isinstance(item, dict):
        out["item.id"] = item.get("id")
        out["item.type"] = item.get("type")
        if "status" in item:
            out["item.status"] = item["status"]
        if item.get("clientId"):
            out["item.clientId"] = item["clientId"]
    st = obj.get("status")
    if isinstance(st, dict) and "type" in st:
        out["status"] = st["type"]
    qs = obj.get("queuedSubmission")
    if isinstance(qs, dict):
        out["queuedSubmission.id"] = qs.get("id")
    if "decision" in obj:
        out["decision"] = obj["decision"] if isinstance(obj["decision"], str) else "object"
    for k in ("approvalPolicy",):
        if k in obj:
            out[k] = obj[k] if isinstance(obj[k], str) else "granular"
    for k in ("sandbox", "sandboxPolicy"):
        v = obj.get(k)
        if isinstance(v, dict):
            out[k + ".type"] = v.get("type")
            if "writableRoots" in v:
                out[k + ".writableRoots.count"] = len(v["writableRoots"])
            if "networkAccess" in v:
                out[k + ".networkAccess"] = v["networkAccess"]
        elif isinstance(v, str):
            out[k] = v
    settings = obj.get("settings")
    if isinstance(settings, dict):
        out.update({"settings." + k2: v2 for k2, v2 in _ids(settings).items()})
        out["settings.keys"] = sorted(settings.keys())
    q = obj.get("queue") or obj.get("submissions")
    if isinstance(q, list):
        out["queue.len"] = len(q)
    return out


class Log:
    def __init__(self, path: str):
        self.path = path
        self.fh = open(path, "a")

    def frame(self, side: str, direction: str, **kw: Any) -> None:
        rec = {"t": round(time.monotonic() - T0, 3), "side": side, "dir": direction, **kw}
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    def note(self, text: str) -> None:
        self.frame("harness", "note", note=text)


SKIP_NOTIFY = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/reasoning/summaryPartAdded",
    "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta",
    "thread/tokenUsage/updated",
    "account/rateLimits/updated",
    "item/plan/delta",
    "mcpServer/startupStatus/updated",
}


class Client:
    def __init__(self, name: str, log: Log, sock: str = SOCK):
        self.name = name
        self.log = log
        self.sock = sock
        self.ws: Any = None
        self.pending: dict[str, asyncio.Future] = {}
        self.requests: dict[str, dict] = {}  # server requests received, by id
        self.handler: Callable[["Client", dict], Any] | None = None
        self.events: list[dict] = []
        self.reader: asyncio.Task | None = None
        self.cond = asyncio.Condition()

    async def connect(self) -> None:
        self.ws = await unix_connect(self.sock, uri="ws://localhost/", max_size=None)
        self.reader = asyncio.create_task(self._recv())
        await self.call(
            "initialize",
            {"clientInfo": {"name": f"p2exp_{self.name}", "version": "0"},
             "capabilities": {"experimentalApi": True}},
        )
        await self.ws.send(json.dumps({"method": "initialized"}))

    async def close(self) -> None:
        self.log.frame(self.name, "local", method="close")
        await self.ws.close()
        if self.reader:
            self.reader.cancel()

    async def call(self, method: str, params: dict, quiet: bool = False) -> dict:
        key = str(uuid.uuid4())[:8]
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        if not quiet:
            self.log.frame(self.name, "out", method=method, id=key, **_ids(params))
        await self.ws.send(json.dumps({"id": key, "method": method, "params": params}))
        try:
            msg = await fut
        finally:
            self.pending.pop(key, None)
        if "error" in msg:
            err = msg["error"]
            self.log.frame(self.name, "in", method=f"{method}:error", id=key,
                           code=err.get("code"), message=str(err.get("message"))[:300])
            raise RuntimeError(f"{method}: {err}")
        if not quiet:
            self.log.frame(self.name, "in", method=f"{method}:result", id=key, **_ids(msg.get("result", {})))
        return msg.get("result", {})

    async def respond(self, req_id: Any, result: dict) -> None:
        self.log.frame(self.name, "out", method="serverRequest:response", requestId=req_id, **_ids(result))
        await self.ws.send(json.dumps({"id": req_id, "result": result}))

    async def respond_raw_error(self, req_id: Any) -> None:
        await self.ws.send(json.dumps({"id": req_id, "error": {"code": -32000, "message": "declined"}}))

    async def _recv(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "method" not in msg:
                    fut = self.pending.get(str(msg.get("id")))
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                method = msg["method"]
                params = msg.get("params", {})
                if "id" in msg:
                    self.requests[str(msg["id"])] = msg
                    self.log.frame(self.name, "in", kind="serverRequest", method=method,
                                   requestId=msg["id"], **_ids(params))
                elif method not in SKIP_NOTIFY:
                    self.log.frame(self.name, "in", kind="notification", method=method, **_ids(params))
                async with self.cond:
                    self.events.append(msg)
                    self.cond.notify_all()
                if self.handler and "id" in msg:
                    asyncio.create_task(self.handler(self, msg))
        except Exception as exc:  # noqa: BLE001
            self.log.frame(self.name, "in", method="connection_closed", error=type(exc).__name__)

    async def wait_for(self, pred: Callable[[dict], bool], timeout: float = 120, start: int = 0) -> dict | None:
        deadline = time.monotonic() + timeout
        idx = start
        async with self.cond:
            while True:
                while idx < len(self.events):
                    ev = self.events[idx]
                    idx += 1
                    if pred(ev):
                        return ev
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), rem)
                except asyncio.TimeoutError:
                    return None


def is_method(m: str, **match: Any) -> Callable[[dict], bool]:
    def pred(ev: dict) -> bool:
        if ev.get("method") != m:
            return False
        p = ev.get("params", {})
        for k, v in match.items():
            if p.get(k) != v:
                return False
        return True
    return pred


def text(s: str) -> list[dict]:
    return [{"type": "text", "text": s}]


# Cheap model settings for every turn the harness starts.
MODEL = os.environ.get("P2_MODEL", "")
EFFORT = os.environ.get("P2_EFFORT", "low")
