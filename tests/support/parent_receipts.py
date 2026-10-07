"""Observe original parent evidence on either its protocol or a legal business rebase."""

import json

from qq_ai_bot.domain.messages import FunctionCallOutput


def observation_bodies(content):
    """Read the explicit Host activation envelope without scanning arbitrary nesting."""
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return ()
    if not isinstance(body, dict):
        return ()
    if body.get("source") == "host" and body.get("kind") == "initial_runtime_context":
        return tuple(
            item
            for observation in body["data"]["observations"]
            for item in observation_bodies(observation.get("content"))
        )
    return (body,)


def parent_receipts(request, call_id):
    # F9 settles the old protocol before rebase. Preserve exact ID/result and
    # multiplicity assertions without demanding retired tool/opaque messages.
    results = []
    for entry in (*request.messages, *request.continuation_items):
        if isinstance(entry, FunctionCallOutput):
            if entry.call_id == call_id:
                results.append(entry.output)
            continue
        if entry.role == "tool" and entry.tool_call_id == call_id:
            results.append(entry.content)
        elif entry.role == "user" and entry.content:
            for body in observation_bodies(entry.content):
                if body.get("kind") == "work_unobserved_tool_round":
                    results.extend(
                        row["result"] for row in body["calls"] if row["call_id"] == call_id
                    )
    return results
