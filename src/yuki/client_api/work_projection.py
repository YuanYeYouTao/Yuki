"""Public summaries of original Work effects and subscriptions, without tool payloads."""

import hashlib
import hmac
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.capabilities.executions import is_execution
from yuki.client_api.errors import ClientError
from yuki.client_api.pagination import decode_cursor
from yuki.runtime.effect_outcomes import execution_finished, historical_evidence, recorded_tool
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_wait_schema import waits

_COMMAND_TOOLS = frozenset({"run_command", "read_command", "control_command"})

# Display kinds of the original call (after historical names map to current tools).
# Display only: never an authorization, completion or classification of effects.
_KINDS: dict[str, str] = {
    **dict.fromkeys(_COMMAND_TOOLS, "terminal"),
    "computer": "computer",
    "browser": "browser",
    **dict.fromkeys(("send", "recall_message"), "message"),
    **dict.fromkeys(("read_chat", "find_contacts", "get_members"), "chat"),
    "web_search": "search",
    "read_webpage": "web",
    **dict.fromkeys(
        (
            "view_file",
            "copy_file",
            "workspace_publish",
            "read_result",
            "workspace_list",
            "workspace_read",
            "workspace_write",
        ),
        "file",
    ),
    "get_coding_agents": "agent_read",
    "use_coding_agent": "agent_control",
    **dict.fromkeys(
        ("search_memory", "save_memory", "edit_memory", "get_memory_fact", "set_short_note"),
        "memory",
    ),
    **dict.fromkeys(("get_schedules", "manage_schedule", "automation_list"), "schedule"),
    **dict.fromkeys(("manage_task", "start_subtask", "message_task", "get_tasks"), "task"),
    "wait": "wait",
    **dict.fromkeys(("get_environment", "time_get_current", "get_my_capabilities"), "environment"),
    "local_device": "device_legacy",
}
# Agent actions recorded on the original call are a closed protocol vocabulary.
_AGENT_ACTIONS = frozenset(
    {"prompt", "queue", "watch", "steer", "answer", "respond", "cancel", "detach", "get", "list"}
)
_AGENT_READS = frozenset({"get", "list"})


def step_id(secret: bytes, key: str) -> str:
    return "s_" + hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()


def timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


async def steps_token(session: AsyncSession, identity: str) -> tuple[int, str]:
    count, latest = (
        await session.execute(
            select(func.count(), func.max(effects.c.updated)).where(
                effects.c.work_id == identity, effects.c.kind == "tool"
            )
        )
    ).one()
    return count, f"{count}:{latest if latest is not None else 0}"


def step_operation(receipt: dict[str, Any]) -> str | None:
    """The external-agent operation an original call created or targeted, if any."""
    outcome = receipt.get("outcome")
    identity = outcome.get("operation_id") if isinstance(outcome, dict) else None
    return identity if isinstance(identity, str) else None


