"""Raw ACP (JSON-RPC over stdio) client used for E5/E8. Logs frames without bodies."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable

T0 = time.monotonic()
HERE = os.path.dirname(os.path.abspath(__file__))


class Log:
    def __init__(self, path: str):
        self.fh = open(path, "a")

    def frame(self, side: str, direction: str, **kw: Any) -> None:
        rec = {"t": round(time.monotonic() - T0, 3), "side": side, "dir": direction, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        self.fh.write(line + "\n")
        self.fh.flush()
        print(line, flush=True)

    def note(self, text: str) -> None:
        self.frame("harness", "note", note=text)


def summarize(method: str | None, params: Any, scope_roots: list[str]) -> dict:
    out: dict[str, Any] = {}
    if not isinstance(params, dict):
        return out
    for k in ("sessionId", "toolCallId", "terminalId", "protocolVersion", "stopReason"):
        if k in params:
            out[k] = params[k]
    upd = params.get("update")
    if isinstance(upd, dict):
        out["update"] = upd.get("sessionUpdate")
        for k in ("toolCallId", "kind", "status"):
            if k in upd:
                out[f"update.{k}"] = upd[k]
        locs = upd.get("locations")
        if isinstance(locs, list):
            out["update.locations.count"] = len(locs)
            out["update.locations.in_scope"] = [inside(l.get("path", ""), scope_roots) for l in locs]
        raw = upd.get("rawInput")
        if isinstance(raw, dict):
            out["update.rawInput.keys"] = sorted(raw.keys())
    tc = params.get("toolCall")
    if isinstance(tc, dict):
        for k in ("toolCallId", "kind", "status"):
            if k in tc:
                out[f"toolCall.{k}"] = tc[k]
        locs = tc.get("locations")
        if isinstance(locs, list):
            out["toolCall.locations.count"] = len(locs)
            out["toolCall.locations.in_scope"] = [inside(l.get("path", ""), scope_roots) for l in locs]
        raw = tc.get("rawInput")
        if isinstance(raw, dict):
            out["toolCall.rawInput.keys"] = sorted(raw.keys())
    if "options" in params and isinstance(params["options"], list):
        out["options"] = [{"optionId": o.get("optionId"), "kind": o.get("kind")} for o in params["options"]]
    if method in ("fs/write_text_file", "fs/read_text_file") and "path" in params:
        out["path.in_scope"] = inside(params["path"], scope_roots)
    if method == "terminal/create":
        out["has_command"] = "command" in params
    return out


def inside(path: str, roots: list[str]) -> bool:
    p = os.path.realpath(os.path.expanduser(path)) if path else ""
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


class ACPClient:
    def __init__(self, name: str, log: Log, argv: list[str], cwd: str, env: dict[str, str],
                 scope_roots: list[str]):
        self.name, self.log, self.argv, self.cwd, self.env = name, log, argv, cwd, env
        self.scope = [os.path.realpath(r) for r in scope_roots]
        self.proc: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self.cond = asyncio.Condition()
        self.handler: Callable[["ACPClient", dict], Any] | None = None
        self.reader: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=open(os.path.join(HERE, f"acp-{self.name}.stderr.log"), "ab"),
            limit=1 << 26)
        self.log.frame(self.name, "local", method="spawn", pid=self.proc.pid)
        self.reader = asyncio.create_task(self._recv())

    async def send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self.next_id += 1
        key = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        self.log.frame(self.name, "out", method=method, id=key, **summarize(method, params, self.scope))
        await self.send({"jsonrpc": "2.0", "id": key, "method": method, "params": params})
        msg = await (asyncio.wait_for(fut, timeout) if timeout else fut)
        if "error" in msg:
            e = msg["error"]
            self.log.frame(self.name, "in", method=f"{method}:error", id=key, code=e.get("code"),
                           message=str(e.get("message"))[:200])
            raise RuntimeError(f"{method}: {e.get('code')} {e.get('message')}")
        res = msg.get("result") or {}
        self.log.frame(self.name, "in", method=f"{method}:result", id=key, **summarize(method, res, self.scope))
        return res

    async def notify(self, method: str, params: dict) -> None:
        self.log.frame(self.name, "out", method=method, **summarize(method, params, self.scope))
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def respond(self, req_id: Any, result: dict | None = None, error: dict | None = None,
                      **logkw: Any) -> None:
        self.log.frame(self.name, "out", method="response", id=req_id, error=bool(error), **logkw)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result or {}
        await self.send(msg)

    async def _recv(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                self.log.frame(self.name, "in", method="eof")
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" not in msg:
                fut = self.pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
                continue
            m = msg["method"]
            p = msg.get("params", {})
            kind = "request" if "id" in msg else "notification"
            upd = (p.get("update") or {}).get("sessionUpdate") if isinstance(p, dict) else None
            if upd not in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
                self.log.frame(self.name, "in", kind=kind, method=m, id=msg.get("id"), **summarize(m, p, self.scope))
            async with self.cond:
                self.events.append(msg)
                self.cond.notify_all()
            if kind == "request" and self.handler:
                asyncio.create_task(self.handler(self, msg))

    async def wait_for(self, pred: Callable[[dict], bool], timeout: float, start: int = 0) -> dict | None:
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

    def count(self, pred: Callable[[dict], bool], start: int = 0) -> int:
        return sum(1 for e in self.events[start:] if pred(e))

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.log.frame(self.name, "local", method="process_exit", returncode=self.proc.returncode if self.proc else None)
