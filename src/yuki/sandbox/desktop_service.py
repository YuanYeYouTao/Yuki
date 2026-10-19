"""One foreground Supervisor service; all children stay in its process tree."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from types import FrameType
from typing import Any


def main() -> int:
    children: list[subprocess.Popen[Any]] = []
    stopped = False

    def stop(_signal: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    profile = Path.home() / ".local/share/yuki-computer/chromium"
    profile.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "DISPLAY": ":1"}
    try:
        children.append(
            subprocess.Popen(
                [
                    "Xvnc",
                    ":1",
                    "-geometry",
                    "1440x900",
                    "-depth",
                    "24",
                    "-rfbport",
                    "5901",
                    "-localhost",
                    "yes",
                    "-SecurityTypes",
                    "None",
                    "-AlwaysShared",
                ],
                env=environment,
            )
        )
        # Read actual socket readiness, rather than declaring failure after an age.
        while not stopped and children[0].poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", 5901), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        if stopped:
            return 0
        if children[0].poll() is not None:
            return 1
        for command in (
            ["openbox"],
            [
                "chromium",
                "--user-data-dir=" + str(profile),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222",
                "--restore-last-session",
                "--no-first-run",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        ):
            children.append(subprocess.Popen(command, env=environment))
        root = Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
        root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".desktop-status-")
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"run_id": os.environ["YUKI_JOB_ID"], "display": "running"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "desktop-status.json")
        while not stopped:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.1)
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
