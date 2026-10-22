"""Frozen original ACP receipt files; no server-side Agent launcher."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


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
