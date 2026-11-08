"""Classify submitted executions independently of their host or transport."""

from typing import Any


def is_execution(name: str, args: dict[str, Any]) -> bool:
    """``name`` is a current tool; historical readers pass it via ``recorded_tool``."""
    return name == "run_command" or (
        name == "use_coding_agent" and args.get("action") in {"prompt", "queue"}
    )
