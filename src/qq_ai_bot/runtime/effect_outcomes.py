"""Typed execution evidence, separate from lossy model-facing tool text."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from qq_ai_bot.capabilities.results import ToolExecutionResult, process_receipt


@dataclass(slots=True)
class ResultCapture:
    work_id: str
    effect_key: str
    outcome: ToolExecutionResult | None = None
    artifact_handle: str | None = None
    evidence: dict[str, Any] | None = None


current_result_capture: ContextVar[ResultCapture | None] = ContextVar(
    "work_result_capture", default=None
)


def execution_finished(evidence: dict[str, Any]) -> bool:
    """Only an explicit terminal receipt can settle an original execution.

    A successful read can observe a failed process. Conversely, a failed read
    with no status says nothing about whether that process has finished.
    """
    status = evidence.get("status")
    return (
        isinstance(status, str)
        and status in {"completed", "succeeded", "failed", "cancelled"}
        and not evidence.get("pending")
        and not evidence.get("uncertain")
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
    process = process_receipt(outcome)
    return {
        "tool": tool,
        "side_effecting": side_effecting,
        "artifacts": list(dict.fromkeys(artifacts)),
        "delivered_artifacts": delivered,
        "caption_delivered": caption_delivered,
        "delivered_message": delivered_message,
        "delivery_target": body.get("target") if delivered or delivered_message else None,
        "run_id": body.get("run_id"),
        **({"request_id": body["request_id"]} if isinstance(body.get("request_id"), str) else {}),
        "ok": outcome.ok
        and not body.get("error")
        and process.get("succeeded") is not False
        and status not in {"failed", "cancelled", "uncertain", "unknown"}
        and body.get("exit_code") in (None, 0),
        "pending": bool(body.get("pending")) or status in {"running", "queued", "waiting"},
        "uncertain": outcome.uncertain
        or bool(body.get("uncertain"))
        or status in {"uncertain", "unknown"},
        "status": status,
        "error_code": outcome.error_code,
        "retryable": outcome.retryable,
        "mutation_committed": outcome.mutation_committed,
        **(
            {"request_postcondition_satisfied": True}
            if outcome.ok
            and not outcome.uncertain
            and outcome.request_postcondition_satisfied is True
            else {}
        ),
        "executed": body.get("executed", True),
        **({"process": process} if process else {}),
    }


def readonly_call_signature(name: str, arguments: str) -> str:
    """Canonical readonly reuse signature; argument key order is not identity."""
    try:
        value = json.loads(arguments)
    except ValueError:
        normalized = arguments.strip()
    else:
        normalized = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(json.dumps([name, normalized], ensure_ascii=False).encode()).hexdigest()


def effect_evidence(
    outcome: ToolExecutionResult,
    *,
    tool: str,
    side_effecting: bool,
    arguments: str,
) -> dict[str, Any]:
    """Durable receipt evidence: typed facts plus the readonly signature."""
    evidence = execution_evidence(
        outcome, tool=tool, side_effecting=side_effecting, arguments=arguments
    )
    if not side_effecting:
        evidence["readonly_call_signature"] = readonly_call_signature(tool, arguments)
    return evidence


def captured_evidence(
    capture: ResultCapture, *, tool: str, side_effecting: bool, arguments: str
) -> dict[str, Any] | None:
    """Recorded evidence wins; otherwise project the typed outcome; None if neither."""
    if capture.evidence is not None:
        return capture.evidence
    if capture.outcome is None:
        return None
    return execution_evidence(
        capture.outcome, tool=tool, side_effecting=side_effecting, arguments=arguments
    )


def historical_evidence(
    receipt: dict[str, Any],
    *,
    state: str = "accepted",
    original_tool: str = "legacy_tool",
    kind: str = "tool",
) -> dict[str, Any]:
    """Read original facts without promoting absent or malformed display data."""
    from qq_ai_bot.capabilities.results import normalize_legacy_result

    # Final transport receipts predate and intentionally do not use tool outcomes.
    # Only their exact domain/state proof can settle them; a tool row never gets this exemption.
    if kind == "final" and not any(key in receipt for key in ("outcome", "result", "status", "ok")):
        accepted = (
            state == "accepted"
            and receipt.get("transport_accepted") is True
            and "error" not in receipt
            and all(
                receipt.get(key, expected) is expected
                for key, expected in (
                    ("pending", False),
                    ("uncertain", False),
                    ("executed", True),
                    ("mutation_committed", True),
                )
            )
        )
        refused = (
            state == "failed"
            and receipt.get("error") == "delivery_not_dispatched"
            and receipt.get("executed") is False
            and receipt.get("mutation_committed") is False
            and "transport_accepted" not in receipt
            and receipt.get("pending", False) is False
            and receipt.get("uncertain", False) is False
        )
        if accepted or refused:
            return {
                "tool": "final_delivery",
                "side_effecting": True,
                "ok": accepted,
                "executed": accepted,
                "mutation_committed": accepted,
                "pending": False,
                "uncertain": False,
                "status": "succeeded" if accepted else "not_dispatched",
                "transport_accepted": accepted,
                "error_code": None if accepted else "delivery_not_dispatched",
            }

    evidence = receipt.get("outcome")
    valid = isinstance(evidence, dict) and (
        type(evidence.get("ok")) is bool
        or evidence.get("pending") is True
        or evidence.get("uncertain") is True
        or evidence.get("side_effecting") is False
    )
    if isinstance(evidence, dict):
        for key in ("ok", "pending", "uncertain", "side_effecting", "executed", "retryable"):
            if key in evidence and type(evidence[key]) is not bool:
                valid = False
        if evidence.get("status") is not None and not isinstance(evidence["status"], str):
            valid = False
        if (
            "mutation_committed" in evidence
            and evidence["mutation_committed"] is not None
            and type(evidence["mutation_committed"]) is not bool
        ):
            valid = False
    if valid and isinstance(evidence, dict):
        result = dict(evidence)
    elif evidence is None or evidence == {}:
        payload = receipt.get("result")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        try:
            if (
                not isinstance(payload, dict)
                or type(payload.get("ok")) is not bool
                or payload.get("truncated") is True
            ):
                raise ValueError("historical_outcome_unknown")
            body = payload.get("data", payload)
            if not isinstance(body, dict):
                body = payload
            original_body = body
            if isinstance(body, dict) and isinstance(body.get("progress"), dict):
                body = body["progress"]
            if isinstance(body, dict):
                if body.get("status") is not None and not isinstance(body["status"], str):
                    raise ValueError("historical_outcome_unknown")
                if body.get("truncated") is True or any(
                    key in body and type(body[key]) is not bool
                    for key in ("pending", "uncertain", "executed")
                ):
                    raise ValueError("historical_outcome_unknown")
            result = execution_evidence(
                replace(
                    normalize_legacy_result(
                        payload, provider_id="historical", tool_name=original_tool
                    ),
                    data=original_body,
                ),
                tool=original_tool,
                side_effecting=True,
            )
        except (TypeError, ValueError):
            result = {"ok": False, "uncertain": True, "side_effecting": True, "executed": True}
    else:
        # Retain original identifiers and explicit pending facts for reconciliation.
        result = dict(evidence) if isinstance(evidence, dict) else {}
        result.update(ok=False, uncertain=True, side_effecting=True, executed=True)
        if result.get("status") is not None and not isinstance(result["status"], str):
            result.pop("status")
    if result.get("status") in {"unknown", "uncertain"}:
        result.update(ok=False, uncertain=True)
    elif result.get("status") in {"running", "queued", "waiting"}:
        result["pending"] = True
    if state in {"prepared", "unknown"} and result.get("side_effecting") is not False:
        result["uncertain"] = True
    return result
