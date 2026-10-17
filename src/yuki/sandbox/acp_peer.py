"""Environment-owned ACP connection and durable originals, independent of Bot observers.

The file is mounted read-only into the persistent environment. Keep imports here
standard-library only: the host Manager also reads the original protocol files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("ACP original must be an object")
    return value


async def read_protocol_line(reader: asyncio.StreamReader) -> bytes:
    """Read one original frame; the stream buffer is not a message-size policy."""
    chunks: list[bytes] = []
    while True:
        try:
            chunks.append(await reader.readuntil(b"\n"))
            return b"".join(chunks)
        except asyncio.LimitOverrunError as exc:
            chunks.append(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            if chunks or exc.partial:
                raise ValueError("incomplete protocol frame") from exc
            return b""


def operation_key(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode()).hexdigest()


def post_request(peer_root: Path, request: dict[str, Any]) -> None:
    """Publish one original intent; a lost confirmation grants no redispatch."""
    key = operation_key(request["operation_id"])
    rpc = peer_root / "rpc"
    for kind in ("accepted", "inbox"):
        path = rpc / kind / f"{key}.json"
        if path.exists():
            if read_json(path) != request:
                raise ValueError("ACP operation parameters changed")
            return
    result = rpc / "results" / f"{key}.json"
    if result.exists():
        if read_json(result).get("request_hash") != hashlib.sha256(_encode(request)).hexdigest():
            raise ValueError("ACP operation parameters changed")
        return
    inbox = rpc / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=inbox, prefix=".pending-")
    target = inbox / f"{key}.json"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(request))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            _sync_directory(inbox)
        except FileExistsError:
            if read_json(target) != request:
                raise ValueError("ACP operation parameters changed") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_events(peer_root: Path, after_cursor: int = 0, limit: int = 100) -> dict[str, Any]:
    path = peer_root / "rpc" / "events.jsonl"
    events: list[dict[str, Any]] = []
    cursor = after_cursor
    if not path.exists():
        return {"events": events, "next_cursor": cursor, "has_more": False}
    with path.open("rb") as stream:
        if after_cursor < 0 or after_cursor > os.fstat(stream.fileno()).st_size:
            raise ValueError("invalid ACP event cursor")
        stream.seek(after_cursor)
        while len(events) < limit:
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                break
            event = json.loads(line)
            cursor = stream.tell()
            if event["cursor"] != cursor:
                raise ValueError("ACP event cursor does not match original")
            events.append(event)
        more = bool(stream.readline().endswith(b"\n"))
    return {"events": events, "next_cursor": cursor, "has_more": more}


def agent_environment(configured: dict[str, str]) -> dict[str, str]:
    """Pass environment proxy/CA explicitly; never import the Bot environment."""
    names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    )
    return {**{key: os.environ[key] for key in names if key in os.environ}, **configured}


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


@dataclass
class Terminal:
    process: asyncio.subprocess.Process
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    drain: asyncio.Task[None] | None = None


class Peer:
    def __init__(self, identity: str, root: Path, spec: dict[str, Any]) -> None:
        self.identity = identity
        self.root = root
        self.spec = spec
        self.session_id: str | None = None
        self.connection: Any = None
        self.active: dict[str, Any] | None = None
        self.active_cancelled = False
        self.sent = asyncio.Event()
        self.stopping = asyncio.Event()
        self.state_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()
        self.permissions: dict[str, tuple[dict[str, Any], asyncio.Future[Any]]] = {}
        self.terminals: dict[str, Terminal] = {}
        self.close_requests: list[dict[str, Any]] = []
        self.initial_updates: list[tuple[str, Any]] = []
        for kind in ("inbox", "accepted", "results"):
            (root / "rpc" / kind).mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, payload: Any, *, permission_id: str | None = None) -> None:
        active = self.active or {}
        event = {
            "event_id": str(uuid4()),
            "cursor": 0,
            "type": kind,
            "run_id": active.get("run_id"),
            "request_id": active.get("operation_id"),
            "session_id": self.session_id,
            "permission_id": permission_id,
            "payload": payload,
        }
        path = self.root / "rpc" / "events.jsonl"
        with path.open("ab") as stream:
            offset = stream.tell()
            while True:
                data = _encode(event) + b"\n"
                end = offset + len(data)
                if event["cursor"] == end:
                    break
                event["cursor"] = end
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self, request: dict[str, Any], **facts: Any) -> None:
        atomic_json(
            self.root / "rpc" / "results" / f"{operation_key(request['operation_id'])}.json",
            {
                "request_id": request["operation_id"],
                "operation_id": request["operation_id"],
                "request_hash": hashlib.sha256(_encode(request)).hexdigest(),
                "peer_id": self.identity,
                "run_id": request.get("run_id"),
                "session_id": self.session_id,
                "pending": False,
                **facts,
            },
        )

    def require_session(self, session_id: str) -> dict[str, Any]:
        if session_id != self.session_id or self.active is None or self.active_cancelled:
            raise ValueError("callback has no original active prompt")
        return self.active

    def save_permissions(self) -> None:
        path = self.root / "peer_metadata.json"
        metadata = read_json(path)
        metadata["permission_requests"] = [
            question for question, future in self.permissions.values() if not future.done()
        ]
        atomic_json(path, metadata)

    def on_connect(self, connection: Any) -> None:
        self.connection = connection

    def observe(self, event: Any) -> None:
        if event.direction == "outgoing" and event.message.get("method") == "session/prompt":
            self.sent.set()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self.session_id is None:
            self.initial_updates.append((session_id, update))
            return
        if session_id != self.session_id:
            raise ValueError("update belongs to another ACP session")
        self.event("session_update", _dump(update))

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> Any:
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        active = self.require_session(session_id)
        if self.spec.get("permission_mode", "allow") == "allow":
            selected = next((item for item in options if item.kind == "allow_once"), None)
            if selected is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=selected.option_id)
                )
        permission_id = str(uuid4())
        payload = {
            "permission_id": permission_id,
            "tool_call": _dump(tool_call),
            "options": [_dump(item) for item in options],
            "run_id": active["run_id"],
        }
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.permissions[permission_id] = (payload, future)
        self.event("permission_request", payload, permission_id=permission_id)
        self.save_permissions()
        try:
            outcome = await future
            return RequestPermissionResponse(
                outcome=(
                    DeniedOutcome(outcome="cancelled")
                    if outcome is None
                    else AllowedOutcome(outcome="selected", option_id=outcome)
                )
            )
        finally:
            self.permissions.pop(permission_id, None)
            self.save_permissions()

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import ReadTextFileResponse

        self.require_session(session_id)
        content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = (line or 1) - 1
            content = "".join(lines[start : None if limit is None else start + limit])
        return ReadTextFileResponse(content=content)

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> Any:
        from acp.schema import WriteTextFileResponse

        self.require_session(session_id)
        await asyncio.to_thread(Path(path).write_text, content, encoding="utf-8")
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import CreateTerminalResponse

        self.require_session(session_id)
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=cwd or self.spec["cwd"],
            env={**os.environ, **{item.name: item.value for item in (env or [])}},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        identity = str(uuid4())
        terminal = Terminal(process)

        async def drain() -> None:
            assert process.stdout is not None
            while data := await process.stdout.read(65536):
                terminal.output.extend(data)
                if output_byte_limit is not None and len(terminal.output) > output_byte_limit:
                    terminal.truncated = True
                    if output_byte_limit:
                        del terminal.output[:-output_byte_limit]
                    else:
                        terminal.output.clear()

        terminal.drain = asyncio.create_task(drain())
        self.terminals[identity] = terminal
        return CreateTerminalResponse(terminal_id=identity)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        from acp.schema import TerminalExitStatus, TerminalOutputResponse

        if session_id != self.session_id:
            raise ValueError("terminal belongs to another ACP session")
        terminal = self.terminals[terminal_id]
        process, buffer = terminal.process, terminal.output
        code = process.returncode
        status = (
            None
            if code is None
            else TerminalExitStatus(
                exit_code=code if code >= 0 else None, signal=str(-code) if code < 0 else None
            )
        )
        return TerminalOutputResponse(
            output=buffer.decode("utf-8", errors="replace"),
            truncated=terminal.truncated,
            exit_status=status,
        )

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        from acp.schema import WaitForTerminalExitResponse

        if session_id != self.session_id:
            raise ValueError("terminal belongs to another ACP session")
        terminal = self.terminals[terminal_id]
        code = await asyncio.shield(terminal.process.wait())
        assert terminal.drain is not None
        await asyncio.shield(terminal.drain)
        return WaitForTerminalExitResponse(
            exit_code=code if code >= 0 else None, signal=str(-code) if code < 0 else None
        )

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        from acp.schema import KillTerminalResponse

        if session_id != self.session_id:
            raise ValueError("terminal belongs to another ACP session")
        process = self.terminals[terminal_id].process
        try:
            if os.name == "nt":
                if process.returncode is None:
                    process.kill()
            else:
                # The leader may have exited while its background children live.
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.shield(process.wait())
        return KillTerminalResponse()

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        from acp.schema import ReleaseTerminalResponse

        await self.kill_terminal(session_id, terminal_id)
        terminal = self.terminals.pop(terminal_id)
        assert terminal.drain is not None
        await asyncio.shield(terminal.drain)
        return ReleaseTerminalResponse()

    async def dispatch(self, request: dict[str, Any]) -> None:
        try:
            if request["peer_id"] != self.identity or request["version"] != 1:
                raise ValueError("ACP request does not match original peer/protocol")
            method = request["method"]
            payload = request.get("payload", {})
            if method == "prompt":
                async with self.state_lock:
                    if self.active is not None or self.stopping.is_set():
                        raise ValueError("ACP session already has a prompt or is closing")
                    self.active = request
                    self.active_cancelled = False
                    self.sent.clear()
                    events_path = self.root / "rpc" / "events.jsonl"
                    start_cursor = events_path.stat().st_size if events_path.exists() else 0
                try:
                    from acp.schema import TextContentBlock

                    response = await self.connection.prompt(
                        session_id=self.session_id,
                        prompt=[TextContentBlock(type="text", text=payload["prompt"])],
                    )
                    value = _dump(response)
                    self.finish(
                        request,
                        status="cancelled"
                        if value.get("stopReason") == "cancelled"
                        else "succeeded",
                        result=value,
                        event_cursor_start=start_cursor,
                        event_cursor_end=events_path.stat().st_size if events_path.exists() else 0,
                    )
                except Exception as exc:
                    # Only a real protocol result proves completion after dispatch.
                    self.finish(
                        request,
                        status="failed",
                        uncertain=True,
                        error={"type": type(exc).__name__, "message": str(exc)},
                        event_cursor_start=start_cursor,
                        event_cursor_end=events_path.stat().st_size if events_path.exists() else 0,
                    )
                finally:
                    async with self.state_lock:
                        for _question, future in self.permissions.values():
                            if not future.done():
                                future.set_result(None)
                        self.active = None
                        self.sent.set()
                return
            if method == "permission_reply":
                async with self.state_lock:
                    if self.active is None or self.active["run_id"] != request.get("run_id"):
                        raise ValueError("permission prompt is no longer active")
                    question, future = self.permissions[payload["permission_id"]]
                    if future.done():
                        raise ValueError("permission already answered")
                    option = payload.get("option_id")
                    if payload.get("outcome") == "cancelled":
                        option = None
                    elif option not in {item["optionId"] for item in question["options"]}:
                        raise ValueError("permission option does not belong to original question")
                    future.set_result(option)
                    self.event(
                        "permission_resolved",
                        {"permission_id": payload["permission_id"], "option_id": option},
                        permission_id=payload["permission_id"],
                    )
                    self.save_permissions()
                self.finish(request, status="succeeded", result={"accepted": True})
            elif method in {"cancel", "close"}:
                if method == "close":
                    self.stopping.set()
                    self.close_requests.append(request)
                if self.active is not None:
                    await self.sent.wait()
                async with self.state_lock:
                    active = self.active
                    same = active is not None and (
                        method == "close" or active["run_id"] == request.get("run_id")
                    )
                    if same:
                        self.active_cancelled = True
                        await self.connection.cancel(session_id=self.session_id)
                        for _question, future in self.permissions.values():
                            if not future.done():
                                future.set_result(None)
                if method == "cancel":
                    self.finish(request, status="succeeded", result={"cancel_sent": same})
            else:
                raise ValueError("unsupported ACP peer operation")
        except Exception as exc:
            self.finish(
                request, status="failed", error={"type": type(exc).__name__, "message": str(exc)}
            )

    async def scan(self) -> None:
        # Accepted operations are deliberately never dispatched by a new process.
        while not self.stopping.is_set():
            for path in sorted((self.root / "rpc" / "inbox").glob("*.json")):
                request = read_json(path)
                accepted = self.root / "rpc" / "accepted" / path.name
                if accepted.exists():
                    if read_json(accepted) != request:
                        raise ValueError("ACP accepted original changed")
                    path.unlink()
                    continue
                os.replace(path, accepted)
                _sync_directory(accepted.parent)
                task = asyncio.create_task(self.dispatch(request))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            await asyncio.sleep(0.05)

    async def run(self) -> None:
        from acp import Client
        from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation
        from acp.stdio import spawn_agent_process

        marker = self.root / "rpc" / "peer_started"
        with marker.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(marker.parent)
        try:
            async with spawn_agent_process(
                cast(Client, self),
                self.spec["command"],
                *self.spec.get("args", []),
                cwd=self.spec["cwd"],
                env=agent_environment(self.spec.get("env", {})),
                transport_kwargs={"stderr": sys.stderr},
                observers=[self.observe],
            ) as (connection, process):
                self.connection = connection
                initialized = await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                        terminal=True,
                    ),
                    client_info=Implementation(name="Yuki", version="1"),
                )
                new = await connection.new_session(cwd=self.spec["cwd"], mcp_servers=[])
                self.session_id = new.session_id
                atomic_json(
                    self.root / "peer_metadata.json",
                    {
                        "peer_id": self.identity,
                        "session_id": self.session_id,
                        "agent_id": self.spec["agent_id"],
                        "initialize_result": _dump(initialized),
                        "new_session_result": _dump(new),
                        "agent_info": _dump(initialized).get("agentInfo"),
                        "agent_capabilities": _dump(initialized).get("agentCapabilities"),
                    },
                )
                for session_id, update in self.initial_updates:
                    await self.session_update(session_id, update)
                self.initial_updates.clear()
                scanner = asyncio.create_task(self.scan())
                exited = asyncio.create_task(process.wait())
                stopped = asyncio.create_task(self.stopping.wait())
                try:
                    done, _pending = await asyncio.wait(
                        (scanner, exited, stopped), return_when=asyncio.FIRST_COMPLETED
                    )
                    if scanner in done:
                        await scanner
                    self.stopping.set()
                finally:
                    scanner.cancel()
                    exited.cancel()
                    stopped.cancel()
                    await asyncio.gather(scanner, exited, stopped, return_exceptions=True)
        finally:
            self.stopping.set()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for terminal_id in tuple(self.terminals):
                await self.release_terminal(str(self.session_id), terminal_id)
            for request in self.close_requests:
                self.finish(request, status="succeeded", result={"closed": True})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer_id")
    parser.add_argument("--root", type=Path, default=Path("/var/lib/yuki-runtime"))
    args = parser.parse_args()
    root = args.root / args.peer_id
    peer = Peer(args.peer_id, root, read_json(root / "spec.json")["acp_peer"])
    asyncio.run(peer.run())


if __name__ == "__main__":
    main()
