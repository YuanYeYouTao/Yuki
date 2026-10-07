"""Observe original parent evidence on either its protocol or a legal business rebase."""

import json

from qq_ai_bot.domain.messages import FunctionCallOutput


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
            try:
                body = json.loads(entry.content)
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("kind") == "work_unobserved_tool_round":
                results.extend(row["result"] for row in body["calls"] if row["call_id"] == call_id)
    return results
