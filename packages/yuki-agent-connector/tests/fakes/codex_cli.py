"""A local-only Codex CLI substitute; it never starts a real native service."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

arguments = sys.argv[1:]
command = arguments[1:] if arguments[:1] == ["--original"] else arguments
trace = Path(os.environ["FAKE_CODEX_TRACE"])
with trace.open("a", encoding="utf-8") as output:
    output.write(
        json.dumps(
            {"args": arguments, "codex_home": os.environ.get("CODEX_HOME"), "cwd": os.getcwd()}
        )
        + "\n"
    )

if command == ["app-server", "daemon", "start"]:
    if os.environ.get("FAKE_CODEX_BLOCK_START"):
        threading.Event().wait()
    if os.environ.get("FAKE_CODEX_START_FAIL"):
        print("private daemon error: fixture-secret", file=sys.stderr)
        raise SystemExit(17)
    print('{"ready":true}')
elif command[:2] == ["app-server", "proxy"]:
    with socket.create_connection(("127.0.0.1", int(os.environ["FAKE_CODEX_PORT"]))) as connection:

        def copy_input() -> None:
            try:
                while data := sys.stdin.buffer.read1(65536):
                    connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=copy_input, daemon=True).start()
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
else:
    raise SystemExit(2)
