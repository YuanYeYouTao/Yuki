"""Run SDK readonly discovery in the selected profile's own environment.

The public SDK discovery functions consult process environment. Isolating that
read avoids mutating os.environ across concurrently configured Claude profiles.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

from claude_agent_sdk import get_session_info, get_session_messages, list_sessions


def inspect_profile(request: dict[str, Any]) -> dict[str, Any]:
    directory = request.get("directory")
    if request["action"] == "list":
        offset = int(request.get("cursor") or 0)
        limit = request.get("limit", 30)
        sessions = list_sessions(directory=directory, limit=limit, offset=offset)
        return {
            "sessions": [
                {
                    "id": item.session_id,
                    "label": item.summary,
                    "cwd": item.cwd,
                    "status": "unknown",
                    "live_attach": False,
                }
                for item in sessions
            ],
            "next_cursor": str(offset + len(sessions)) if len(sessions) == limit else None,
        }
    identity = request["session_id"]
    item = get_session_info(identity, directory=directory)
    result: dict[str, Any] = {
        "session_id": identity,
        "status": "unknown",
        "live_attach": False,
        "native": asdict(item) if item else None,
    }
    if request.get("include_history"):
        result["messages"] = []
        for message in get_session_messages(
            identity,
            directory=directory,
            limit=request.get("limit", 30),
            offset=int(request.get("cursor") or 0),
        ):
            original = message.message
            body = original.get("content", [])
            if isinstance(body, list):
                body = [
                    {
                        key: block[key]
                        for key in ("type", "text", "id", "name", "tool_use_id")
                        if key in block
                    }
                    for block in body
                    if block.get("type") in {"text", "tool_use", "tool_result"}
                ]
            result["messages"].append({"id": message.uuid, "role": message.type, "content": body})
    return result


if __name__ == "__main__":
    print(json.dumps(inspect_profile(json.loads(sys.stdin.read())), ensure_ascii=False))
