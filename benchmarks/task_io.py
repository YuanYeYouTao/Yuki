"""Standard-library task-side transport for the original Yuki Supervisor."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def view(job: Path, cursor: int = 0) -> dict:
    state = job / "state"
    if not (state / "status.json").exists():
        return {"status": "running", "pending": True, "output": "", "next_cursor": cursor}
    result = json.loads((state / "status.json").read_text())
    end = result.get("output_offset", 0)
    segment_size = 4 * 1024 * 1024
    first = max(0, (max(0, end - 1) // segment_size - 1) * segment_size)
    position = min(end, max(cursor, first))
    start = position
    output = bytearray()
    lost = cursor < first
    while position < end and len(output) < 32768:
        number, offset = divmod(position, segment_size)
        path = state / f"output-{number}.log"
        if not path.exists():
            lost = True
            position = min(end, (number + 1) * segment_size)
            continue
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(min(32768 - len(output), end - position))
        if not chunk:
            break
        output.extend(chunk)
        position += len(chunk)
    return {
        **result,
        "pending": result["status"] not in {"succeeded", "failed", "cancelled"},
        "output": output.decode(errors="replace"),
        "cursor": start,
        "next_cursor": position,
        "output_offset": end,
        "output_lost": lost,
        "truncated": position < end,
    }


def handle(request: dict) -> dict:
    root = Path(request["root"])
    action = request["action"]
    if action == "info":
        return {"cwd": os.getcwd(), "home": os.environ.get("HOME"), "python": sys.executable}
    if action in {"file_info", "media_read", "write_file"}:
        path = Path(request["path"])
        if not path.is_absolute():
            path = Path(request["cwd"]) / path
        expected = request.get("expected_version")
        if action == "write_file":
            actual = "missing"
            if path.exists():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                actual = digest.hexdigest()
            if (actual != "missing" and expected != actual) or (
                actual == "missing" and expected not in {None, "missing"}
            ):
                return {"error": "version_conflict"}
            path.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(request["base64"])
            temporary = path.with_name(path.name + ".yuki-pending")
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Preserve the original conditional import semantics if the terminal edited it.
            latest = "missing"
            if path.exists():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                latest = digest.hexdigest()
            if latest != actual:
                temporary.unlink()
                return {"error": "version_conflict"}
            temporary.replace(path)
            expected = None
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if action == "media_read" and before.st_size > 20 * 1024 * 1024:
                return {"error": "workspace_media_too_large"}
            digest = hashlib.sha256()
            chunks = []
            size = 0
            while chunk := stream.read(65536):
                digest.update(chunk)
                size += len(chunk)
                if action == "media_read":
                    if size > 20 * 1024 * 1024:
                        return {"error": "workspace_media_too_large"}
                    chunks.append(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return {"error": "file_changed_during_read"}
        version = digest.hexdigest()
        if expected is not None and expected != version:
            return {"error": "version_conflict"}
        result = {"path": str(path), "version": version, "size": size}
        if action == "media_read":
            result["base64"] = base64.b64encode(b"".join(chunks)).decode()
        return result
    identity = request["run_id"]
    job = root / identity
    state = job / "state"
    if action == "start":
        state.mkdir(parents=True, exist_ok=True)
        spec = request["spec"]
        spec_path = job / "spec.json"
        if spec_path.exists():
            if json.loads(spec_path.read_text()) != spec:
                return {"error": "request_conflict", "run_id": identity}
        else:
            spec_path.write_text(json.dumps(spec))
        # A previously accepted dispatch is observed, never executed a second time.
        try:
            (state / "dispatch").touch(exist_ok=False)
        except FileExistsError:
            return {"run_id": identity, **view(job)}
        fifo = job / "stdin"
        os.mkfifo(fifo, 0o600)
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            with (job / "supervisor.err").open("wb") as errors:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(root / "supervisor.py"), identity, str(root)],
                    stdin=descriptor,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
            (state / "supervisor_pid").write_text(str(process.pid))
        finally:
            os.close(descriptor)
        return {"run_id": identity, **view(job)}
    if not job.exists():
        return {"error": "run_not_found", "run_id": identity}
    if action == "control":
        control = request["control"]
        if not view(job)["pending"]:
            return {"run_id": identity, **view(job)}
        if control in {"cancel", "close"}:
            (state / "cancel").touch()
        elif control == "interrupt":
            os.kill(int((state / "supervisor_pid").read_text()), signal.SIGINT)
        elif control == "input":
            descriptor = os.open(job / "stdin", os.O_WRONLY | os.O_NONBLOCK)
            incoming = memoryview(request.get("text", "").encode())
            written = 0
            try:
                while written < len(incoming):
                    select.select([], [descriptor], [])
                    try:
                        written += os.write(descriptor, incoming[written:])
                    except BlockingIOError:
                        continue
            except OSError:
                return {"error": "terminal_input_incomplete", "input_bytes_written": written}
            finally:
                os.close(descriptor)
        else:
            raise ValueError("invalid_terminal_action")
    if action == "read":
        until = time.monotonic() + request.get("wait_seconds", 0)
        while view(job).get("pending") and time.monotonic() < until:
            time.sleep(min(0.1, max(0, until - time.monotonic())))
    return {"run_id": identity, **view(job, request.get("cursor", 0))}


if __name__ == "__main__":
    request_path = Path(sys.argv[1])
    payload = json.loads(request_path.read_text())
    request_path.unlink()
    try:
        response = handle(payload)
    except (OSError, ValueError) as exc:
        response = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(response, ensure_ascii=False))
