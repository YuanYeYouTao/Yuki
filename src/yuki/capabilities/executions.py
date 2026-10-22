"""Classify submitted executions independently of their host or transport."""

from typing import Any


def is_execution(name: str, args: dict[str, Any]) -> bool:
    return name == "terminal_exec" or (
        name == "external_agent" and args.get("action") in {"prompt", "queue"}
    )
