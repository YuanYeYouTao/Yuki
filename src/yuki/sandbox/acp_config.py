"""Operator-selected ACP programs inside the existing persistent environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentProgram:
    id: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str = "/workspace"
    description: str = ""
    env: dict[str, str] = field(default_factory=dict, repr=False)
    permission_mode: str = "allow"

    def spec(self) -> dict[str, Any]:
        return {
            "agent_id": self.id,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "env": self.env,
            "permission_mode": self.permission_mode,
        }


def load_agents(path: Path | None) -> dict[str, AgentProgram]:
    if path is None:
        return {}
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    programs: dict[str, AgentProgram] = {}
    for identity, raw in document.get("agents", {}).items():
        command = raw["command"]
        args = raw.get("args", [])
        environment = raw.get("env", {})
        references = raw.get("env_from", {})
        if (
            not isinstance(command, str)
            or not command
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not isinstance(environment, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            )
            or not isinstance(references, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in references.items()
            )
        ):
            raise ValueError("invalid_acp_program")
        permission = raw.get("permission_mode", "allow")
        if permission not in {"allow", "ask"}:
            raise ValueError("invalid_acp_permission_mode")
        resolved = {key: os.environ[value] for key, value in references.items()}
        resolved.update(environment)
        programs[identity] = AgentProgram(
            id=identity,
            command=command,
            args=tuple(args),
            cwd=raw.get("cwd", "/workspace"),
            description=raw.get("description", ""),
            env=resolved,
            permission_mode=permission,
        )
    return programs
