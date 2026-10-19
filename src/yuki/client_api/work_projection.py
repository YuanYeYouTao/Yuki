"""Public summaries of original Work effects and subscriptions, without tool payloads."""

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.runtime.effect_outcomes import execution_finished, historical_evidence
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_wait_schema import waits
from yuki.sandbox.environment_tools import is_execution


def step_secret(database_url: str) -> bytes:
    database = make_url(database_url).database
    if not database or database == ":memory:":
        return secrets.token_bytes(32)
    path = Path(database + ".client-steps.key")
    if not path.exists():
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".client-key-", delete=False
            ) as writer:
                temporary = writer.name
                writer.write(secrets.token_bytes(32))
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as reader:
        secret = reader.read(33)
    if len(secret) != 32:
        raise ValueError("invalid_client_steps_key")
    return secret


def step_id(secret: bytes, key: str) -> str:
    return "s_" + hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()


def cursor(value: Any) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(value: str) -> Any:
    try:
        return json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, TypeError):
        raise ClientError("validation_error") from None


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
        if tool == "web_read"
        else ("file", "發布檔案")
        if tool == "workspace_publish"
        else ("agent", "委託其他 Agent")
        if tool == "external_agent"
        else ("device", "使用你的電腦")
        if tool == "local_device"
        else ("memory", "記憶")
        if "memory" in tool
        else ("message", "傳送訊息")
        if tool == "send_message"
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