def public_step(
    row: Any, secret: bytes, readable_operations: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Project one original call to a public step.

    `readable_operations` are agent operations the viewer may read now (checked by the
    caller against their own ACL); no other cross-resource reference is returned.
    """
    receipt = json.loads(row["receipt_json"])
    evidence = historical_evidence(receipt, state=row["state"], kind=row["kind"])
    # Original receipts keep their recorded name; it is read through the history map.
    tool = recorded_tool(receipt.get("invocation", {}).get("tool_id", evidence.get("tool", "")))
    if row["state"] == "prepared":
        status = "recorded"
    elif row["state"] == "failed":
        status = "failed"
    elif row["state"] == "unknown" or evidence.get("uncertain"):
        status = "unknown"
    elif evidence.get("pending"):
        status = "running"
    elif evidence.get("ok") is False:
        status = "failed"
    elif evidence.get("ok") is True:
        execution = is_execution(
            tool, {"action": receipt.get("invocation", {}).get("action", evidence.get("action"))}
        )
        status = "succeeded" if not execution or execution_finished(evidence) else "unknown"
    else:
        status = "unknown"
    # Retired terminal_* names without a successor (terminal_write) stay terminal.
    terminal = tool in _COMMAND_TOOLS or tool.startswith("terminal_")
    category, label = (
        ("terminal", "終端")
        if terminal
        else ("search", "搜尋")
        if tool == "web_search"
        else ("web", "讀網頁")
        if tool == "read_webpage"
        else ("file", "讀取檔案")
        if tool == "view_file"
        else ("file", "保存檔案")
        if tool == "copy_file"
        else ("file", "發布檔案")
        if tool == "workspace_publish"
        else ("agent", "委託其他 Agent")
        if tool in {"use_coding_agent", "get_coding_agents"}
        else ("device", "本機操作（歷史）")
        if tool == "local_device"
        else ("memory", "記憶")
        if "memory" in tool
        else ("message", "傳送訊息")
        if tool == "send"
        else ("computer", "操作電腦")
        if tool == "computer"
        else ("browser", "使用瀏覽器")
        if tool == "browser"
        else ("other", "其他")
    )
    if terminal and evidence.get("step_label") in {"browser", "computer"}:
        category = evidence["step_label"]
        label = "使用瀏覽器" if category == "browser" else "操作電腦"
    artifacts = []
    stack = [receipt.get("result", {}), receipt.get("outcome", {})]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            version = value.get("version", value.get("sha256"))
            if isinstance(value.get("artifact_id"), str) and isinstance(version, str):
                reference = {"artifact_id": value["artifact_id"], "version": version}
                if reference not in artifacts:
                    artifacts.append(reference)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    invocation = receipt.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}
    outcome = receipt.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    kind = _KINDS.get(tool, "terminal" if terminal else "tool")
    if terminal and category in {"browser", "computer"}:
        kind = category
    recorded_action = invocation.get("action")
    action = (
        recorded_action
        if kind in {"agent_read", "agent_control"} and recorded_action in _AGENT_ACTIONS
        else None
    )
    if kind == "agent_control" and action in _AGENT_READS:
        # Historical `external_agent` also carried read-only list/get.
        kind = "agent_read"
    detail: dict[str, Any] = {"record_seconds": max(0.0, row["updated"] - row["created"])}
    if kind == "terminal":
        process = outcome.get("process")
        if isinstance(process, dict):
            if type(process.get("exit_code")) is int:
                detail["exit_code"] = process["exit_code"]
            if isinstance(process.get("output_lost"), bool):
                detail["output_lost"] = process["output_lost"]
        detail["where"] = "device" if isinstance(outcome.get("device_id"), str) else "yuki"
    elif kind == "message":
        if isinstance(outcome.get("delivered_message"), bool):
            # Text delivery only; attachments and partial delivery are not implied.
            detail["text_delivered"] = outcome["delivered_message"]
        target = outcome.get("delivery_target")
        if isinstance(target, dict) and target.get("kind") in {"group", "private", "channel"}:
            detail["target_kind"] = target["kind"]
    elif tool in {"start_subtask", "message_task", "manage_task"}:
        # The subtask (or delegation record) the call created or targeted: the Desktop
        # links the step to it and shows its live state (Client API 14).
        try:
            shown = json.loads(receipt.get("result") or "{}")
        except (TypeError, ValueError):
            shown = {}
        if isinstance(shown, dict) and isinstance(shown.get("id"), str):
            detail["subtask_id"] = shown["id"]
            if isinstance(shown.get("relationship"), str):
                detail["relationship"] = shown["relationship"]
    elif kind in {"agent_read", "agent_control"}:
        operation = step_operation(receipt)
        if operation is not None and operation in readable_operations:
            detail["operation_id"] = operation
    return {
        "step_id": step_id(secret, row["effect_key"]),
        "category": category,
        "label": label,
        "record": row["state"],
        "status": status,
        "created_at": timestamp(row["created"]),
        "updated_at": timestamp(row["updated"]),
        "artifacts": artifacts,
        "kind": kind,
        "tool": tool,
        "action": action,
        "detail": detail,
    }


async def step_position(
    session: AsyncSession, identity: str, value: str, secret: bytes
) -> tuple[float, str]:
    decoded = decode_cursor(value)
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or type(decoded[0]) not in (int, float)
        or not math.isfinite(decoded[0])
        or not isinstance(decoded[1], str)
    ):
        raise ClientError("validation_error")
    created, reference = decoded
    # The cursor contains an opaque public ID, never an internal effect key.
    keys = await session.stream_scalars(
        select(effects.c.effect_key)
        .where(
            effects.c.work_id == identity, effects.c.kind == "tool", effects.c.created == created
        )
        .order_by(effects.c.effect_key)
    )
    async for key in keys:
        if hmac.compare_digest(step_id(secret, key), reference):
            return created, key
    raise ClientError("validation_error")


async def wait_view(
    session: AsyncSession, identity: str, run_kind: Callable[[str, str], str | None] | None = None
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                select(waits).where(waits.c.work_id == identity, waits.c.status == "active")
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    conditions = []
    for item in json.loads(row["conditions_json"]):
        kind = item["kind"]
        summary: dict[str, Any] = {"kind": kind}
        if kind == "time_due":
            summary["due_at"] = timestamp(item["due"])
        elif kind == "plugin_event":
            summary.update(plugin_id=item["plugin_id"], event_type=item["event_type"])
        elif kind == "owned_run":
            tools = await session.scalars(
                select(func.json_extract(effects.c.receipt_json, "$.invocation.tool_id")).where(
                    effects.c.work_id == identity,
                    func.json_extract(effects.c.receipt_json, "$.outcome.run_id") == item["run_id"],
                )
            )
            names = {recorded_tool(name) for name in tools if isinstance(name, str)}
            summary["run_kind"] = (run_kind(item["run_id"], identity) if run_kind else None) or (
                "terminal"
                if "run_command" in names
                else "acp"
                if "use_coding_agent" in names
                else "other"
            )
        conditions.append(summary)
    return {
        "mode": row["mode"],
        "since": timestamp(row["created"]),
        "deadline_at": timestamp(row["deadline"]),
        "conditions": conditions,
    }
