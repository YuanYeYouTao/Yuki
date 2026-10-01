"""Typed execution evidence, separate from lossy model-facing tool text."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.capabilities.results import ToolExecutionResult


@dataclass(slots=True)
class ResultCapture:
    work_id: str
    effect_key: str
    outcome: ToolExecutionResult | None = None
    artifact_handle: str | None = None


current_result_capture: ContextVar[ResultCapture | None] = ContextVar(
    "work_result_capture", default=None
)


def execution_evidence(
    outcome: ToolExecutionResult,
    *,
    tool: str,
    side_effecting: bool,
    arguments: str = "{}",
) -> dict[str, Any]:
    """Project execution facts before any presentation truncation occurs."""
    value = outcome.data
    body = value if isinstance(value, dict) else {}
    if isinstance(body.get("progress"), dict):
        body = body["progress"]
    artifacts: list[str] = []

    # Typed external payloads can be deep or reuse containers. Walk iteratively
    # so presentation failures cannot also break the durable receipt projection.
    stack, seen = [body], set()
    while stack:
        item = stack.pop()
        if isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, dict):
                if isinstance(item.get("artifact_id"), str):
                    artifacts.append(item["artifact_id"])
                stack.extend(reversed(tuple(item.values())))
            else:
                stack.extend(reversed(item))
    try:
        args = json.loads(arguments)
    except ValueError:
        args = {}
    delivered: list[str] = []
    caption_delivered = False
    delivered_message = False
    if tool == "send_message" and isinstance(args, dict):
        if isinstance(args.get("artifact_id"), str):
            receipt = body.get("file", body)
            if isinstance(receipt, dict) and receipt.get("status") == "succeeded":
                delivered.append(args["artifact_id"])
                artifacts.append(args["artifact_id"])
                caption = body.get("caption")
                caption_delivered = bool(
                    isinstance(args.get("text"), str)
                    and args["text"].strip()
                    and (
                        (isinstance(caption, dict) and caption.get("status") == "succeeded")
                        or (
                            args.get("attachment_kind") == "image"
                            and body.get("status") == "succeeded"
                        )
                    )
                )
        delivered_message = bool(
            isinstance(args.get("text"), str)
            and args["text"].strip()
            and (
                caption_delivered
                if args.get("attachment_kind") == "file"
                else body.get("status") == "succeeded"
            )
        )
    raw_status = body.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    return {
        "tool": tool,
        "side_effecting": side_effecting,
        "artifacts": list(dict.fromkeys(artifacts)),
        "delivered_artifacts": delivered,
        "caption_delivered": caption_delivered,
        "delivered_message": delivered_message,
        "delivery_target": body.get("target") if delivered or delivered_message else None,
        "run_id": body.get("run_id"),
        "ok": outcome.ok
        and not body.get("error")
        and status not in {"failed", "cancelled", "uncertain", "unknown"}
        and body.get("exit_code") in (None, 0),
        "pending": bool(body.get("pending")) or status in {"running", "queued", "waiting"},
        "uncertain": outcome.uncertain
        or bool(body.get("uncertain"))
        or status in {"uncertain", "unknown"},
        "status": status,
        "error_code": outcome.error_code,
        "mutation_committed": outcome.mutation_committed,
        "executed": body.get("executed", True),
    }
