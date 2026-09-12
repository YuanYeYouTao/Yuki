"""In-container durable process supervisor. No host control credentials."""

from __future__ import annotations

import errno
import json
import os
import pty
import select
import signal
import sys
import termios
import time
import tty
from pathlib import Path
from uuid import UUID

SEGMENT = 4 * 1024 * 1024


def atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def children(parent: int) -> set[int]:
    pairs = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                pairs[int(entry.name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                pass
    found = {parent}
    for _ in range(32):
        added = {pid for pid, ppid in pairs.items() if ppid in found} - found
        if not added:
            break
        found.update(added)
    return found


def main() -> int:
    identity = str(UUID(sys.argv[1]))
    root = Path("/var/lib/yuki-runtime") / identity
    spec = json.loads((root / "spec.json").read_text())
    state = root / "state"
    try:
        fd = os.open(state / "started", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return 125
    original = termios.tcgetattr(0) if os.isatty(0) else None
    interactive = bool(spec.get("tty"))
    if interactive:
        child, output = pty.fork()
        input_fd = output
    else:
        output, output_write = os.pipe()
        input_read, input_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.setsid()
            os.dup2(input_read, 0)
            os.dup2(output_write, 1)
            os.dup2(output_write, 2)
            for descriptor in (output, output_write, input_read, input_fd):
                if descriptor > 2:
                    os.close(descriptor)
        else:
            os.close(output_write)
            os.close(input_read)
    if child == 0:
        try:
            for key in tuple(os.environ):
                if key.startswith(("EXECD_", "OPENSANDBOX_")):
                    os.environ.pop(key, None)
            os.environ["YUKI_JOB_ID"] = identity
            os.chdir(spec["cwd"])
            argv = spec.get("argv") or ["/bin/bash", "-lc", spec["command"]]
            os.execvpe(argv[0], argv, os.environ)
        except Exception as exc:
            os.write(2, (type(exc).__name__ + ": " + str(exc) + "\n").encode())
            os._exit(126)
    if original:
        tty.setraw(0)
    requested: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda value, _frame: requested.append(value))
    start = time.time()
    offset = 0
    last_save = 0.0
    termination = 0.0
    reason = None
    readable = True
    stdin_open = not spec.get("capture", False)
    status = {"pid": child, "supervisor_pid": os.getpid(), "status": "running", "started_at": start}
    descendants = {child}

    def emit(data: bytes) -> None:
        nonlocal offset
        while data:
            number, position = divmod(offset, SEGMENT)
            piece, data = data[: SEGMENT - position], data[SEGMENT - position :]
            with (state / f"output-{number}.log").open("ab", buffering=0) as stream:
                stream.write(piece)
            offset += len(piece)
            if number >= 2:
                (state / f"output-{number - 2}.log").unlink(missing_ok=True)
            if not spec.get("capture"):
                try:
                    os.write(1, piece)
                except OSError:
                    pass

    exit_status = None
    try:
        while True:
            now = time.time()
            if now - last_save >= 0.5:
                status.update(heartbeat=now, output_offset=offset)
                atomic(state / "status.json", status)
                last_save = now
            timeout = spec.get("timeout_seconds", 0)
            if not termination and (
                (timeout and now - start >= timeout) or (state / "cancel").exists()
            ):
                reason = "timeout" if timeout and now - start >= timeout else "cancelled"
                termination = now
                requested.append(signal.SIGTERM)
            for sig in requested:
                descendants.update(children(child))
                for pid in descendants:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            requested.clear()
            if termination and now - termination > 3:
                for pid in descendants | children(child):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            watchers = ([output] if readable else []) + ([0] if stdin_open else [])
            available = select.select(watchers, [], [], 0.1)[0]
            if 0 in available:
                incoming = os.read(0, 8192)
                if not incoming:
                    stdin_open = False
                else:
                    try:
                        os.write(input_fd, incoming)
                    except OSError:
                        stdin_open = False
            if output in available:
                try:
                    data = os.read(output, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if data:
                    emit(data)
                else:
                    readable = False
            if exit_status is None:
                pid, raw = os.waitpid(child, os.WNOHANG)
                if pid:
                    exit_status = os.waitstatus_to_exitcode(raw)
            if exit_status is not None and (not readable or output not in available):
                break
        status.update(
            status="cancelled"
            if reason == "cancelled"
            else "failed"
            if reason or exit_status
            else "succeeded",
            exit_code=exit_status,
            error=reason,
            finished_at=time.time(),
            output_offset=offset,
        )
        atomic(state / "status.json", status)
        return exit_status or (1 if reason else 0)
    finally:
        if original:
            termios.tcsetattr(0, termios.TCSANOW, original)


if __name__ == "__main__":
    sys.exit(main())
