"""Environment-side computer CLI. Uses only its own runtime mount and stdlib."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def wait(root: Path, kind: str, note: str | None) -> dict[str, Any]:
    identity = str(UUID(os.environ["YUKI_JOB_ID"]))
    result = root / "results" / (identity + ".json")
    # A prior registration may already have finished, including before restart
    # or before this process first entered its file wait.
    if not result.exists():
        atomic_json(root / "requests" / (identity + ".json"), {"kind": kind, "note": note})
    while True:
        try:
            return cast(dict[str, Any], json.loads(result.read_text()))
        except FileNotFoundError:
            time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="yuki-computer")
    commands = parser.add_subparsers(dest="command", required=True)
    help_request = commands.add_parser("help-request")
    help_request.add_argument("--note", required=True)
    commands.add_parser("wait-control")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
    if args.command == "status":
        print((root / "control.json").read_text())
    else:
        print(
            json.dumps(
                wait(
                    root,
                    "help" if args.command == "help-request" else "wait_control",
                    getattr(args, "note", None),
                ),
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
