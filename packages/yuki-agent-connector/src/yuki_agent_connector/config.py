"""Explicit local installs and profiles; no native credentials cross the wire."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from platformdirs import user_data_path


def private_path(path: Path, *, directory: bool = False) -> None:
    """Private files on POSIX, an explicit current-user ACL on Windows."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            check=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        sid = next(csv.reader(io.StringIO(result.stdout)))[1]
        grant = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


def atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    private_path(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        private_path(Path(temporary))
        os.replace(temporary, path)
        if sys.platform != "win32":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Instance:
    id: str
    driver: str
    label: str
    command: str
    args: list[str] = field(default_factory=list)
    cwd: str = field(default_factory=lambda: str(Path.home()))
    env: dict[str, str] = field(default_factory=dict)
    socket: str | None = None
    endpoint: str | None = None
    installation: str | None = None

    def environment(self) -> dict[str, str]:
        return {**os.environ, **self.env}


@dataclass(frozen=True)
class Config:
    url: str
    device_id: str
    token: str
    instances: list[Instance] = field(default_factory=list)
    full_access: bool = False

    @classmethod
    def load(cls, path: Path) -> Config:
        value = json.loads(path.read_text(encoding="utf-8"))
        instances = [Instance(**item) for item in value.pop("instances", [])]
        result = cls(**value, instances=instances)
        target = urlsplit(result.url)
        if target.scheme != "wss" and not (
            target.scheme == "ws" and target.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Remote connector URLs require wss://")
        if not result.device_id or not result.token:
            raise ValueError("Pair the connector with an explicit device credential first")
        if len({item.id for item in instances}) != len(instances):
            raise ValueError("Instance IDs must be unique within this device")
        return result

    def save(self, path: Path) -> None:
        atomic_private_json(path, asdict(self))


def data_path() -> Path:
    return user_data_path("YukiAgentConnector", appauthor=False)
