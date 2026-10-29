"""Run SDK readonly discovery in the selected profile's own environment.

The public SDK discovery functions consult process environment. Isolating that
read avoids mutating os.environ across concurrently configured Claude profiles.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import get_session_info, get_session_messages, list_sessions


def message_view(message: Any) -> dict[str, Any]:
    original = message.message
    body = original.get("content", [])
    omitted = False
    if isinstance(body, list):
        selected = [
            dict(block)
            for block in body
            if block.get("type") in {"text", "tool_use", "tool_result"}
        ]
        omitted = len(selected) != len(body)
        body = selected
    return {
        "id": message.uuid,
        "role": message.type,
        "content": body,
        "content_projection": "public_actions",
        "content_omitted": omitted,
        **({"timestamp": original["timestamp"]} if "timestamp" in original else {}),
    }


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
                    **{
                        key: value
                        for key, value in asdict(item).items()
                        if key in {"last_modified", "created_at", "git_branch", "first_prompt"}
                    },
                }
                for item in sessions
            ],
            "next_cursor": str(offset + len(sessions)) if len(sessions) == limit else None,
            "cursor_parameter": "native_cursor",
            "source_scope": "configured_profile",
        }
    identity = request["session_id"]
    item = get_session_info(identity, directory=directory)
    result: dict[str, Any] = {
        "session_id": identity,
        "status": "unknown",
        "live_attach": False,
        "native": asdict(item) if item else None,
        "query_scope": "execution" if request.get("execution_id") else "session",
        "status_scope": "session_metadata",
        "observed_at": datetime.now(UTC).isoformat(),
        "history_available": True,
    }
    if item is not None:
        # The SDK parses the original JSONL before slicing. Read it once, then
        # select from its chronological tail rather than returning its oldest page.
        messages = get_session_messages(identity, directory=directory)
        expected = request.get("execution_id")
        if expected:
            start = next(
                (i for i, message in enumerate(messages) if message.uuid == expected), None
            )
            if start is None:
                result.update(
                    messages=[], history_available=False, history_reason="execution_not_found"
                )
                return result
            end = len(messages)
            for index in range(start + 1, len(messages)):
                message = messages[index]
                content = message.message.get("content", [])
                if message.type == "user" and (
                    isinstance(content, str)
                    or any(block.get("type") != "tool_result" for block in content)
                ):
                    end = index
                    break
            messages = messages[start:end]
        offset = int(request.get("cursor") or 0)
        limit = request.get("limit", 30) if request.get("include_history") else 1
        end = max(0, len(messages) - offset)
        start = max(0, end - limit)
        result.update(
            messages=[message_view(message) for message in messages[start:end]],
            latest_message=message_view(messages[-1]) if messages else None,
            history_order="asc",
            history_scope="execution_messages" if expected else "session_messages",
            history_items_view="public_actions",
            cursor_parameter="native_cursor",
            cursor_direction="older",
            next_cursor=str(offset + end - start) if start else None,
        )
    else:
        result.update(history_available=False, history_reason="session_not_found")
    return result


if __name__ == "__main__":
    print(json.dumps(inspect_profile(json.loads(sys.stdin.read())), ensure_ascii=False))
