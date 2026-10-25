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
from yuki.runtime.effect_outcomes import execution_finished, historical_evidence
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_wait_schema import waits
from yuki.workspace.tools import WORKSPACE_READ_TOOLS, WORKSPACE_TOOLS


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


def public_step(row: Any, secret: bytes) -> dict[str, Any]:
    receipt = json.loads(row["receipt_json"])
    evidence = historical_evidence(receipt, state=row["state"], kind=row["kind"])
    tool = receipt.get("invocation", {}).get("tool_id", evidence.get("tool", ""))
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
    category, label = (
        ("terminal", "終端")
        if tool.startswith("terminal_")
        else ("search", "搜尋")
        if tool == "web_search"
        else ("web", "讀網頁")
        if tool in {"read_webpage", "web_read"}
        else ("file", "讀取檔案")
        if tool in WORKSPACE_READ_TOOLS
        else ("file", "保存檔案")
        if tool in WORKSPACE_TOOLS
        else ("file", "發布檔案")
        if tool == "workspace_publish"
        else ("agent", "委託其他 Agent")
        if tool == "external_agent"
        else ("device", "本機操作（歷史）")
        if tool == "local_device"
        else ("memory", "記憶")
        if "memory" in tool
        else ("message", "傳送訊息")
        if tool in {"send", "send_message"}
        else ("other", "其他")
    )
    if tool.startswith("terminal_") and evidence.get("step_label") in {"browser", "computer"}:
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
    return {
        "step_id": step_id(secret, row["effect_key"]),
        "category": category,
        "label": label,
        "record": row["state"],
        "status": status,
        "created_at": timestamp(row["created"]),
        "updated_at": timestamp(row["updated"]),
        "artifacts": artifacts,
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
            names = set(tools)
            summary["run_kind"] = (run_kind(item["run_id"], identity) if run_kind else None) or (
                "terminal"
                if "terminal_exec" in names
                else "acp"
                if "external_agent" in names
                else "other"
            )
        conditions.append(summary)
    return {
        "mode": row["mode"],
        "since": timestamp(row["created"]),
        "deadline_at": timestamp(row["deadline"]),
        "conditions": conditions,
    }
